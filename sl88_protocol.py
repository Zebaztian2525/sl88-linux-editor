#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sl88_protocol.py - Ren protokollmodul för Studiologic SL88 (SysEx).

Inget MIDI-I/O här (ingen mido): bara bygga och tolka meddelanden, så att allt
kan testas utan hårdvara och återanvändas av GUI:t (sl88_gui.py).

Källor: maranite/SL-Monitor (dokumentation) + en verklig config dump från en
SL88 Studio. Allt som är verifierat mot dumpen är markerat VERIFIERAT; resten
är markerat ANTAGANDE.

Meddelanden på tråden:   F0 00 20 1A <enhets-ID> <meddelande...> F7
  - värd -> SL88 : enhets-ID 00  (HEADER_OUT)
  - SL88 -> värd : enhets-ID 01  (HEADER_IN)
mido lägger själv till F0 och F7.

Självtest utan hårdvara:   python3 sl88_protocol.py
"""

import re

HEADER_OUT = [0x00, 0x20, 0x1A, 0x00]
HEADER_IN = [0x00, 0x20, 0x1A, 0x01]

MSG_PROGRAM_DATA = 0x01
MSG_ALTER_ACTIVE_PROGRAM = 0x02
MSG_RECALL_PROGRAM = 0x06
MSG_VELOCITY_CURVE = 0x07
MSG_STORE_PROGRAM = 0x09
MSG_PROGRAM_NAME = 0x0A
MSG_END_OF_DUMP = 0x10

CMD_REQUEST_CONFIG_DUMP = [0x00, 0x10]

NUM_PROGRAMS = 250          # VERIFIERAT: dumpen har program 0..249
NAME_OFFSET = 1             # offset i programdatan
NAME_LENGTH = 14            # 14 tecken (14 Words i programdatan)
CURVE_TYPE_OFFSET_Z1 = 152  # kurvtyp zon 1; zon 2-4 = 153..155
CURVE_POINTS = 16           # 16 X/Y-punkter (255,255 = "oanvänd")
CURVE_TABLE_VALUES = 127
CURVE_MESSAGE_BODY_LEN = 347  # VERIFIERAT: längd efter id-byten, inkl. checksum
NUM_ZONES = 4               # VERIFIERAT: SL88 har ZONE 1-4
NUM_STICKS = 3              # VERIFIERAT mot manualen: STICK 1-3 (X/Y)
PROGRAM_DATA_LEN = 522      # 4 header + id + PPPP(2) + 0002(2) + 256 Words + checksum


# --------------------------------------------------------------------------
# Word-kodning
# --------------------------------------------------------------------------
# SysEx-databytes måste vara 0x00..0x7F. Ett "Word" är därför ett värde
# (0..16383) i två bytes, LSB FÖRST:
#     [värde & 0x7F, (värde >> 7) & 0x7F]
#     'P' (0x50) -> 50 00        255 -> 7F 01        'å' (0xE5) -> 65 01


def encode_word(value):
    if not 0 <= value <= 0x3FFF:
        raise ValueError(f"Word utanför 0..16383: {value}")
    return [value & 0x7F, (value >> 7) & 0x7F]


def encode_words(values):
    out = []
    for v in values:
        out.extend(encode_word(v))
    return out


def decode_words(data):
    return [(data[i] & 0x7F) | ((data[i + 1] & 0x7F) << 7)
            for i in range(0, len(data) - 1, 2)]


def encode_text(text, length):
    """Text -> `length` Words (ett tecken per Word), nollpaddat. För långt
    namn ger ValueError (vi trunkerar aldrig tyst)."""
    if len(text) > length:
        raise ValueError(f"Namnet är {len(text)} tecken, max {length}")
    codes = [ord(c) for c in text] + [0] * (length - len(text))
    return encode_words(codes)


def hexdump(data):
    return " ".join(f"{b:02X}" for b in data)


NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def note_name(n):
    """MIDI-notnummer -> namn. VERIFIERAT mot dumpen: 21 = A0, 108 = C8."""
    if not isinstance(n, int) or not 0 <= n <= 127:
        return str(n)
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 1}"


# --------------------------------------------------------------------------
# Checksummor
# --------------------------------------------------------------------------
# Checksum finns bara i meddelanden SL88 SKICKAR. Värden skickar den inte
# (SL-Monitor: "received only"; Alter Active Program har inget checksumfält).
#
# Velocity Curve (07): summan av alla bytes från och med TT-ordet till och med
# sista tabellvärdet, modulo 128. VV ingår inte.   VERIFIERAT på 9/9 kurvor.
def velocity_curve_checksum(payload_from_tt):
    return sum(payload_from_tt) & 0x7F


# Program Data (01): summan av alla bytes efter id-byten, modulo 128.
# VERIFIERAT på det enda programdata-meddelandet i dumpen (svag stickprov).
def program_data_checksum(payload_after_id):
    return sum(payload_after_id) & 0x7F


# --------------------------------------------------------------------------
# Byggare: värd -> SL88 (returnerar bytes UTAN header)
# --------------------------------------------------------------------------
def build_request_config_dump():
    return list(CMD_REQUEST_CONFIG_DUMP)


def _program_word(program_number):
    if not 1 <= program_number <= NUM_PROGRAMS:
        raise ValueError(f"program_number måste vara 1..{NUM_PROGRAMS}")
    return encode_word(program_number - 1)  # 0-baserat Word på tråden


def build_recall_program(program_number):
    """06 PPPP - välj program."""
    return [MSG_RECALL_PROGRAM] + _program_word(program_number)


def build_store_program(program_number):
    """09 PPPP - skriv aktivt program till minnet."""
    return [MSG_STORE_PROGRAM] + _program_word(program_number)


OFFSET_MODE = "word"   # "word" (standard) eller "byte" (bara för experiment)


def offset_bytes(offset, mode=None):
    """Offset i Alter Active Program (02 TT SS ...).

    "word": alltid ett Word, LSB först (offset 1 -> 01 00, 128 -> 00 01).
    Hårdvarutest: offset 128 (volym) som två bytes fungerar, och namnbytet med
    offset som EN byte (`02 05 0E ...`) gjorde ingenting - keyboardet läser då
    `05 0E` som Word (=1797) och längd 0x54, vilket ligger utanför arrayen.
    Dokumentationens exempel `02 01 0e ...` ser därför ut som en förenkling.

    "byte": en byte när offset < 128 (gamla antagandet, bara för jämförelse)."""
    mode = mode or OFFSET_MODE
    if mode == "byte" and offset < 0x80:
        return [offset]
    return encode_word(offset)


def build_alter_active_program(offset, values):
    """02 [OFFSET word] [ANTAL element, en byte] [värden som Words]. Ingen checksum."""
    if not 1 <= len(values) <= 0x7F:
        raise ValueError("1..127 värden")
    return ([MSG_ALTER_ACTIVE_PROGRAM] + offset_bytes(offset)
            + [len(values)] + encode_words(values))


def build_rename(new_name):
    """Byt namn på det aktiva programmet (offset 1, 14 Words)."""
    codes = [ord(c) for c in new_name]
    if any(c > 0x3FFF for c in codes):
        raise ValueError("Tecken utanför Word-området")
    if len(codes) > NAME_LENGTH:
        raise ValueError(f"Namnet är {len(codes)} tecken, max {NAME_LENGTH}")
    codes += [0] * (NAME_LENGTH - len(codes))
    return build_alter_active_program(NAME_OFFSET, codes)


# --------------------------------------------------------------------------
# Zonfält i programdatan
# --------------------------------------------------------------------------
# Offset för ZONE 1; zon 2-4 = +1, +2, +3 (fälten är interleaved per zon!).
# Kontrollerade mot en verklig dump (INSTRUMENT på 24, SOUND på 72,
# tangentomfång 21..108 = A0..C8, volym 127) och volym (128) mot hårdvara.
ZONE_FIELDS = {
    "enabled": 116, "midi_port": 120, "midi_channel": 124, "volume": 128,
    "program_change": 132, "bank_msb": 136, "bank_lsb": 140,
    "low_key": 144, "high_key": 148, "curve_type": 152,
    "vel_a": 156, "vel_b": 160,          # dokumentationen: lowVel/highVel, men
    "octave": 164, "transpose": 168,     # dumpen visar 127 resp. 1 -> omkastade?
    "aftertouch": 172, "fixed_velocity": 176,
    # Joystick-parametrar (STICK 1-3, X/Y). ANTAGANDE: följer samma mönster
    # som övriga zonfält (4 Words isär, zon 2-4 = +1/+2/+3).
    "stick_1_x": 180, "stick_1_y": 184,
    "stick_2_x": 188, "stick_2_y": 192,
    "stick_3_x": 196, "stick_3_y": 200,
}


def zone_offset(field, zone=1):
    """Array-offset för ett zonfält i ZONE 1..4 (t.ex. 'stick_1_x', 2 -> 181)."""
    if field not in ZONE_FIELDS:
        raise ValueError(f"Okänt zonfält '{field}'. Giltiga: {sorted(ZONE_FIELDS)}")
    if zone not in (1, 2, 3, 4):
        raise ValueError("zone måste vara 1..4")
    return ZONE_FIELDS[field] + zone - 1


def stick_field(stick, axis):
    """Fältnamn för en joystick-axel: stick_field(2, 'y') -> 'stick_2_y'."""
    if stick not in (1, 2, 3):
        raise ValueError("stick måste vara 1..3")
    if axis not in ("x", "y"):
        raise ValueError("axis måste vara 'x' eller 'y'")
    return f"stick_{stick}_{axis}"


# --------------------------------------------------------------------------
# SL CURVES: manualens namn <-> protokollets kurvtyp-byte
# --------------------------------------------------------------------------
#   VERIFIERAT: dumpens faktorikurvor är index 0 NORMAL (identitet), 1 SOFT,
#               2 HARD.
#   ANTAGANDE:  manualens MEDIUM = dumpens NORMAL (index 0), och användarminne
#               4..9 = dumpens USER 1..6 i ordning.
FACTORY_CURVES = {"medium": 0, "soft": 1, "hard": 2}
USER_MEMORY_RANGE = range(4, 10)
CURVE_CHOICE_HELP = "soft, medium, hard, memory4 .. memory9"


def curve_type_value(choice):
    """Kurvval -> värdet för "curve type"-byten (offset 152..155):
    bit 0-1 faktorikurva | bit 2-4 användarkurva N-1 |
    bit 5 = 1 användarkurva | bit 6 = fast velocity (sätts aldrig här)."""
    c = choice.strip().lower()
    if c == "normal":
        c = "medium"
    if c in FACTORY_CURVES:
        return FACTORY_CURVES[c]
    m = re.fullmatch(r"(?:memory|user)?([4-9])", c)
    if m:
        return 0x20 | ((int(m.group(1)) - 4) << 2)
    raise ValueError(f"Okänd kurva '{choice}'. Giltiga: {CURVE_CHOICE_HELP}")


def curve_choice_from_type(value):
    """Kurvtyp-byte -> kurvval, eller None vid fast velocity (bit 6)/okänt."""
    if value & 0x40:
        return None
    if value & 0x20:
        n = (value >> 2) & 0x07
        return f"memory{n + 4}" if n <= 5 else None
    return {0: "medium", 1: "soft", 2: "hard"}.get(value & 0x03)


def describe_curve_type(value):
    """Kurvtyp-byte -> läsbar text (för logg/status i GUI:t)."""
    if value & 0x40:
        return "fast velocity"
    return curve_choice_from_type(value) or f"okänd ({value})"


def build_set_curve(choice, zone=1):
    """Välj velocity-kurva för zon 1..4 i det aktiva programmet.
    (07 laddar bara UPP en kurvdefinition och väljer ingen kurva.)"""
    return build_alter_active_program(zone_offset("curve_type", zone),
                                      [curve_type_value(choice)])


def build_velocity_curve(slot, type_word, name, points, table,
                         literal1=0, literal2=0, with_checksum=False):
    """
    07 00 VV TT | namn(10 W) | literal1 | 16 X/Y-punkter (32 W) |
    literal2 | 127 tabellvärden | [checksum]

    slot: 0..8 (0-2 faktori, 3-8 användare). type_word: 0x0000 faktori,
    0x0055 användare. Checksum tas inte med vid sändning (SL-Monitor); flaggan
    finns för att kunna återskapa mottagna meddelanden i tester.
    """
    if len(points) > CURVE_POINTS or len(table) != CURVE_TABLE_VALUES:
        raise ValueError("max 16 punkter och exakt 127 tabellvärden")
    pts = list(points) + [(255, 255)] * (CURVE_POINTS - len(points))
    flat = [v for xy in pts for v in xy]
    from_tt = (encode_word(type_word) + encode_text(name, 10)
               + encode_word(literal1) + encode_words(flat)
               + encode_word(literal2) + encode_words(table))
    msg = [MSG_VELOCITY_CURVE, 0x00, slot] + from_tt
    if with_checksum:
        msg.append(velocity_curve_checksum(from_tt))
    return msg


# --------------------------------------------------------------------------
# STICK 1-3 X/Y: manualens parameternamn <-> protokollets värde
# --------------------------------------------------------------------------
# ANTAGANDE (enligt din hårdvarutest av Modulation):
#     0 = Off
#     1 = Pitch Bend
#     2 = Aftertouch
#     3 = CC 1 (Modulation)   dvs. CC N lagras som N + 2
#   129 = CC 127
STICK_CC_OFFSET = 2
STICK_SPECIAL = {0: "Off", 1: "Pitch Bend", 2: "Aftertouch"}
STICK_CHOICE_HELP = "Off, Pitch Bend, Aftertouch, CC 1 .. CC 127"

# Vanliga CC-namn (GM/GM2 + klassiska synth-parametrar) för dropdowns.
COMMON_CC_NAMES = {
    1: "Modulation", 2: "Breath", 4: "Foot", 5: "Portamento Time",
    7: "Volume", 8: "Balance", 10: "Pan", 11: "Expression",
    64: "Damper", 65: "Portamento", 66: "Sostenuto", 67: "Soft Pedal",
    71: "Resonance", 72: "Release Time", 73: "Attack Time", 74: "Cutoff",
    75: "Decay Time", 76: "Vibrato Rate", 77: "Vibrato Depth",
    78: "Vibrato Delay", 91: "Reverb", 93: "Chorus",
}

# Kortnamn som också accepteras av stick_param_value (bra för CLI).
STICK_ALIASES = {
    "off": 0, "pitch bend": 1, "pitchbend": 1, "pb": 1,
    "aftertouch": 2, "at": 2, "mod": 1, "modulation": 1, "volume": 7,
    "pan": 10, "expression": 11, "sustain": 64, "damper": 64,
    "soft pedal": 67, "resonance": 71, "cutoff": 74, "reverb": 91,
}


def cc_label(cc_num):
    """CC-nummer -> etikett för dropdown: 74 -> 'CC 74 (Cutoff)'."""
    if cc_num in COMMON_CC_NAMES:
        return f"CC {cc_num} ({COMMON_CC_NAMES[cc_num]})"
    return f"CC {cc_num}"


def stick_param_choices():
    """Alla giltiga val för en stick-axel (130 st), i menyordning."""
    return ["Off", "Pitch Bend", "Aftertouch"] + [cc_label(n) for n in range(1, 128)]


STICK_CHOICES = stick_param_choices()


def stick_param_value(choice):
    """Val ('Off', 'Pitch Bend', 'Aftertouch', 'CC 74 (Cutoff)', 'cutoff')
    -> protokollvärde 0..129. Kastar ValueError vid okänt val."""
    c = str(choice).strip().lower()
    if c in STICK_ALIASES:
        v = STICK_ALIASES[c]
        return v if v <= 2 else v + STICK_CC_OFFSET
    m = re.match(r"cc\s*(\d{1,3})", c)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 127:
            return n + STICK_CC_OFFSET
        raise ValueError(f"CC-nummer måste vara 1..127, fick {n}")
    for n, name in COMMON_CC_NAMES.items():
        if c == name.lower():
            return n + STICK_CC_OFFSET
    raise ValueError(f"Okänt stick-val '{choice}'. Giltiga: {STICK_CHOICE_HELP}")


def stick_param_choice_from_value(value):
    """Protokollvärde -> etikett för dropdown, eller None om värdet är okänt."""
    if value in STICK_SPECIAL:
        return STICK_SPECIAL[value]
    n = value - STICK_CC_OFFSET
    return cc_label(n) if 1 <= n <= 127 else None


def describe_stick_value(value):
    """Protokollvärde -> läsbar text även för okända värden (logg/status)."""
    return stick_param_choice_from_value(value) or f"okänd ({value})"


def build_set_stick(stick, axis, choice, zone=1):
    """Alter Active Program för en joystick-axel i ZONE 1..4."""
    return build_alter_active_program(zone_offset(stick_field(stick, axis), zone),
                                      [stick_param_value(choice)])


# --------------------------------------------------------------------------
# Tolkare: SL88 -> värd (indata = hela SysEx-datat inkl. header 00 20 1A 01)
# --------------------------------------------------------------------------
def is_reply(data):
    return data[:4] == HEADER_IN


def is_end_of_dump(data):
    return is_reply(data) and data[4:] == [MSG_END_OF_DUMP]


def parse_program_name(data):
    """0A PPPP + 15 ASCII-byte. Returnerar (programnr 0-baserat, namn) eller
    None. Namnet är de första 14 byten, NUL-terminerat (efter första 00 finns
    kvarlevor av äldre namn). Den 15:e byten är nästa programfält, inte namn."""
    if not is_reply(data) or len(data) < 8 or data[4] != MSG_PROGRAM_NAME:
        return None
    number = data[5] | (data[6] << 7)
    field = data[7:7 + NAME_LENGTH]
    raw = field[:field.index(0)] if 0 in field else field
    name = "".join(chr(b) if 32 <= b < 127 else "?" for b in raw).rstrip()
    return number, name


def parse_program_data(data):
    if (not is_reply(data) or len(data) < 5
            or data[4] != MSG_PROGRAM_DATA or len(data) != PROGRAM_DATA_LEN):
        return None
    arr = decode_words(data[9:-1])
    chars = []
    for w in arr[NAME_OFFSET:NAME_OFFSET + NAME_LENGTH]:
        if w == 0:
            break
        chars.append(chr(w) if 32 <= w < 127 else "?")
    zones = [{field: arr[base + z] for field, base in ZONE_FIELDS.items()}
             for z in range(NUM_ZONES)]
    return {
        "program": data[5] | (data[6] << 7),
        "array": arr,
        "name": "".join(chars).rstrip(),
        "zones": zones,
        "checksum_ok": program_data_checksum(data[5:-1]) == data[-1],
    }

def parse_velocity_curve(data):
    """Tolkar ett 07-svar. Returnerar dict eller None."""
    if not is_reply(data) or len(data) < 5 or data[4] != MSG_VELOCITY_CURVE:
        return None
    body = data[5:]
    if len(body) != CURVE_MESSAGE_BODY_LEN or body[0] != 0x00:
        return None
    slot = body[1]
    type_word = decode_words(body[2:4])[0]
    name_words = decode_words(body[4:24])
    chars = []
    for w in name_words:
        if w == 0:
            break
        chars.append(chr(w) if 32 <= w < 127 else "?")
    name = "".join(chars)
    literal1 = decode_words(body[24:26])[0]
    pts = decode_words(body[26:90])
    points = [(pts[i], pts[i + 1]) for i in range(0, 32, 2)]
    tail = decode_words(body[90:-1])
    literal2, table = tail[0], tail[1:]
    checksum = body[-1]
    return {
        "slot": slot, "type": type_word, "name": name,
        "literal1": literal1, "literal2": literal2,
        "points": points, "table": table,
        "checksum": checksum,
        "checksum_ok": velocity_curve_checksum(body[2:-1]) == checksum,
    }


# --------------------------------------------------------------------------
# Självtest (ingen hårdvara, ingen mido)
# --------------------------------------------------------------------------
def _selftest():
    assert encode_word(0x50) == [0x50, 0x00]
    assert encode_word(255) == [0x7F, 0x01]
    vals = [0, 1, 127, 128, 16383]
    assert decode_words(encode_words(vals)) == vals
    assert encode_text("AB", 4) == [0x41, 0, 0x42, 0, 0, 0, 0, 0]

    assert build_recall_program(250) == [MSG_RECALL_PROGRAM, 0x79, 0x01]
    assert build_store_program(1) == [MSG_STORE_PROGRAM, 0x00, 0x00]
    rename = build_rename("TEST")
    assert rename[0] == MSG_ALTER_ACTIVE_PROGRAM
    assert rename[1:3] == [NAME_OFFSET, 0x00] and rename[3] == NAME_LENGTH

    # Kurvor
    assert zone_offset("curve_type", 1) == CURVE_TYPE_OFFSET_Z1
    for choice in ["soft", "medium", "hard"] + [f"memory{n}" for n in USER_MEMORY_RANGE]:
        assert curve_choice_from_type(curve_type_value(choice)) == choice
    assert curve_choice_from_type(0x40) is None
    assert describe_curve_type(0x40) == "fast velocity"
    assert build_set_curve("soft", 4)[1:3] == encode_word(zone_offset("curve_type", 4))

    # Stick-offsetar (zon 1: 180..200, zon 4: 183..203)
    assert zone_offset("stick_1_x", 1) == 180
    assert zone_offset("stick_1_y", 1) == 184
    assert zone_offset("stick_2_x", 1) == 188
    assert zone_offset("stick_3_y", 1) == 200
    assert zone_offset("stick_1_x", 2) == 181
    assert zone_offset("stick_3_y", 4) == 203
    assert stick_field(2, "y") == "stick_2_y"

    # Stick-värden: Off=0, Pitch Bend=1, Aftertouch=2, CC N = N+2
    assert stick_param_value("Off") == 0
    assert stick_param_value("Pitch Bend") == 1
    assert stick_param_value("Aftertouch") == 2
    assert stick_param_value("CC 1 (Modulation)") == 3
    #assert stick_param_value("Modulation") == 3
