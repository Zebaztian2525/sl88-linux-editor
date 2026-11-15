#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sl88_gui.py - Tkinter-editor för Studiologic SL88 Studio (Linux).

Lägg filen i samma mapp som sl88_protocol.py och sl88_editor.py:

    sudo apt install python3-tk
    source ~/sl88env/bin/activate
    python3 sl88_gui.py

Arkitektur:
  * ALL MIDI-kommunikation går via sl88_editor.SL88Editor och körs i EN
    arbetstråd (MidiWorker). GUI-tråden anropar aldrig mido/SL88Editor.
  * Resultat skickas tillbaka som rena datakopior via en queue som töms
    med root.after() -> fönstret fryser inte under en config dump.
  * print()-utskrifter från SL88Editor fångas (redirect_stdout) och visas
    i loggrutan. Det är process-globalt, men bara arbetstråden printar.

Upplägg följer SL Editor-manualen: PROGRAM-lista, PROGRAM-namn, ZONE 1-4,
STICK 1-3 (X/Y per zon, färgkodat som manualen) och SL CURVES.
"""

import contextlib
import queue
import sys
import threading
import traceback

try:
    import tkinter as tk
    from tkinter import ttk, messagebox, scrolledtext
except ImportError:
    print("tkinter saknas. Installera: sudo apt install python3-tk")
    sys.exit(1)

try:
    import mido
    import sl88_protocol as p
    from sl88_editor import SL88Editor   # sätter även mido-backend (rtmidi)
except ImportError as exc:
    print(f"Kunde inte importera MIDI-modulerna: {exc}\n"
          "Kontrollera att sl88_protocol.py och sl88_editor.py ligger i samma\n"
          "mapp och att mido + python-rtmidi är installerade i din venv.")
    sys.exit(1)

# --- versionsvakt: GUI:t behöver stick-stödet i protokoll och editor --------
_REQUIRED_P = ("NUM_STICKS", "STICK_CHOICES", "stick_field", "zone_offset",
               "stick_param_value", "stick_param_choice_from_value")
_missing = [n for n in _REQUIRED_P if not hasattr(p, n)]
if _missing or not hasattr(SL88Editor, "set_zone_field"):
    print("sl88_gui.py kräver nyare sl88_protocol.py / sl88_editor.py.")
    print(f"  sl88_protocol läses från: {p.__file__}")
    print(f"  Saknade symboler: {_missing or 'inga'}")
    print(f"  SL88Editor.set_zone_field finns: {hasattr(SL88Editor, 'set_zone_field')}")
    print("  Kör: python3 sl88_editor.py --check")
    sys.exit(1)

CURVE_CHOICES = ["soft", "medium", "hard"] + [f"memory{n}" for n in p.USER_MEMORY_RANGE]
STICK_CHOICES = p.STICK_CHOICES
DUMP_TIMEOUT = 5.0          # inaktivitetstimeout i request_config_dump
MAX_LOG_LINES = 2000
NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

# Zonfärger enligt SL Editor-manualen (zon 1 orange, 2 gul, 3 grön, 4 blå).
ZONE_STYLES = {
    1: {"bg": "#f59b23", "deep": "#a8620a", "tint": "#fde3c0", "ink": "#2b1600"},
    2: {"bg": "#e0d81c", "deep": "#8f8905", "tint": "#f7f3c1", "ink": "#2b2900"},
    3: {"bg": "#8fd094", "deep": "#3f7f47", "tint": "#dcf0dd", "ink": "#0e2a12"},
    4: {"bg": "#74b4e2", "deep": "#2f6d99", "tint": "#d8e9f6", "ink": "#0d2438"},
}
STICKS_BG = "#2b3038"


def note_name(n):
    """MIDI-notnummer -> namn (21 = A0, 108 = C8, som i dumpen)."""
    if not isinstance(n, int) or not 0 <= n <= 127:
        return str(n)
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 1}"


def require(ed):
    if ed is None:
        raise RuntimeError("Inte ansluten till SL88.")
    return ed


def snapshot(ed):
    """Kopia av editorns tillstånd - GUI-tråden läser aldrig ed direkt."""
    return {
        "names": dict(ed.program_names),
        "curves": {k: dict(v) for k, v in ed.curves.items()},
        "active": ed.active_program,
        "dry_run": ed.dry_run,
    }


# ---------------------------------------------------------------------------
# Brygga mellan arbetstråd och GUI
# ---------------------------------------------------------------------------
class QueueWriter:
    """Fil-liknande objekt: fångar print() och skickar rader till GUI-kön."""

    def __init__(self, out_q):
        self.q = out_q
        self.buf = ""

    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if line.strip():
                self.q.put(("log", line))
        return len(s)

    def flush(self):
        if self.buf.strip():
            self.q.put(("log", self.buf))
        self.buf = ""

    def isatty(self):
        return False


class Job:
    """fn(editor) körs i arbetstråden; on_done(result) körs i GUI-tråden.
    key != None betyder 'bara det senaste jobbet av denna sort körs'."""

    __slots__ = ("fn", "on_done", "label", "key")

    def __init__(self, fn, on_done=None, label="Arbetar", key=None):
        self.fn = fn
        self.on_done = on_done
        self.label = label
        self.key = key


class MidiWorker(threading.Thread):
    """Äger SL88Editor och gör alla mido-anrop, seriellt, i en tråd."""

    def __init__(self, out_q):
        super().__init__(daemon=True)
        self.out = out_q
        self.jobs = queue.Queue()
        self.editor = None
        self._stopping = threading.Event()

    # -- anropas från GUI-tråden -----------------------------------------
    def submit(self, job):
        self.jobs.put(job)

    def stop(self):
        self._stopping.set()

    # -- körs i arbetstråden ---------------------------------------------
    def close_editor(self):
        if self.editor is not None:
            try:
                self.editor.close()
            except Exception:
                pass
            self.editor = None

    def _drop_stale(self, job):
        """Finns ett nyare jobb med samma key i kön? Hoppa över detta."""
        if job.key is None:
            return job
        rest = []
        while True:
            try:
                rest.append(self.jobs.get_nowait())
            except queue.Empty:
                break
        newer = any(j.key == job.key for j in rest)
        for j in rest:
            self.jobs.put(j)
        if newer:
            self.out.put(("cancelled", job))
            return None
        return job

    def _run_job(self, job):
        self.out.put(("status", job.label + " …"))
        writer = QueueWriter(self.out)
        try:
            with contextlib.redirect_stdout(writer):
                result = job.fn(self.editor)
        except Exception as exc:
            writer.flush()
            self.out.put(("error", job, f"{type(exc).__name__}: {exc}"))
            if not isinstance(exc, (ValueError, RuntimeError)):
                self.out.put(("log", traceback.format_exc().rstrip()))
        else:
            writer.flush()
            self.out.put(("done", job, result))

    def run(self):
        while not self._stopping.is_set():
            try:
                job = self.jobs.get(timeout=0.1)
            except queue.Empty:
                continue
            job = self._drop_stale(job)
            if job is not None:
                self._run_job(job)
        self.close_editor()


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class SL88App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SL88 Editor – Studiologic SL88 Studio")
        self.geometry("1180x800")
        self.minsize(980, 620)

        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("TNotebook.Tab", padding=(14, 6), font=("", 9, "bold"))
        style.configure("Sticks.Vertical.TScrollbar", gripcount=0,
                        background="#f59b23", darkcolor="#a8620a",
                        lightcolor="#f59b23", troughcolor=STICKS_BG,
                        bordercolor=STICKS_BG, arrowcolor="#f2efe6")
        style.map("Sticks.Vertical.TScrollbar",
                  background=[("active", "#ffc069")])

        self.out_q = queue.Queue()
        self.worker = MidiWorker(self.out_q)
        self.worker.start()

        self.connected = False
        self.pending = 0
        self.closing = False

        self.names = {}             # 0-baserat programnummer -> namn
        self.curves = {}            # kurvplats 0..8 -> dict
        self.pdata = None           # tolkad programdata för vald plats
        self.current_program = None # 1-baserat

        self._conn_widgets = []
        self._prog_widgets = []
        self.zone_vars = []
        self.zone_labels = {}
        self.stick_vars = {}        # (zon, stick, 'x'/'y') -> StringVar
        self.stick_raw = {}         # (zon, stick) -> Label med råvärden
        self.stick_range = {}       # zon -> Label med tangentomfång

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._set_connected(False)
        self.after(30, self._poll)
        self.refresh_ports()

    # -- uppbyggnad -------------------------------------------------------
    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # ---- verktygsrad ----
        bar = ttk.Frame(self, padding=(8, 8, 8, 4))
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(bar, text="MIDI-port:").pack(side="left")
        self.port_var = tk.StringVar(value="SL STUDIO MIDI 2")
        self.port_cb = ttk.Combobox(bar, textvariable=self.port_var, width=34)
        self.port_cb.pack(side="left", padx=6)
        self.ports_btn = ttk.Button(bar, text="Uppdatera portar",
                                    command=self.refresh_ports)
        self.ports_btn.pack(side="left", padx=2)
        self.connect_btn = ttk.Button(bar, text="Anslut", command=self.toggle_connect)
        self.connect_btn.pack(side="left", padx=8)
        self.dry_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Torrkörning (skickar inget)", variable=self.dry_var,
                        command=self._on_dry_toggle).pack(side="left", padx=6)
        self.conn_lbl = ttk.Label(bar, text="● ej ansluten", foreground="#aa0000")
        self.conn_lbl.pack(side="right")

        # ---- delbar yta: programlista | programdetaljer ----
        paned = ttk.Panedwindow(self, orient="horizontal")
        paned.grid(row=1, column=0, sticky="nsew", padx=8, pady=4)
        left = ttk.Frame(paned, padding=4)
        right = ttk.Frame(paned, padding=4)
        paned.add(left, weight=1)
        paned.add(right, weight=3)
        self._build_program_list(left)
        self._build_details(right)

        # ---- logg ----
        logf = ttk.Frame(self, padding=(8, 0, 8, 4))
        logf.grid(row=2, column=0, sticky="ew")
        head = ttk.Frame(logf)
        head.pack(fill="x")
        ttk.Label(head, text="Logg (SysEx och status)").pack(side="left")
        ttk.Button(head, text="Rensa", width=7,
                   command=self._clear_log).pack(side="right")
        self.log = scrolledtext.ScrolledText(logf, height=8, wrap="none",
                                             state="disabled", font=("TkFixedFont", 9))
        self.log.pack(fill="x", pady=(2, 0))

        # ---- statusrad ----
        status = ttk.Frame(self)
        status.grid(row=3, column=0, sticky="ew")
        status.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="Redo. Välj MIDI-port och klicka Anslut.")
        ttk.Label(status, textvariable=self.status_var, anchor="w",
                  relief="sunken", padding=(6, 3)).grid(row=0, column=0, sticky="ew")
        self.progress = ttk.Progressbar(status, mode="indeterminate", length=140)
        self.progress.grid(row=0, column=1, padx=4, pady=2)

    def _build_program_list(self, parent):
        parent.rowconfigure(2, weight=1)
        parent.columnconfigure(0, weight=1)
        ttk.Label(parent, text=f"PROGRAM (1–{p.NUM_PROGRAMS})",
                  font=("", 10, "bold")).grid(row=0, column=0, sticky="w")
        filt = ttk.Frame(parent)
        filt.grid(row=1, column=0, sticky="ew", pady=4)
        ttk.Label(filt, text="Filter:").pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._fill_tree())
        ttk.Entry(filt, textvariable=self.filter_var).pack(side="left", fill="x",
                                                           expand=True, padx=4)
        wrap = ttk.Frame(parent)
        wrap.grid(row=2, column=0, sticky="nsew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(wrap, columns=("nr", "namn"), show="headings",
                                 selectmode="browse")
        self.tree.heading("nr", text="#")
        self.tree.heading("namn", text="Namn")
        self.tree.column("nr", width=48, anchor="e", stretch=False)
        self.tree.column("namn", width=190, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        btns = ttk.Frame(parent)
        btns.grid(row=3, column=0, sticky="ew", pady=6)
        self.dump_btn = ttk.Button(btns, text="Läs config dump", command=self.load_dump)
        self.dump_btn.pack(side="left")
        self.reload_btn = ttk.Button(btns, text="Läs om program",
                                     command=self.reload_program)
        self.reload_btn.pack(side="left", padx=6)
        self._conn_widgets.append(self.dump_btn)
        self._prog_widgets.append(self.reload_btn)
        self._fill_tree()

    def _build_details(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        # ---- programnamn ----
        pf = ttk.LabelFrame(parent, text="PROGRAM", padding=8)
        pf.grid(row=0, column=0, sticky="ew")
        pf.columnconfigure(1, weight=1)
        ttk.Label(pf, text="Namn:").grid(row=0, column=0, sticky="w")
        self.name_var = tk.StringVar()
        vcmd = (self.register(lambda s: len(s) <= p.NAME_LENGTH), "%P")
        self.name_entry = ttk.Entry(pf, textvariable=self.name_var, width=20,
                                    validate="key", validatecommand=vcmd)
        self.name_entry.grid(row=0, column=1, sticky="w", padx=6)
        self.name_entry.bind("<Return>", lambda e: self.save_name())
        self.save_btn = ttk.Button(pf, text="Spara namn", command=self.save_name)
        self.save_btn.grid(row=0, column=2, sticky="e")
        ttk.Label(pf, text=f"(max {p.NAME_LENGTH} tecken)").grid(row=0, column=3, padx=6)

        opt = ttk.Frame(pf)
        opt.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self.store_var = tk.BooleanVar(value=True)
        self.verify_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="STORE (sparar i minnet)",
                        variable=self.store_var).pack(side="left")
        ttk.Checkbutton(opt, text="Verifiera / läs tillbaka (långsammare)",
                        variable=self.verify_var).pack(side="left", padx=12)
        self.prog_info = ttk.Label(pf, text="Inget program valt.", foreground="#555")
        self.prog_info.grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self._prog_widgets += [self.name_entry, self.save_btn]

        # ---- flikar: zoner, sticks och kurvor ----
        nb = ttk.Notebook(parent)
        nb.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        zf = ttk.Frame(nb, padding=8)
        sf = ttk.Frame(nb, padding=8)
        cf = ttk.Frame(nb, padding=8)
        nb.add(zf, text="ZONE 1–4")
        nb.add(sf, text="STICKS")
        nb.add(cf, text="SL CURVES")
        self._build_zones(zf)
        self._build_sticks(sf)
        self._build_curves(cf)

    def _build_zones(self, zf):
        headers = ["Zon", "Velocity-kurva", "", "Aktiv*", "Kanal*", "Port*",
                   "Volym*", "Tangentomfång*", "Fast vel*"]
        for c, txt in enumerate(headers):
            ttk.Label(zf, text=txt, font=("", 9, "bold")).grid(
                row=0, column=c, sticky="w", padx=5, pady=(0, 6))
        for z in range(1, p.NUM_ZONES + 1):
            ttk.Label(zf, text=f"ZON {z}", font=("", 9, "bold"),
                      foreground=ZONE_STYLES[z]["deep"]).grid(
                row=z, column=0, sticky="w", padx=5, pady=3)
            var = tk.StringVar()
            self.zone_vars.append(var)
            ttk.Combobox(zf, textvariable=var, values=CURVE_CHOICES,
                         state="readonly", width=12).grid(row=z, column=1, padx=5)
            btn = ttk.Button(zf, text="Sätt", width=6,
                             command=lambda z=z: self.apply_curve(z))
            btn.grid(row=z, column=2, padx=2)
            self._prog_widgets.append(btn)
            for col, field in ((3, "enabled"), (4, "midi_channel"),
                               (5, "midi_port"), (6, "volume"),
                               (7, "range"), (8, "fixed_velocity")):
                lbl = ttk.Label(zf, text="–")
                lbl.grid(row=z, column=col, sticky="w", padx=5)
                self.zone_labels[(z, field)] = lbl

        ttk.Label(zf, text="* råvärden ur programdatan (okodade).",
                  foreground="#666").grid(row=p.NUM_ZONES + 1, column=0, columnspan=9,
                                          sticky="w", pady=(10, 0))
        self.curve_raw_lbl = ttk.Label(zf, text="", foreground="#666", wraplength=680,
                                       justify="left")
        self.curve_raw_lbl.grid(row=p.NUM_ZONES + 2, column=0, columnspan=9, sticky="w")
        ttk.Label(zf, text="Att sätta kurva skriver hela kurvtyp-byten och stänger av "
                           "fast velocity för zonen.", foreground="#a05",
                  wraplength=680, justify="left").grid(
            row=p.NUM_ZONES + 3, column=0, columnspan=9, sticky="w", pady=(8, 0))

    # ---- STICKS ---------------------------------------------------------
    def _build_sticks(self, parent):
        """Färgkodad, scrollbar vy: STICK 1-3 X/Y för varje zon."""
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        top = ttk.Frame(parent)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(top, text="STICK 1–3 · X/Y-parameter per zon",
                  font=("", 10, "bold")).pack(side="left")
        self.sticks_all_btn = tk.Button(
            top, text="Sätt alla zoner", command=self.apply_all_sticks,
            bg="#3a4048", fg="#f2efe6", activebackground="#565e69",
            activeforeground="#ffffff", disabledforeground="#8b929b",
            relief="flat", font=("", 9, "bold"), cursor="hand2", padx=10)
        self.sticks_all_btn.pack(side="right")
        self._prog_widgets.append(self.sticks_all_btn)
        ttk.Label(top, text="Off=0 · Pitch Bend=1 · Aftertouch=2 · CC N = N+2",
                  foreground="#666").pack(side="right", padx=12)

        wrap = ttk.Frame(parent)
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.sticks_canvas = tk.Canvas(wrap, bg=STICKS_BG, highlightthickness=0)
        self.sticks_canvas.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(wrap, orient="vertical", style="Sticks.Vertical.TScrollbar",
                           command=self.sticks_canvas.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.sticks_canvas.configure(yscrollcommand=sb.set)
        self.sticks_inner = tk.Frame(self.sticks_canvas, bg=STICKS_BG)
        self._sticks_win = self.sticks_canvas.create_window(
            (0, 0), window=self.sticks_inner, anchor="nw")
        self.sticks_inner.columnconfigure(0, weight=1)
        self.sticks_inner.bind(
            "<Configure>",
            lambda e: self.sticks_canvas.configure(
                scrollregion=self.sticks_canvas.bbox("all")))
        self.sticks_canvas.bind(
            "<Configure>",
            lambda e: self.sticks_canvas.itemconfigure(self._sticks_win, width=e.width))
        for seq in ("<Button-4>", "<Button-5>", "<MouseWheel>"):
            self.bind_all(seq, self._on_stick_wheel)

        for z in range(1, p.NUM_ZONES + 1):
            self._build_stick_zone(z)

        ttk.Label(self.sticks_inner,
                  text="Värdena är ANTAGANDE enligt mönstret CC N = N+2 "
                       "(Modulation = CC 1 = 3). Verifiera mot hårdvaran med "
                       "'Verifiera / läs tillbaka' ikryssad.",
                  background=STICKS_BG, foreground="#b9c0ca",
                  wraplength=700, justify="left").grid(
            row=p.NUM_ZONES, column=0, sticky="w", padx=12, pady=(4, 12))

    def _build_stick_zone(self, z):
        st = ZONE_STYLES[z]
        card = tk.Frame(self.sticks_inner, bg=st["bg"], highlightthickness=2,
                        highlightbackground=st["deep"], bd=0)
        card.grid(row=z - 1, column=0, sticky="ew", padx=12, pady=6)
        for c in (1, 2):
            card.columnconfigure(c, weight=1)

        head = tk.Frame(card, bg=st["deep"])
        head.grid(row=0, column=0, columnspan=5, sticky="ew")
        tk.Label(head, text=f" {z} ", bg=st["bg"], fg=st["ink"],
                 font=("", 11, "bold")).pack(side="left", padx=6, pady=3)
        tk.Label(head, text=f"ZONE {z}", bg=st["deep"], fg="#ffffff",
                 font=("", 10, "bold")).pack(side="left")
        rng = tk.Label(head, text="–", bg=st["deep"], fg="#e8e8e8",
                       font=("TkFixedFont", 9))
        rng.pack(side="left", padx=10)
        self.stick_range[z] = rng
        zbtn = tk.Button(head, text="Sätt zon",
                         command=lambda z=z: self.apply_zone_sticks(z),
                         bg=st["bg"], fg=st["ink"], activebackground=st["tint"],
                         activeforeground=st["ink"], disabledforeground="#9a9a9a",
                         relief="flat", font=("", 9, "bold"), cursor="hand2", padx=8)
        zbtn.pack(side="right", padx=6, pady=3)
        self._prog_widgets.append(zbtn)

        for c, txt in ((0, "Stick"), (1, "X-parameter"), (2, "Y-parameter"),
                       (3, "Råvärde"), (4, "")):
            tk.Label(card, text=txt, bg=st["bg"], fg=st["ink"],
                     font=("", 9, "bold")).grid(row=1, column=c, sticky="w",
                                                padx=6, pady=(6, 2))

        for s in range(1, p.NUM_STICKS + 1):
            r = s + 1
            tk.Label(card, text=f"STICK {s}", bg=st["bg"], fg=st["ink"],
                     font=("", 9, "bold")).grid(row=r, column=0, sticky="w",
                                                padx=6, pady=3)
            for col, axis in ((1, "x"), (2, "y")):
                var = tk.StringVar()
                self.stick_vars[(z, s, axis)] = var
                ttk.Combobox(card, textvariable=var, values=STICK_CHOICES,
                             state="readonly", width=22).grid(
                    row=r, column=col, sticky="ew", padx=6, pady=3)
            raw = tk.Label(card, text="–", bg=st["bg"], fg=st["ink"],
                           font=("TkFixedFont", 9))
            raw.grid(row=r, column=3, sticky="w", padx=6)
            self.stick_raw[(z, s)] = raw
            btn = tk.Button(card, text="Sätt", width=6,
                            command=lambda z=z, s=s: self.apply_stick(z, s),
                            bg=st["deep"], fg="#ffffff", activebackground=st["ink"],
                            activeforeground="#ffffff", disabledforeground="#c9c9c9",
                            relief="flat", font=("", 9, "bold"), cursor="hand2")
            btn.grid(row=r, column=4, padx=6, pady=3)
            self._prog_widgets.append(btn)

        tk.Frame(card, bg=st["bg"], height=6).grid(
            row=p.NUM_STICKS + 2, column=0, columnspan=5, sticky="ew")

    def _on_stick_wheel(self, event):
        """Scrolla STICKS-ytan bara när pekaren är över den."""
        w = self.sticks_canvas.winfo_containing(event.x_root, event.y_root)
        while w is not None:
            if w is self.sticks_canvas or w is self.sticks_inner:
                break
            w = getattr(w, "master", None)
        else:
            return
        if event.num == 4 or getattr(event, "delta", 0) > 0:
            self.sticks_canvas.yview_scroll(-2, "units")
        elif event.num == 5 or getattr(event, "delta", 0) < 0:
            self.sticks_canvas.yview_scroll(2, "units")

    def _build_curves(self, cf):
        cf.rowconfigure(1, weight=1)
        cf.columnconfigure(0, weight=1)
        ttk.Label(cf, text="Kurvdefinitioner från config dump (plats 0–8)").grid(
            row=0, column=0, sticky="w", pady=(0, 4))
        self.curve_tree = ttk.Treeview(cf, columns=("slot", "val", "namn", "typ", "chk"),
                                       show="headings", height=9)
        for col, txt, w in (("slot", "Plats", 50), ("val", "Val", 90),
                            ("namn", "Namn", 160), ("typ", "Typ", 90),
                            ("chk", "Checksum", 90)):
            self.curve_tree.heading(col, text=txt)
            self.curve_tree.column(col, width=w, anchor="w")
        self.curve_tree.grid(row=1, column=0, sticky="nsew")
        ttk.Label(cf, text="medium/soft/hard = plats 0–2 (faktori). "
                           "memory4…memory9 = användarkurva 1–6 (plats 3–8).",
                  foreground="#666").grid(row=2, column=0, sticky="w", pady=6)

    # -- kö-hantering -----------------------------------------------------
    def _submit(self, fn, on_done=None, label="Arbetar", key=None):
        self.pending += 1
        if self.pending == 1:
            self.progress.start(12)
        self._update_buttons()
        self.worker.submit(Job(fn, on_done, label, key))

    def _job_finished(self):
        self.pending = max(0, self.pending - 1)
        if self.pending == 0:
            self.progress.stop()
        self._update_buttons()

    def _poll(self):
        """Tömmer kön från arbetstråden – allt här körs i GUI-tråden."""
        if self.closing:
            return
        try:
            while True:
                item = self.out_q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._append_log(item[1])
                elif kind == "status":
                    self.status_var.set(item[1])
                elif kind == "cancelled":
                    self._job_finished()
                elif kind == "done":
                    _, job, result = item
                    self._job_finished()
                    self.status_var.set(f"Klar – {job.label}")
                    if job.on_done:
                        try:
                            job.on_done(result)
                        except Exception:
                            self._append_log(traceback.format_exc().rstrip())
                elif kind == "error":
                    _, job, text = item
                    self._job_finished()
                    self._append_log("FEL: " + text)
                    self.status_var.set("Fel: " + text)
                    if job.key == "connect":
                        self._set_connected(False)
                        messagebox.showerror("Anslutning misslyckades", text)
        except queue.Empty:
            pass
        self.after(30, self._poll)

    # -- logg/status ------------------------------------------------------
    def _append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip("\n") + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > MAX_LOG_LINES:
            self.log.delete("1.0", "200.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _set_connected(self, flag):
        self.connected = flag
        self.connect_btn.config(text="Koppla från" if flag else "Anslut")
        self.conn_lbl.config(text="● ansluten" if flag else "● ej ansluten",
                             foreground="#008800" if flag else "#aa0000")
        self.port_cb.config(state="disabled" if flag else "normal")
        self._update_buttons()

    def _update_buttons(self):
        idle = self.pending == 0
        live = self.connected and idle
        has_prog = self.current_program is not None
        for w in self._conn_widgets:
            w.config(state="normal" if live else "disabled")
        for w in self._prog_widgets:
            w.config(state="normal" if live and has_prog else "disabled")
        self.connect_btn.config(state="normal" if idle else "disabled")
        self.ports_btn.config(state="normal" if idle else "disabled")

    # -- listor -----------------------------------------------------------
    def _fill_tree(self):
        kids = self.tree.get_children()
        if kids:
            self.tree.delete(*kids)
        filt = self.filter_var.get().strip().lower()
        for num in range(1, p.NUM_PROGRAMS + 1):
            name = self.names.get(num - 1, "")
            if filt and filt not in str(num) and filt not in name.lower():
                continue
            self.tree.insert("", "end", iid=str(num), values=(num, name or "–"))
        sel = self.current_program
        if sel is not None and self.tree.exists(str(sel)):
            self.tree.selection_set(str(sel))   # utlöser event, men numret är samma
            self.tree.see(str(sel))

    def _fill_curves(self):
        kids = self.curve_tree.get_children()
        if kids:
            self.curve_tree.delete(*kids)
        for slot in sorted(self.curves):
            c = self.curves[slot]
            val = {0: "medium", 1: "soft", 2: "hard"}.get(slot, f"memory{slot + 1}")
            kind = "användare" if c["type"] == 0x55 else "faktori"
            self.curve_tree.insert("", "end", values=(
                slot, val, c["name"], kind, "OK" if c["checksum_ok"] else "FEL"))

    def _clear_zones(self):
        for var in self.zone_vars:
            var.set("")
        for lbl in self.zone_labels.values():
            lbl.config(text="–")
        self.curve_raw_lbl.config(text="")
        self._clear_sticks()

    def _clear_sticks(self):
        for var in self.stick_vars.values():
            var.set("")
        for lbl in self.stick_raw.values():
            lbl.config(text="–")
        for lbl in self.stick_range.values():
            lbl.config(text="–")

    def _show_program(self, num, pdata):
        self.current_program = num
        self.pdata = pdata
        self.name_var.set(pdata["name"])
        chk = "OK" if pdata["checksum_ok"] else "FEL"
        self.prog_info.config(
            text=f"Program {num} • namn {pdata['name']!r} • checksum {chk}")
        if not pdata["checksum_ok"]:
            self._append_log(f"VARNING: checksumfel i programdata för program {num}.")
        for z, zd in enumerate(pdata["zones"], start=1):
            choice = p.curve_choice_from_type(zd["curve_type"])
            self.zone_vars[z - 1].set(choice or "")
            self.zone_labels[(z, "enabled")].config(text=str(zd["enabled"]))
            self.zone_labels[(z, "midi_channel")].config(text=str(zd["midi_channel"]))
            self.zone_labels[(z, "midi_port")].config(text=str(zd["midi_port"]))
            self.zone_labels[(z, "volume")].config(text=str(zd["volume"]))
            self.zone_labels[(z, "range")].config(
                text=f"{note_name(zd['low_key'])}–{note_name(zd['high_key'])}")
            self.zone_labels[(z, "fixed_velocity")].config(text=str(zd["fixed_velocity"]))
        self._update_curve_raw()
        self._show_sticks(pdata)
        self._update_buttons()

    def _show_sticks(self, pdata):
        """Fyller stick-dropdowns och råvärden från programdata."""
        for z in range(1, p.NUM_ZONES + 1):
            zd = pdata["zones"][z - 1]
            self.stick_range[z].config(
                text=f"{note_name(zd['low_key'])}–{note_name(zd['high_key'])}")
            for s in range(1, p.NUM_STICKS + 1):
                xv = zd.get(p.stick_field(s, "x"), 0)
                yv = zd.get(p.stick_field(s, "y"), 0)
                self.stick_vars[(z, s, "x")].set(p.stick_param_choice_from_value(xv) or "")
                self.stick_vars[(z, s, "y")].set(p.stick_param_choice_from_value(yv) or "")
                self.stick_raw[(z, s)].config(text=f"X {xv} · Y {yv}")
                for axis, val in (("x", xv), ("y", yv)):
                    if p.stick_param_choice_from_value(val) is None:
                        self._append_log(
                            f"Zon {z} stick {s} {axis.upper()}: okänt värde {val} "
                            "– välj parameter manuellt.")

    def _update_curve_raw(self):
        if not self.pdata:
            self.curve_raw_lbl.config(text="")
            return
        parts = [f"zon {i + 1}: {p.describe_curve_type(z['curve_type'])} "
                 f"(0x{z['curve_type']:02X})"
                 for i, z in enumerate(self.pdata["zones"])]
        self.curve_raw_lbl.config(text="Kurvtyp i datan – " + " | ".join(parts))

    # -- åtgärder (allt tungt sker i arbetstråden) -------------------------
    def refresh_ports(self):
        def fn(_ed):
            return {"in": mido.get_input_names(), "out": mido.get_output_names()}

        def done(res):
            values = sorted(set(res["in"]) | set(res["out"]))
            self.port_cb["values"] = values
            current = self.port_var.get().strip()
            if values and not any(current.lower() in v.lower() for v in values):
                sl = [v for v in values if "sl" in v.lower() and "studio" in v.lower()]
                pref = [v for v in sl if "midi 2" in v.lower()] or sl
                self.port_var.set(pref[0] if pref else values[0])
            self._append_log(f"Ingångar: {res['in']}")
            self._append_log(f"Utgångar: {res['out']}")
            self.status_var.set(f"Hittade {len(values)} MIDI-portar.")

        self._submit(fn, done, "Listar MIDI-portar", key="ports")

    def toggle_connect(self):
        if self.connected:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        port = self.port_var.get().strip()
        if not port:
            messagebox.showwarning("SL88", "Ange en MIDI-port (del av namnet räcker).")
            return
        dry = self.dry_var.get()

        def fn(_ed):
            self.worker.close_editor()
            self.worker.editor = SL88Editor(port, dry_run=dry)  # kan kasta RuntimeError
            return {"port": port}

        def done(res):
            self._set_connected(True)
            self.status_var.set(f"Ansluten till {res['port']}.")
            self.load_dump()

        self._submit(fn, done, f"Ansluter till '{port}'", key="connect")

    def _disconnect(self):
        def fn(_ed):
            self.worker.close_editor()
            return {}

        def done(_res):
            self._set_connected(False)
            self.status_var.set("Frånkopplad.")

        self._submit(fn, done, "Kopplar från", key="connect")

    def _on_dry_toggle(self):
        dry = self.dry_var.get()
        if not self.connected:
            return

        def fn(ed):
            require(ed).dry_run = dry
            return {"dry": dry}

        def done(res):
            self.status_var.set("Torrkörning PÅ – inget skickas." if res["dry"]
                                else "Torrkörning AV – skickar på riktigt.")

        self._submit(fn, done, "Uppdaterar torrkörning", key="dry")

    def load_dump(self):
        def fn(ed):
            require(ed)
            count = ed.request_config_dump(timeout=DUMP_TIMEOUT, summary=False)
            return {"count": count, **snapshot(ed)}

        def done(res):
            self.names = res["names"]
            self.curves = res["curves"]
            self._fill_tree()
            self._fill_curves()
            self.status_var.set(f"Dump: {res['count']} meddelanden, "
                                f"{len(self.names)} programnamn, "
                                f"{len(self.curves)} kurvor.")

        self._submit(fn, done, "Läser config dump", key="dump")

    def _on_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        num = int(sel[0])
        if num == self.current_program:
            return
        if not self.connected:
            self.status_var.set("Inte ansluten – klicka Anslut först.")
            return
        if self.dry_var.get():
            # I torrkörning skickas ingen Recall -> ingen läsning att visa.
            self.current_program = num
            self.pdata = None
            self._clear_zones()
            self.name_var.set(self.names.get(num - 1, ""))
            self.prog_info.config(text=f"Program {num} (torrkörning – ingen läsning)")
            self.status_var.set(f"Torrkörning: program {num} valt, inget läses.")
            self._update_buttons()
            return
        self.load_program(num)

    def load_program(self, num):
        self.current_program = num
        self.pdata = None
        self._clear_zones()
        self.prog_info.config(text=f"Program {num} – läser …")
        self._update_buttons()

        def fn(ed):
            require(ed)
            pdata = ed.read_program(num)   # Recall (+ config dump som fallback)
            return {"pdata": pdata, **snapshot(ed)}

        def done(res):
            if res["names"]:
                self.names = res["names"]
                self.curves = res["curves"]
                self._fill_tree()
                self._fill_curves()
            pdata = res["pdata"]
            if pdata is None:
                self.prog_info.config(text=f"Program {num} – ingen programdata mottogs.")
                self.status_var.set(f"Program {num}: ingen programdata (01) i svaret.")
                return
            self._show_program(num, pdata)
            self.status_var.set(f"Program {num} inläst: {pdata['name']!r}")

        self._submit(fn, done, f"Läser program {num}", key="load")

    def reload_program(self):
        if self.current_program is not None:
            self.load_program(self.current_program)

    def save_name(self):
        num = self.current_program
        if num is None:
            messagebox.showinfo("SL88", "Välj ett program i listan först.")
            return
        name = self.name_var.get()
        store, verify = self.store_var.get(), self.verify_var.get()

        def fn(ed):
            require(ed)
            ok = ed.rename_program(num, name, store=store, verify=verify)
            return {"ok": ok, **snapshot(ed)}

        def done(res):
            if res["dry_run"]:
                self.status_var.set("Torrkörning – namnet skickades inte.")
                return
            self.names = res["names"] or self.names
            if not store:
                self.names[num - 1] = name.rstrip()   # finns bara i RAM
            self._fill_tree()
            if res["ok"] is False:
                self.status_var.set(f"Verifiering AVVEK för program {num} – se loggen.")
            elif store:
                self.status_var.set(f"Program {num} sparat som {name.rstrip()!r}.")
            else:
                self.status_var.set(f"Namn ändrat i RAM för program {num} "
                                    "(STORE av – försvinner vid programbyte).")

        self._submit(fn, done, f"Byter namn på program {num}", key="write")

    def apply_curve(self, zone):
        num = self.current_program
        if num is None:
            messagebox.showinfo("SL88", "Välj ett program i listan först.")
            return
        choice = self.zone_vars[zone - 1].get()
        if not choice:
            messagebox.showinfo("SL88", f"Välj en kurva för zon {zone} "
                                        f"({p.CURVE_CHOICE_HELP}).")
            return
        store, verify = self.store_var.get(), self.verify_var.get()

        def fn(ed):
            require(ed)
            ed.set_velocity_curve(choice, zone=zone, program_number=num, store=store)
            pdata = ed.read_program(num) if (verify and not ed.dry_run) else None
            return {"pdata": pdata, **snapshot(ed)}

        def done(res):
            if res["dry_run"]:
                self.status_var.set(f"Torrkörning – kurva {choice} zon {zone} "
                                    "skickades inte.")
                return
            if res["pdata"] is not None:
                self.names = res["names"] or self.names
                self._fill_tree()
                self._show_program(num, res["pdata"])
                got = p.curve_choice_from_type(
                    res["pdata"]["zones"][zone - 1]["curve_type"])
                self.status_var.set(f"Zon {zone}: {choice} skickat, tillbaka läst: {got}.")
            else:
                if self.pdata:
                    self.pdata["zones"][zone - 1]["curve_type"] = p.curve_type_value(choice)
                    self._update_curve_raw()
                self.status_var.set(f"Zon {zone}: kurva {choice} satt"
                                    + (" och sparad (STORE)." if store else " (ej STORE)."))

        self._submit(fn, done, f"Sätter kurva {choice} i zon {zone}", key="write")

    # ---- STICKS: åtgärder ----------------------------------------------
    def _stick_values(self, zone, sticks):
        """Läser dropdowns för valda sticks i en zon -> [(stick, xv, yv, xval, yval)].
        Visar felruta och returnerar None om något är ogiltigt."""
        out = []
        for s in sticks:
            xval = self.stick_vars[(zone, s, "x")].get()
            yval = self.stick_vars[(zone, s, "y")].get()
            if not xval or not yval:
                messagebox.showinfo("SL88", f"Välj både X- och Y-parameter för "
                                            f"STICK {s} i zon {zone}.")
                return None
            try:
                out.append((s, p.stick_param_value(xval), p.stick_param_value(yval),
                            xval, yval))
            except ValueError as exc:
                messagebox.showerror("SL88", str(exc))
                return None
        return out

    def _apply_sticks(self, changes, label):
        """changes: {zon: [(stick, xv, yv, xval, yval), ...]}"""
        num = self.current_program
        store, verify = self.store_var.get(), self.verify_var.get()

        def fn(ed):
            require(ed)
            if not ed.dry_run:
                ed.recall_program(num)          # ett Recall för hela batchen
            for zone, items in changes.items():
                for s, xv, yv, _x, _y in items:
                    ed.set_zone_field(p.stick_field(s, "x"), zone, xv)
                    ed.set_zone_field(p.stick_field(s, "y"), zone, yv)
            if store:
                ed.store_program(num)
            pdata = ed.read_program(num) if (verify and not ed.dry_run) else None
            return {"pdata": pdata, **snapshot(ed)}

        def done(res):
            if res["dry_run"]:
                self.status_var.set(f"Torrkörning – {label} skickades inte.")
                return
            if res["pdata"] is not None:
                self.names = res["names"] or self.names
                self._fill_tree()
                self._show_program(num, res["pdata"])
                self.status_var.set(f"{label}: skickat och tillbaka läst – se STICKS.")
                return
            if self.pdata:
                for zone, items in changes.items():
                    for s, xv, yv, _x, _y in items:
                        self.pdata["zones"][zone - 1][p.stick_field(s, "x")] = xv
                        self.pdata["zones"][zone - 1][p.stick_field(s, "y")] = yv
                self._show_sticks(self.pdata)
            self.status_var.set(f"{label}: skickat"
                                + (" och sparat (STORE)." if store else " (ej STORE)."))

        self._submit(fn, done, label, key="write")

    def apply_stick(self, zone, stick):
        if self.current_program is None:
            messagebox.showinfo("SL88", "Välj ett program i listan först.")
            return
        items = self._stick_values(zone, [stick])
        if items is None:
            return
        self._apply_sticks({zone: items}, f"Sätter STICK {stick} X/Y i zon {zone}")

    def apply_zone_sticks(self, zone):
        if self.current_program is None:
            messagebox.showinfo("SL88", "Välj ett program i listan först.")
            return
        items = self._stick_values(zone, range(1, p.NUM_STICKS + 1))
        if items is None:
            return
        self._apply_sticks({zone: items}, f"Sätter alla sticks i zon {zone}")

    def apply_all_sticks(self):
        if self.current_program is None:
            messagebox.showinfo("SL88", "Välj ett program i listan först.")
            return
        changes = {}
        for z in range(1, p.NUM_ZONES + 1):
            items = self._stick_values(z, range(1, p.NUM_STICKS + 1))
            if items is None:
                return
            changes[z] = items
        self._apply_sticks(changes, "Sätter alla sticks i alla zoner (24 fält)")

    # -- avslut -----------------------------------------------------------
    def _on_close(self):
        self.closing = True
        self.worker.stop()
        self.worker.join(timeout=1.0)   # låt ett pågående jobb avslutas snyggt
        self.destroy()


def main():
    app = SL88App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())