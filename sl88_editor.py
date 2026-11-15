#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sl88_editor.py - Minimal Linux-editor för Studiologic SL88 (SysEx via mido).

Kräver sl88_protocol.py i samma mapp. Installation:
  sudo apt install build-essential python3-dev python3-venv libasound2-dev libjack-dev
  python3 -m venv ~/sl88env && source ~/sl88env/bin/activate
  pip install mido python-rtmidi

Användning (portnamn = delsträng, se --list):
  python3 sl88_editor.py --list
  python3 sl88_editor.py --check                      # API + självtest, ingen hårdvara
  python3 sl88_editor.py --port "SL STUDIO MIDI 2" --dump
  python3 sl88_editor.py --port "SL STUDIO MIDI 2" --read 250 --send
  python3 sl88_editor.py --port "SL STUDIO MIDI 2" --rename 250 "TEST NAMN" --send --store --verify
  python3 sl88_editor.py --port "SL STUDIO MIDI 2" --program 250 --curve soft --zone 1 --send
  python3 sl88_editor.py --port "SL STUDIO MIDI 2" --program 250 --zone 1 \
      --stick 1 "Pitch Bend" "CC 1 (Modulation)" --send --store

Terminologi följer SL Editor-manualen: PROGRAM, STORE, ZONE 1-4, STICK 1-3,
SL CURVES (soft, medium, hard, memory4..memory9).
rename/curve/stick är TORRKÖRNING tills du lägger till --send.
"""

import argparse
import sys
import time

import mido
import sl88_protocol as p

mido.set_backend("mido.backends.rtmidi")

GUI_API_PROTOCOL = [
    "NUM_PROGRAMS", "NUM_ZONES", "NUM_STICKS", "NAME_LENGTH", "NAME_OFFSET",
    "ZONE_FIELDS", "USER_MEMORY_RANGE", "CURVE_CHOICE_HELP", "STICK_CHOICES",
    "curve_type_value", "curve_choice_from_type", "describe_curve_type",
    "stick_param_value", "stick_param_choice_from_value", "stick_param_choices",
    "describe_stick_value", "zone_offset", "stick_field",
    "parse_program_data", "parse_program_name", "parse_velocity_curve",
    "build_request_config_dump", "build_recall_program", "build_store_program",
    "build_alter_active_program", "build_rename", "build_set_curve",
    "build_set_stick", "hexdump",
]
GUI_API_EDITOR = [
    "request_config_dump", "read_program", "recall_program", "store_program",
    "rename_program", "set_velocity_curve", "set_zone_field", "set_stick",
    "poke", "close",
]


class SL88Editor:
    def __init__(self, port_name, dry_run=False):
        """Öppnar in- och utport (delsträngsmatchning). dry_run=True skickar
        inget, utan skriver bara ut bytes."""
        self.dry_run = dry_run
        self.port_name = port_name
        self.program_names = {}     # 0-baserat programnummer -> namn
        self.curves = {}            # kurvplats 0..8 -> tolkad kurva (dict)
        self.active_program = None  # senast mottagna programdata (01), tolkad
        self._closed = False

        in_name = self._match(port_name, mido.get_input_names())
        out_name = self._match(port_name, mido.get_output_names())
        self.inport = mido.open_input(in_name)
        self.outport = mido.open_output(out_name)
        self.in_name, self.out_name = in_name, out_name
        print(f"Öppnade in: {in_name}\n         ut: {out_name}")

    @staticmethod
    def _match(needle, names):
        for n in names:
            if needle.lower() in n.lower():
                return n
        raise RuntimeError(f"Ingen MIDI-port matchar '{needle}'. Tillgängliga: {names}")

    def close(self):
        """Idempotent - GUI:t kan råka stänga två gånger."""
        if self._closed:
            return
        self._closed = True
        for port in (getattr(self, "inport", None), getattr(self, "outport", None)):
            try:
                if port is not None:
                    port.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- låg nivå ----------------------------------------------------------
    def _send(self, message):
        data = p.HEADER_OUT + list(message)
        assert all(b < 0x80 for b in data), "Databyte med bit 7 satt - ogiltig SysEx!"
        print(f"SysEx: F0 {p.hexdump(data)} F7")
        if self.dry_run:
            print("  (torrkörning - skickas inte)")
            return
        self.outport.send(mido.Message("sysex", data=data))

    def _handle_message(self, data):
        """Uppdaterar programnamn, kurvor och aktivt program från ett svar."""
        name = p.parse_program_name(data)
        if name:
            self.program_names[name[0]] = name[1]
        curve = p.parse_velocity_curve(data)
        if curve:
            self.curves[curve["slot"]] = curve
        if data[:4] == p.HEADER_IN and len(data) > 4 and data[4] == p.MSG_PROGRAM_DATA:
            pdata = p.parse_program_data(data)
            if pdata:
                self.active_program = pdata
            else:
                print(f"  (varning: 01-svar med längd {len(data)} kunde inte tolkas, "
                      f"förväntade {p.PROGRAM_DATA_LEN})")

    def _collect(self, duration):
        """Tar emot och tolkar SysEx under `duration` sekunder."""
        end = time.time() + duration
        while time.time() < end:
            msg = self.inport.poll()
            if msg is None:
                time.sleep(0.005)
                continue
            if msg.type == "sysex":
                self._handle_message(list(msg.data))

    # -- config dump -------------------------------------------------------
    def request_config_dump(self, timeout=5.0, raw=False, summary=True):
        """Begär config dump. Slutar när SL88 skickar 'End of Config Dump'
        (10) eller efter `timeout` sekunder utan nya meddelanden. Fyller
        self.program_names, self.curves och self.active_program."""
        for _ in self.inport.iter_pending():
            pass
        # Dump är bara läsning: skickas alltid på riktigt.
        was_dry, self.dry_run = self.dry_run, False
        self._send(p.build_request_config_dump())
        self.dry_run = was_dry

        count = 0
        end = time.time() + timeout
        while time.time() < end:
            msg = self.inport.poll()
            if msg is None:
                time.sleep(0.005)
                continue
            if msg.type != "sysex":
                continue
            data = list(msg.data)
            count += 1
            end = time.time() + timeout
            if raw:
                print(f"[{count:03d}] F0 {p.hexdump(data)} F7")
            self._handle_message(data)
            if p.is_end_of_dump(data):
                break
        if summary:
            self.print_summary(count)
        return count

    def print_summary(self, count):
        print(f"Tog emot {count} SysEx-meddelanden, "
              f"{len(self.program_names)} programnamn, {len(self.curves)} kurvor.")
        for num in sorted(self.program_names):
            print(f"  Program {num + 1:3d}: {self.program_names[num]!r}")
        for slot in sorted(self.curves):
            c = self.curves[slot]
            kind = "användare" if c["type"] == 0x55 else "faktori"
            ok = "OK" if c["checksum_ok"] else "FEL"
            print(f"  Kurva {slot}: {c['name']!r} ({kind}), checksum {ok}")

    # -- program -----------------------------------------------------------
    def recall_program(self, program_number):
        """Väljer program (1..250) med SysEx Recall Program."""
        self._send(p.build_recall_program(program_number))
        if not self.dry_run:
            time.sleep(0.3)

    def store_program(self, program_number):
        """STORE: skriver aktivt program till minnet."""
        self._send(p.build_store_program(program_number))
        if not self.dry_run:
            time.sleep(0.3)

    def read_program(self, program_number, wait=0.4):
        """
        Väljer program (Recall) och returnerar dess tolkade programdata
        (dict från p.parse_program_data) eller None.

        Först lyssnar vi kort om SL88 självmant skickar programdatan efter
        Recall; annars faller vi tillbaka på en config dump, som innehåller
        det aktiva programmets data (01).

        I torrkörning skickas ingen Recall - då finns inget meningsfullt att
        läsa, och vi returnerar None i stället för att visa fel program.
        """
        if self.dry_run:
            print("  (torrkörning - ingen Recall skickas, ingen programdata läses)")
            return None
        for _ in self.inport.iter_pending():
            pass
        self.active_program = None
        self.recall_program(program_number)
        self._collect(wait)
        if self.active_program is None:
            self.request_config_dump(summary=False)
        return self.active_program

    def print_program(self, pdata):
        """Utskrift av ett tolkat 01-svar (används av --read)."""
        if not pdata:
            print("Ingen programdata mottogs.")
            return
        print(f"Programdata: namn {pdata['name']!r}, "
              f"checksum {'OK' if pdata['checksum_ok'] else 'FEL'}")
        for i, z in enumerate(pdata["zones"], start=1):
            rng = f"{p.note_name(z['low_key'])}-{p.note_name(z['high_key'])}"
            print(f"  ZONE {i}: kurva {p.describe_curve_type(z['curve_type'])}, "
                  f"kanal {z['midi_channel']}, port {z['midi_port']}, "
                  f"volym {z['volume']}, omfång {rng}, enabled {z['enabled']}")
            for s in range(1, p.NUM_STICKS + 1):
                xv = z[f"stick_{s}_x"]
                yv = z[f"stick_{s}_y"]
                print(f"    STICK {s}: X={p.describe_stick_value(xv)} ({xv}), "
                      f"Y={p.describe_stick_value(yv)} ({yv})")

    def rename_program(self, program_number, new_name, store=False, verify=False):
        """
        Byter namn på program 1..250: Recall -> Alter Active Program -> (Store).
        Ändringen ligger bara i RAM tills store=True. Ingen checksum skickas.

        verify=True (kräver store och inte torrkörning): läser en ny config dump
        och jämför namnet. Returnerar True/False, eller None om verify ej kördes.
        """
        self.recall_program(program_number)
        self._send(p.build_rename(new_name))
        if store:
            self.store_program(program_number)
        if verify:
            if self.dry_run or not store:
                print("  (--verify kräver --send och --store)")
                return None
            self.request_config_dump(summary=False)
            got = self.program_names.get(program_number - 1)
            ok = got == new_name.rstrip()
            print(f"Verifiering: program {program_number} heter nu {got!r} "
                  f"-> {'OK' if ok else 'AVVIKER'}")
            return ok
        return None

    def poke(self, offset, value, program_number=None):
        """Experiment: skriver ETT Word (value) på `offset` i det aktiva
        programmets dataarray med Alter Active Program."""
        if program_number is not None:
            self.recall_program(program_number)
        self._send(p.build_alter_active_program(offset, [value]))

    def set_zone_field(self, field, zone, value, program_number=None, store=False):
        """
        Skriver ett zonfält (t.ex. 'volume', 'stick_1_x') för ZONE 1..4.

        field: fältnamn ur p.ZONE_FIELDS
        zone:  1..4 (offset = bas + zone - 1)
        value: Word 0..16383
        program_number: välj programmet först om angivet
        store: STORE efteråt (kräver program_number)
        """
        offset = p.zone_offset(field, zone)      # validerar fält och zon
        if not 0 <= value <= 0x3FFF:
            raise ValueError(f"value måste vara 0..16383, fick {value}")
        if store and program_number is None:
            raise ValueError("store kräver program_number")
        if program_number is not None:
            self.recall_program(program_number)
        self._send(p.build_alter_active_program(offset, [value]))
        if store:
            self.store_program(program_number)

    def set_stick(self, stick, axis, choice, zone=1, program_number=None, store=False):
        """
        Sätter EN joystick-axel: STICK 1..3, axis 'x'/'y', val enligt
        p.STICK_CHOICE_HELP (Off, Pitch Bend, Aftertouch, CC 1..CC 127).
        """
        message = p.build_set_stick(stick, axis, choice, zone)  # validerar först
        if store and program_number is None:
            raise ValueError("store kräver program_number")
        if program_number is not None:
            self.recall_program(program_number)
        self._send(message)
        if store:
            self.store_program(program_number)

    def set_stick_pair(self, stick, x_choice, y_choice, zone=1,
                       program_number=None, store=False):
        """
        Sätter BÅDA axlarna för en stick i en zon. Fälten ligger inte
        intilliggande i arrayen (zonerna är interleaved: stick_1_x zon 1 = 180,
        zon 2 = 181 ...), så två separata Alter-meddelanden skickas - ett
        Recall före och ett STORE efter.
        """
        x_msg = p.build_set_stick(stick, "x", x_choice, zone)   # validerar först
        y_msg = p.build_set_stick(stick, "y", y_choice, zone)
        if store and program_number is None:
            raise ValueError("store kräver program_number")
        if program_number is not None:
            self.recall_program(program_number)
        self._send(x_msg)
        self._send(y_msg)
        if store:
            self.store_program(program_number)

    # -- velocity-kurva ----------------------------------------------------
    def set_velocity_curve(self, choice, zone=1, program_number=None, store=False):
        """
        Väljer SL CURVE (soft, medium, hard, memory4..memory9) för en ZONE (1-4)
        i det aktiva programmet. Skriver hela kurvtyp-byten (fast velocity av).
        """
        message = p.build_set_curve(choice, zone)   # validerar innan något skickas
        if store and program_number is None:
            raise ValueError("store kräver program_number")
        if program_number is not None:
            self.recall_program(program_number)
        self._send(message)
        if store:
            self.store_program(program_number)


# --------------------------------------------------------------------------
# Diagnos: finns allt som sl88_gui.py anropar?
# --------------------------------------------------------------------------
def check_api():
    print("Kontrollerar API mot sl88_gui.py …")
    print(f"  sl88_protocol läses från: {p.__file__}")
    print(f"  sl88_editor   läses från: {__file__}")
    ok = True
    for name in GUI_API_PROTOCOL:
        has = hasattr(p, name)
        ok = ok and has
        print(f"  [{'OK' if has else 'SAKNAS'}] sl88_protocol.{name}")
    for name in GUI_API_EDITOR:
        has = callable(getattr(SL88Editor, name, None))
        ok = ok and has
        print(f"  [{'OK' if has else 'SAKNAS'}] SL88Editor.{name}()")
    for attr in ("program_names", "curves", "active_program", "dry_run"):
        has = attr in SL88Editor.__init__.__code__.co_names
        ok = ok and has
        print(f"  [{'OK' if has else 'SAKNAS'}] SL88Editor.{attr} (instansattribut)")
    try:
        p._selftest()
    except Exception as exc:
        ok = False
        print(f"  [FEL] protokollens självtest: {type(exc).__name__}: {exc}")
    print("Resultat:", "allt finns - GUI:t ska kunna starta."
          if ok else "NÅGOT SAKNAS.")
    return 0 if ok else 1


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Minimal SL88-editor")
    ap.add_argument("--list", action="store_true", help="lista MIDI-portar och avsluta")
    ap.add_argument("--check", action="store_true",
                    help="verifiera GUI:ts API + protokollsjälvtest (ingen hårdvara)")
    ap.add_argument("--port", help="del av SL88:ans portnamn")
    ap.add_argument("--dump", action="store_true", help="läs program och kurvor")
    ap.add_argument("--raw", action="store_true", help="visa råa dump-meddelanden")
    ap.add_argument("--read", type=int, metavar="NR",
                    help="Recall + skriv ut programdata för NR (kräver --send)")
    ap.add_argument("--rename", nargs=2, metavar=("NR", "NAMN"),
                    help=f"byt namn på program 1-{p.NUM_PROGRAMS} (max {p.NAME_LENGTH} tecken)")
    ap.add_argument("--curve", metavar="KURVA", help=p.CURVE_CHOICE_HELP)
    ap.add_argument("--stick", nargs=3, metavar=("NR", "XVAL", "YVAL"),
                    help="sätt STICK 1-3 X/Y i --zone, t.ex. "
                         "--stick 1 'Pitch Bend' 'CC 1 (Modulation)'")
    ap.add_argument("--field", nargs=3, metavar=("FÄLT", "ZON", "VÄRDE"),
                    help="skriv ett zonfält ur ZONE_FIELDS, t.ex. --field volume 1 100")
    ap.add_argument("--poke", nargs=2, type=int, metavar=("OFFSET", "VÄRDE"),
                    help="experiment: skriv ett Word på en offset i aktivt program")
    ap.add_argument("--offset-mode", choices=["word", "byte"], default="word",
                    help="offsetkodning i Alter Active Program (standard word)")
    ap.add_argument("--zone", type=int, default=1, choices=[1, 2, 3, 4])
    ap.add_argument("--program", type=int, metavar="NR", help="välj program före ändring")
    ap.add_argument("--store", action="store_true", help="STORE efter ändring")
    ap.add_argument("--verify", action="store_true", help="läs tillbaka namnet efter --store")
    ap.add_argument("--send", action="store_true", help="skicka på riktigt (annars torrkörning)")
    args = ap.parse_args()

    if args.check:
        return check_api()

    if args.list:
        print("Ingångar:", *mido.get_input_names(), sep="\n")
        print("Utgångar:", *mido.get_output_names(), sep="\n")
        return 0

    if not args.port:
        ap.error("--port krävs (kör --list först)")

    p.OFFSET_MODE = args.offset_mode
    ed = SL88Editor(args.port, dry_run=not args.send)
    try:
        if args.dump:
            ed.request_config_dump(raw=args.raw)
        if args.read:
            ed.print_program(ed.read_program(args.read))
        if args.rename:
            ed.rename_program(int(args.rename[0]), args.rename[1],
                              store=args.store, verify=args.verify)
        if args.poke:
            ed.poke(args.poke[0], args.poke[1], program_number=args.program)
        if args.field:
            ed.set_zone_field(args.field[0], int(args.field[1]), int(args.field[2]),
                              program_number=args.program, store=args.store)
        if args.curve:
            ed.set_velocity_curve(args.curve, zone=args.zone,
                                  program_number=args.program, store=args.store)
        if args.stick:
            ed.set_stick_pair(int(args.stick[0]), args.stick[1], args.stick[2],
                              zone=args.zone, program_number=args.program,
                              store=args.store)
    finally:
        ed.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())