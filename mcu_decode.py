"""
Mackie Control Universal (MCU) MIDI message decoder.
Decodes note numbers to button names and Mackie SysEx (F0 00 00 66 ...).
"""

# MCU note number -> human-readable name (from Control Surface / common MCU maps)
# 0x59-0x60 are the "channel select" buttons many DAWs use
MCU_NOTE_NAMES = {
    0x00: "REC_RDY_1", 0x01: "REC_RDY_2", 0x02: "REC_RDY_3", 0x03: "REC_RDY_4",
    0x04: "REC_RDY_5", 0x05: "REC_RDY_6", 0x06: "REC_RDY_7", 0x07: "REC_RDY_8",
    0x08: "SOLO_1", 0x09: "SOLO_2", 0x0A: "SOLO_3", 0x0B: "SOLO_4",
    0x0C: "SOLO_5", 0x0D: "SOLO_6", 0x0E: "SOLO_7", 0x0F: "SOLO_8",
    0x10: "MUTE_1", 0x11: "MUTE_2", 0x12: "MUTE_3", 0x13: "MUTE_4",
    0x14: "MUTE_5", 0x15: "MUTE_6", 0x16: "MUTE_7", 0x17: "MUTE_8",
    0x18: "SELECT_1", 0x19: "SELECT_2", 0x1A: "SELECT_3", 0x1B: "SELECT_4",
    0x1C: "SELECT_5", 0x1D: "SELECT_6", 0x1E: "SELECT_7", 0x1F: "SELECT_8",
    0x20: "V_POT_SELECT_1", 0x21: "V_POT_SELECT_2", 0x22: "V_POT_SELECT_3",
    0x23: "V_POT_SELECT_4", 0x24: "V_POT_SELECT_5", 0x25: "V_POT_SELECT_6",
    0x26: "V_POT_SELECT_7", 0x27: "V_POT_SELECT_8",
    0x28: "ASSIGN_TRACK", 0x29: "ASSIGN_SEND", 0x2A: "ASSIGN_PAN",
    0x2B: "ASSIGN_PLUGIN", 0x2C: "ASSIGN_EQ", 0x2D: "ASSIGN_INSTR",
    0x2E: "BANK_LEFT", 0x2F: "BANK_RIGHT",
    0x30: "CHANNEL_LEFT", 0x31: "CHANNEL_RIGHT", 0x32: "FLIP",
    0x33: "GLOBAL_VIEW", 0x34: "NAME_VALUE", 0x35: "SMPTE_BEATS",
    0x36: "F1", 0x37: "F2", 0x38: "F3", 0x39: "F4",
    0x3A: "F5", 0x3B: "F6", 0x3C: "F7", 0x3D: "F8",
    0x3E: "VIEW_MIDI", 0x3F: "VIEW_INPUTS", 0x40: "VIEW_AUDIO",
    0x41: "VIEW_INSTR", 0x42: "VIEW_AUX", 0x43: "VIEW_BUSSES",
    0x44: "VIEW_MASTER", 0x45: "VIEW_SOLO",
    # Common DAW mapping: 0x59–0x60 = channel select 1–8 (what you see as note 89)
    0x59: "Ch Select 1", 0x5A: "Ch Select 2", 0x5B: "Ch Select 3",
    0x5C: "Ch Select 4", 0x5D: "Ch Select 5", 0x5E: "Ch Select 6",
    0x5F: "Ch Select 7", 0x60: "Ch Select 8",
}

# Mackie SysEx: F0 00 00 66 [model] [cmd] ...
MCU_MANUFACTURER = (0x00, 0x00, 0x66)
# Model: 0x14 = MCU, 0x10/0x11 = Logic Control
MCU_SYSEX_CMD_NAMES = {
    0x00: "Version request / Ping",
    0x01: "Version response",
    0x0C: "Fader position (7-bit)",
    0x0D: "Fader position (14-bit?)",
    0x12: "LCD line 1",
    0x13: "LCD line 2",
    0x14: "Time display",
    0x20: "LED / meter",
}


def decode_mcu_note(note: int) -> str:
    """Return MCU button name for a note number, or None."""
    return MCU_NOTE_NAMES.get(note)


def decode_sysex(data: bytes) -> str:
    """
    Decode Mackie Control SysEx.
    data: full SysEx including F0 and F7, or raw payload (mido gives payload without F0/F7).
    Format: F0 00 00 66 [model] [cmd] [payload...] F7
    """
    if data[0:1] != b"\xF0":
        data = b"\xF0" + data
    if data[-1:] != b"\xF7":
        data = data + b"\xF7"
    if len(data) < 7:
        return "SysEx (too short)"
    if tuple(data[1:4]) != MCU_MANUFACTURER:
        return "SysEx (other manufacturer)"
    model = data[4]
    cmd = data[5]
    model_name = "MCU" if model == 0x14 else ("Logic" if model in (0x10, 0x11) else f"Model_{model:02X}")
    cmd_name = MCU_SYSEX_CMD_NAMES.get(cmd, f"Cmd_{cmd:02X}")
    payload = data[6:-1]
    if cmd == 0x00 and len(payload) >= 1 and payload[0] == 0x00:
        return f"Mackie {model_name}: Ping/Heartbeat"
    if cmd in (0x12, 0x13):
        try:
            text = payload[1:].decode("ascii", errors="replace").rstrip("\x00 ")
            line = "Line1" if cmd == 0x12 else "Line2"
            return f"Mackie {model_name}: LCD {line} = \"{text}\""
        except Exception:
            pass
    if cmd in (0x0C, 0x0D) and len(payload) >= 2:
        # Fader: often channel index + value
        return f"Mackie {model_name}: {cmd_name} ch={payload[0]} val={payload[1]}"
    return f"Mackie {model_name}: {cmd_name} ({len(payload)} bytes)"


def decode_message(msg) -> str:
    """
    Decode a mido message into a short human-readable MCU description.
    msg: mido Message (or dict with type, note, velocity, control, value, data bytes).
    """
    if hasattr(msg, "type"):
        msg_type = msg.type
    else:
        msg_type = msg.get("type", "")

    if msg_type == "note_on":
        note = msg.note if hasattr(msg, "note") else msg.get("note", 0)
        vel = msg.velocity if hasattr(msg, "velocity") else msg.get("velocity", 0)
        name = decode_mcu_note(note) or f"Note {note}"
        on_off = "ON" if vel else "OFF"
        return f"{name} {on_off} (note={note} vel={vel})"
    if msg_type == "note_off":
        note = msg.note if hasattr(msg, "note") else msg.get("note", 0)
        name = decode_mcu_note(note) or f"Note {note}"
        return f"{name} OFF (note={note})"
    if msg_type == "control_change":
        cc = msg.control if hasattr(msg, "control") else msg.get("control", 0)
        val = msg.value if hasattr(msg, "value") else msg.get("value", 0)
        # MCU faders often use CC 0-7 for channel faders, 16 for master
        if 0 <= cc <= 7:
            return f"Fader Ch {cc + 1} = {val}"
        if cc == 16:
            return f"Master Fader = {val}"
        return f"CC {cc} = {val}"
    if msg_type == "sysex":
        raw = msg.data if hasattr(msg, "data") else msg.get("data", [])
        data = bytes(bytearray(raw))
        return decode_sysex(data)
    return str(msg)
