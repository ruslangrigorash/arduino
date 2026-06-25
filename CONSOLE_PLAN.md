# Console Project — Plan & Notes (for later)

Status: **deferred**. The single-fader motor + GUI + FIT MIDI already works. This
file captures the design for the larger console so we can resume cold.

## What already works today (don't rebuild)
- `motor_fader_v1/motor_fader_v1.ino` (Arduino Uno): one motorized ALPS fader via
  L9110 on D5/D6, wiper on A0, capacitive touch on D7 (1M pull-up to 5V).
  - Hybrid control: GUI/LV1 sets target -> motor moves; physically touching the
    fader releases the motor and reports position; touch debounce + hysteresis.
  - PWM ~1 kHz (Timer0), control loop 400 Hz, telemetry on-change at ~50 Hz
    (`REPORT_MIN_INTERVAL_MS = 20`).
  - Serial telemetry line: `pos=.. target=.. moving=.. touch=..` @ 115200.
- `motor_fader_gui.py` (host, Python/tkinter + mido + teVirtualMIDI SDK):
  - Creates its own virtual MIDI ports via teVirtualMIDI DLL (no loopMIDI needed),
    class `TeVirtualMIDIOut` (open/send_raw/close + RX read thread).
  - **FIT transport**: two virtual ports `MotorFader GUI Out 1/2`, 16 channels each
    via pitchbend `E0..EF` => 32 channels total. Per-port FIT identity reply to
    `F0 7E 7F 06 01 F7` => `F0 7E 7F 06 02 00 00 74 3C 1C F7`.
  - Fader position = 14-bit pitchbend; **fader-touch/select note = `0x60 + local`**
    (also selects the channel in LV1). Touch-gated echo: only report to LV1 while
    physically touched, so LV1 never fights its own automation.
  - `Ch 1-32` => bank = (ch-1)//16, local = (ch-1)%16.
  - Perf: serial poll 12 ms; routine `pos=` lines NOT logged; log capped 500 lines.
- In LV1: add the controller as a **FIT** device (NOT MCU).

## Console target (build much later)
FIT-based control surface. Scope is intentionally modest: reuse FIT + MCU-style
V-pots + scribble-driven displays, all auto-populated by LV1.

### EQ control model (per channel, 4 bands)
- Each band = **Midas Heritage-style dual-concentric encoder**:
  - **Outer encoder = Frequency**.
  - **Inner V-pot = Gain** (default); **press inner => Q mode**.
  - **Q mode timer**: each Q adjustment restarts a **3 s** idle timer; 3 s with no
    adjustment => revert to Gain.
  - **Dual LED ring**: outer ring = Freq, inner ring = Gain/active value.
- Only **one channel is live to LV1 at a time**: touching any control selects that
  channel; all other channels render from a **local per-channel cache** (LV1 only
  ever exposes the selected channel's params).
- On select, refresh that channel's cache from LV1 (scribble + ring values) to
  avoid drift, then edits apply.

### LV1 EQ V-pot mapping (from LV1 Mackie Control appendix, mode note `0x2c`)
8 V-pots = 4 bands. Q lives on a **second page**, not a press:

| knob (0-based) | Page 1 | Page 2 | Press |
|---|---|---|---|
| `2B-2` (1,3,5,7) | Band B Freq | Band B Q | Band B Type |
| `2B-1` (2,4,6,8) | Band B Gain | Band B Gain | Band B On/Off |

- V-pot turn (console->LV1): `B0, 0x10+knob, delta` (sign-magnitude: 0x01..0x3F +,
  0x41..0x7F -).
- V-pot press (console->LV1): note `0x20+knob`.
- Page flip: notes `0x30` (prev) / `0x31` (next), global to all 8 knobs.
- Ring value (LV1->console): `B0, 0x30+knob, value`.
- Param name/value (LV1->console): scribble SysEx (`F0 00 00 74 3C 1A ...`) -> TFT.
- Mode selects: Track `0x28`, Sends `0x29`, Pan `0x2a`, Plugins `0x2b`, EQ `0x2c`,
  Dyn `0x2d`. Bank/layer: `0x2e`/`0x2f`. Vpot page: `0x30`/`0x31`. Channel select:
  `0x18-0x1F`. Solo `0x08-0x0F`, Mute `0x10-0x17`.

### Decision: FIT vs MCU paging
LV1 **FIT** is expected to present each parameter in the correct mode directly, so
we will **NOT** pre-build MCU-style page flipping. Plan: **"sharkbite and see"** —
capture the real FIT traffic when an EQ is open and map from observation. Match
params by **scribble name** (Freq/Gain/Q), not fixed index.

### Open questions to resolve later
- Confirm FIT exposes Freq/Gain/Q without manual paging (capture proves it).
- Band Type + On/Off: assign to other buttons or ignore.
- Q-mode-while-FIT: how FIT addresses Q vs the MCU page-2 scheme.

## Immediate next step when we resume
1. Add a **raw FIT capture mode** to `motor_fader_gui.py`: log every inbound
   virtual-port message (raw hex + decode of V-pot turns `B0 1x`, rings `B0 3x`,
   presses `0x20+`, scribble SysEx) to the log box and a file. (Currently
   `on_virtual_midi_in` drops everything except identity + pitchbend.)
2. In LV1 (FIT device), open an EQ on a selected channel, wiggle Freq/Gain/Q,
   capture the messages.
3. From the capture, write the per-band state machine (outer/inner/press, 3 s Q
   timer) + name-based param map as the host V-pot module.

## Full console hardware scope (reference)
182 pots, 34 motorized faders, 16 MIDI buttons w/ displays, 32 solo + 32 mute +
32 select buttons, 16 OSC controls, 182+34 LED displays.
- L9110 is too weak for many faders -> use TB6612 / DRV8833; budget supply for 34
  motors (ALPS RSA0N motor ~10V, <=800 mA stall).
- Distributed nodes (Teensy 4.1 / RP2040) per section -> host PC app over USB/Eth;
  fader PID/touch loops stay local; host does FIT MIDI + OSC + state cache.
- I/O expansion: 74HC4067 mux (pots), MCP23017 / 74HC165 (buttons),
  74HC595 / TLC5940 / MAX7219 (LEDs), PCA9685 (fader motor PWM), HT16K33/MAX7219
  or TFTs for displays.
