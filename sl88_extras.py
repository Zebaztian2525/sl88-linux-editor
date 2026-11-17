#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sl88_extras.py - Hjälpmodul för SL88 GUI:t (pedaler, key-balance, grupper).
Kräver sl88_protocol.py i samma mapp.

Alla offsets och meddelandestrukturer som inte är direkt verifierade mot en
verklig config-dump är markerade som ANTAGANDE. De följer dock SL88:ans etablerade
mönster (interleaved zoner, Word-kodning, CC-offsets).
"""
import re
import sl88_protocol as p

# --------------------------------------------------------------------------
# Konstanter
# --------------------------------------------------------------------------
GROUP_MSG = 0x03
# ANTAGANDE: Längd på gruppsvarets kropp (efter ID-byten).
# Gissning: 1 (gruppnr) + 28 (14 Words namn) + 500 (250 Words programlista) = 529
GROUP_BODY_LEN = 529

NUM_PEDALS = 4
KEY_COUNT = 88
KB_MAX_PCT = 30
KEY_FIRST_NOTE = 21  # A0

# --------------------------------------------------------------------------
# Pedaler
# --------------------------------------------------------------------------
# ANTAGANDE: Pedalvärden följer samma mönster som STICKS, men med egna specialvärden.
# 0 = Off, 1 = Damper, 2 = Sostenuto, 3 = Soft Pedal.
# CC N lagras som N + 4 (CC 1 = 5, CC 127 = 130).
PEDAL_SPECIAL = {0: "Off", 1: "Damper", 2: "Sostenuto", 3: "Soft Pedal"}
PEDAL_CC_OFFSET = 4

COMMON_PEDAL_CC = {
    1: "Modulation", 7: "Volume", 10: "Pan", 11: "Expression",
    64: "Damper", 66: "Sostenuto", 67: "Soft Pedal",
    71: "Resonance", 74: "Cutoff"
}

def _pedal_cc_label(cc_num):
    if cc_num in COMMON_PEDAL_CC:
        return f"CC {cc_num} ({COMMON_PEDAL_CC[cc_num]})"
    return f"CC {cc_num}"

PEDAL_CHOICES = ["Off", "Damper", "Sostenuto", "Soft Pedal"] + \
                [_pedal_cc_label(n) for n in range(1, 128)]

PEDAL_ALIASES = {
    "off": 0, "damper": 1, "sustain": 1,
    "sostenuto": 2, "soft pedal": 3, "soft": 3,
    "modulation": 1, "volume": 7, "pan": 10, "expression": 11,
    "resonance": 71, "cutoff": 74
}

def pedal_offset(pedal_num, zone):
    """
    ANTAGANDE: Offset för pedaler i programdatan (208-223).
    Interleaved per zon, precis som övriga zonfält.
    Pedal 1 Zon 1 = 208, Pedal 4 Zon 4 = 223.
    """
    if not 1 <= pedal_num <= NUM_PEDALS:
        raise ValueError("pedal_num måste vara 1..4")
    if not 1 <= zone <= 4:
        raise ValueError("zone måste vara 1..4")
    return 208 + (pedal_num - 1) * 4 + (zone - 1)

def pedal_value(choice):
    """Val (t.ex. 'Damper', 'CC 74') -> protokollvärde."""
    c = str(choice).strip().lower()
    if c in PEDAL_ALIASES:
        v = PEDAL_ALIASES[c]
        return v if v <= 3 else v + PEDAL_CC_OFFSET
    
    m = re.match(r"cc\s*(\d{1,3})", c)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 127:
            return n + PEDAL_CC_OFFSET
        raise ValueError(f"CC-nummer måste vara 1..127, fick {n}")
    
    for name, cc in COMMON_PEDAL_CC.items():
        if c == name.lower():
            return cc + PEDAL_CC_OFFSET
            
    raise ValueError(f"Okänt pedalval '{choice}'. Giltiga: Off, Damper, Sostenuto, Soft Pedal, CC 1..CC 127")

def pedal_choice_from_value(value):
    """Protokollvärde -> val för dropdown, eller None."""
    if value in PEDAL_SPECIAL:
        return PEDAL_SPECIAL[value]
    n = value - PEDAL_CC_OFFSET
    if 1 <= n <= 127:
        return _pedal_cc_label(n)
    return None

def describe_pedal_value(value):
    """Protokollvärde -> läsbar text även för okända värden."""
    return pedal_choice_from_value(value) or f"okänd ({value})"

# --------------------------------------------------------------------------
# Key-balance (SL TOUCH)
# --------------------------------------------------------------------------
def is_black_key(index):
    """True om tangenten (index 0..87) är svart."""
    note = KEY_FIRST_NOTE + index
    return (note % 12) in (1, 3, 6, 8, 10)

def key_label(index):
    """Index (0..87) -> notnamn (t.ex. 'A0', 'C#4')."""
    note = KEY_FIRST_NOTE + index
    return p.note_name(note)

# --------------------------------------------------------------------------
# Grupper (Organize)
# --------------------------------------------------------------------------
def parse_group(data):
    """
    ANTAGANDE: Tolkar ett 03-svar (grupp).
    Returnerar dict med 'group', 'name', 'programs' eller None om längden inte stämmer.
    """
    if not p.is_reply(data) or len(data) < 5 or data[4] != GROUP_MSG:
        return None
    body = data[5:]
    if len(body) != GROUP_BODY_LEN:
        return None
    
    # ANTAGANDE: Struktur för gruppsvar
    group_num = body[0]
    name_words = p.decode_words(body[1:29])
    name = "".join(chr(w) if 32 <= w < 127 else "?" for w in name_words).split("\x00")[0].rstrip()
    prog_words = p.decode_words(body[29:529])
    programs = [w for w in prog_words if 1 <= w <= 250]
    
    return {
        "group": group_num,
        "name": name,
        "programs": programs
    }

def new_project():
    """Returnerar en tom projektstruktur (används av GUI:t)."""
    return {
        "programs": [],
        "groups": {i: {"name": "", "programs": []} for i in range(8)}
    }

def build_group(group_num, name, programs):
    """
    ANTAGANDE: Bygger ett 03-meddelande för att sätta en grupp.
    """
    if not 0 <= group_num <= 7:
        raise ValueError("group_num måste vara 0..7")
    name_codes = [ord(c) for c in name[:14]] + [0] * (14 - len(name[:14]))
    name_words = p.encode_words(name_codes)
    prog_list = list(programs)[:250]
    prog_list += [0] * (250 - len(prog_list))
    prog_words = p.encode_words(prog_list)
    return [GROUP_MSG, group_num] + name_words + prog_words

def build_group_order(order):
    """
    ANTAGANDE: Bygger meddelande för att ändra gruppordning.
    order: lista med gruppnummer 0..7
    """
    if len(order) != 8 or sorted(order) != list(range(8)):
        raise ValueError("order måste vara en permutation av 0..7")
    return [0x04] + list(order)  # 0x04 är ett gissat ID för gruppordning