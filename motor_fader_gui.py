import re
import time
import ctypes
import ctypes.wintypes as wt
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

try:
    import serial
    from serial import SerialException
    from serial.tools import list_ports
except ImportError:  # pragma: no cover
    serial = None
    SerialException = Exception
    list_ports = None

try:
    import mido
except ImportError:  # pragma: no cover
    mido = None


KV_RE = re.compile(r"([a-zA-Z_]+)=(-?\d+)")


class TeVirtualMIDIOut:
    """Minimal teVirtualMIDI SDK sender for Windows."""

    TE_VM_FLAGS_PARSE_RX = 1
    TE_VM_FLAGS_PARSE_TX = 2
    TE_VM_FLAGS_INSTANTIATE_BOTH = 12
    MAX_SYSEX_LENGTH = 65535

    _DLL_CANDIDATES = [
        "teVirtualMIDI64.dll",
        r"C:\Windows\System32\teVirtualMIDI64.dll",
        r"C:\Windows\SysWOW64\teVirtualMIDI.dll",
    ]

    def __init__(self, port_name: str, on_data=None) -> None:
        self.port_name = port_name
        self.on_data = on_data
        self._dll = None
        self._handle = None
        self._running = False
        self._read_thread = None

    def _load_dll(self):
        if self._dll is not None:
            return self._dll

        last_error = None
        for candidate in self._DLL_CANDIDATES:
            try:
                dll = ctypes.WinDLL(candidate, use_last_error=True)
                # Bind signatures once loaded.
                dll.virtualMIDICreatePortEx2.argtypes = [
                    wt.LPCWSTR,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                    wt.DWORD,
                    wt.DWORD,
                ]
                dll.virtualMIDICreatePortEx2.restype = ctypes.c_void_p

                dll.virtualMIDIClosePort.argtypes = [ctypes.c_void_p]
                dll.virtualMIDIClosePort.restype = None

                dll.virtualMIDISendData.argtypes = [
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_ubyte),
                    wt.DWORD,
                ]
                dll.virtualMIDISendData.restype = wt.BOOL

                dll.virtualMIDIGetData.argtypes = [
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_ubyte),
                    ctypes.POINTER(wt.DWORD),
                ]
                dll.virtualMIDIGetData.restype = wt.BOOL

                if hasattr(dll, "virtualMIDIShutdown"):
                    dll.virtualMIDIShutdown.argtypes = [ctypes.c_void_p]
                    dll.virtualMIDIShutdown.restype = wt.BOOL

                self._dll = dll
                return dll
            except Exception as exc:
                last_error = exc
                continue

        raise OSError(f"Unable to load teVirtualMIDI DLL: {last_error}")

    def open(self) -> None:
        dll = self._load_dll()
        # Match prior working project behavior.
        flags = self.TE_VM_FLAGS_PARSE_RX | self.TE_VM_FLAGS_INSTANTIATE_BOTH
        self._handle = dll.virtualMIDICreatePortEx2(
            self.port_name,
            None,
            None,
            self.MAX_SYSEX_LENGTH,
            flags,
        )
        if not self._handle:
            err = ctypes.get_last_error()
            raise OSError(f"virtualMIDICreatePortEx2 failed (Windows error {err})")

        self._running = True
        if self.on_data is not None:
            self._read_thread = threading.Thread(target=self._read_loop, daemon=True)
            self._read_thread.start()

    def send_raw(self, data: bytes) -> bool:
        if not self._handle:
            return False
        if not data:
            return True
        buf = (ctypes.c_ubyte * len(data))(*data)
        ok = self._dll.virtualMIDISendData(self._handle, buf, len(data))
        return bool(ok)

    def _read_loop(self):
        buf = (ctypes.c_ubyte * self.MAX_SYSEX_LENGTH)()
        while self._running and self._handle:
            length = wt.DWORD(self.MAX_SYSEX_LENGTH)
            ok = self._dll.virtualMIDIGetData(self._handle, buf, ctypes.byref(length))
            if not ok:
                break
            if length.value > 0 and self._running:
                data = bytes(buf[:length.value])
                if self.on_data is not None:
                    try:
                        self.on_data(data)
                    except Exception:
                        pass

    def close(self) -> None:
        self._running = False
        if self._handle:
            if hasattr(self._dll, "virtualMIDIShutdown"):
                try:
                    self._dll.virtualMIDIShutdown(self._handle)
                except Exception:
                    pass
            try:
                self._dll.virtualMIDIClosePort(self._handle)
            except Exception:
                pass
            self._handle = None
        if self._read_thread is not None:
            try:
                self._read_thread.join(timeout=1.0)
            except Exception:
                pass
            self._read_thread = None


class MotorFaderGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Motor Fader Control")
        self.root.geometry("760x560")

        self.ser = None
        self.poll_interval_ms = 12
        self.log_max_lines = 500
        self.user_dragging_slider = False
        self.last_slider_sent_value = None
        self.last_slider_send_ms = 0.0
        self.slider_send_interval_ms = 8.0
        self.last_moving = 0
        self.user_typing_target = False
        self.last_pos_value = None

        self.midi_out = None
        self.midi_ports = []  # TeVirtualMIDIOut, one per 16-channel bank (FIT-style)
        self.MIDI_PORT_NAMES = ("MotorFader GUI Out 1", "MotorFader GUI Out 2")
        self.CHANNELS_PER_PORT = 16
        self._send_touch_note_active = False
        self.midi_port_var = tk.StringVar()
        self.midi_status_var = tk.StringVar(value="MIDI: Disconnected")
        self.midi_channel_var = tk.StringVar(value="1")
        self.midi_cc_var = tk.StringVar(value="1")
        self.midi_mode_var = tk.StringVar(value="MCU Pitchbend")
        self.last_midi_cc_value = None
        self.last_lv1_target_value = None
        self.last_lv1_target_ms = 0.0

        # Cached copies of channel/mode so the background MIDI read thread never
        # touches Tk variables (Tk is not thread-safe).
        self._channel_cache = 1
        self._mode_cache = "MCU Pitchbend"
        # True while we are reporting a physical fader-touch move to the DAW.
        self._touch_echo_active = False
        # When True, touching the fader also sends the MCU SELECT note so the
        # DAW selects that channel (like the VU meter bridge).
        self.select_on_touch_var = tk.BooleanVar(value=True)

        self.port_var = tk.StringVar()
        self.baud_var = tk.StringVar(value="115200")
        self.status_var = tk.StringVar(value="Disconnected")
        self.current_pos_var = tk.StringVar(value="-")
        self.target_pos_var = tk.StringVar(value="-")
        self.touch_var = tk.StringVar(value="-")
        self.slider_var = tk.IntVar(value=512)
        self.target_entry_var = tk.StringVar(value="512")
        self.command_entry_var = tk.StringVar(value="")

        self.midi_channel_var.trace_add("write", self._on_channel_change)
        self.midi_mode_var.trace_add("write", self._on_mode_change)

        self._build_ui()
        self.refresh_ports()
        self.root.after(self.poll_interval_ms, self.poll_serial)

    def _on_channel_change(self, *_args) -> None:
        try:
            self._channel_cache = max(1, min(32, int(self.midi_channel_var.get().strip())))
        except (ValueError, AttributeError):
            pass

    def _on_mode_change(self, *_args) -> None:
        self._mode_cache = self.midi_mode_var.get().strip()
        self.last_midi_cc_value = None

    def _threadsafe_log(self, text: str) -> None:
        self.root.after(0, lambda: self.append_log(text))

    def _build_ui(self) -> None:
        top = ttk.LabelFrame(self.root, text="Connection")
        top.pack(fill="x", padx=10, pady=(10, 6))

        ttk.Label(top, text="Port").grid(row=0, column=0, padx=6, pady=8, sticky="w")
        self.port_combo = ttk.Combobox(top, textvariable=self.port_var, width=20, state="readonly")
        self.port_combo.grid(row=0, column=1, padx=6, pady=8, sticky="w")

        ttk.Button(top, text="Refresh", command=self.refresh_ports).grid(row=0, column=2, padx=6, pady=8)

        ttk.Label(top, text="Baud").grid(row=0, column=3, padx=6, pady=8, sticky="w")
        ttk.Entry(top, textvariable=self.baud_var, width=10).grid(row=0, column=4, padx=6, pady=8, sticky="w")

        self.connect_btn = ttk.Button(top, text="Connect", command=self.connect)
        self.connect_btn.grid(row=0, column=5, padx=6, pady=8)

        self.disconnect_btn = ttk.Button(top, text="Disconnect", command=self.disconnect, state="disabled")
        self.disconnect_btn.grid(row=0, column=6, padx=6, pady=8)

        ttk.Label(top, textvariable=self.status_var, foreground="#0b5").grid(
            row=0, column=7, padx=10, pady=8, sticky="w"
        )

        middle = ttk.LabelFrame(self.root, text="Set Target")
        middle.pack(fill="x", padx=10, pady=6)

        self.slider = tk.Scale(
            middle,
            from_=0,
            to=1023,
            orient="horizontal",
            resolution=1,
            length=680,
            variable=self.slider_var,
            showvalue=True,
            command=self.on_slider_change,
        )
        self.slider.pack(padx=10, pady=(6, 2), fill="x")
        self.slider.bind("<ButtonPress-1>", self.on_slider_press)
        self.slider.bind("<ButtonRelease-1>", self.on_slider_release)

        quick = ttk.Frame(middle)
        quick.pack(fill="x", padx=8, pady=(2, 8))

        ttk.Button(quick, text="Min", command=lambda: self.send_named("min")).pack(side="left", padx=4)
        ttk.Button(quick, text="Center", command=lambda: self.send_named("center")).pack(side="left", padx=4)
        ttk.Button(quick, text="Max", command=lambda: self.send_named("max")).pack(side="left", padx=4)
        ttk.Button(quick, text="Stop", command=lambda: self.send_named("stop")).pack(side="left", padx=4)
        ttk.Button(quick, text="Read", command=lambda: self.send_named("read")).pack(side="left", padx=4)

        ttk.Label(quick, text="Target").pack(side="left", padx=(20, 4))
        self.target_entry = ttk.Entry(quick, textvariable=self.target_entry_var, width=8)
        self.target_entry.pack(side="left")
        self.target_entry.bind("<FocusIn>", self.on_target_focus_in)
        self.target_entry.bind("<FocusOut>", self.on_target_focus_out)
        self.target_entry.bind("<Return>", lambda _e: self.send_manual_target())
        ttk.Button(quick, text="Send", command=self.send_manual_target).pack(side="left", padx=6)

        ttk.Label(quick, text="Cmd").pack(side="left", padx=(14, 4))
        self.command_entry = ttk.Entry(quick, textvariable=self.command_entry_var, width=10)
        self.command_entry.pack(side="left")
        self.command_entry.bind("<Return>", lambda _e: self.send_command_text())
        ttk.Button(quick, text="Run", command=self.send_command_text).pack(side="left", padx=6)

        telemetry = ttk.LabelFrame(self.root, text="Live Telemetry")
        telemetry.pack(fill="x", padx=10, pady=6)
        ttk.Label(telemetry, text="Position:").grid(row=0, column=0, padx=8, pady=8, sticky="w")
        ttk.Label(telemetry, textvariable=self.current_pos_var, width=8).grid(row=0, column=1, sticky="w")
        ttk.Label(telemetry, text="Target:").grid(row=0, column=2, padx=8, pady=8, sticky="w")
        ttk.Label(telemetry, textvariable=self.target_pos_var, width=8).grid(row=0, column=3, sticky="w")
        ttk.Label(telemetry, text="Touch:").grid(row=0, column=4, padx=8, pady=8, sticky="w")
        ttk.Label(telemetry, textvariable=self.touch_var, width=8).grid(row=0, column=5, sticky="w")

        midi = ttk.LabelFrame(self.root, text="MIDI Output")
        midi.pack(fill="x", padx=10, pady=6)

        ttk.Label(midi, text="Port").grid(row=0, column=0, padx=6, pady=8, sticky="w")
        self.midi_port_combo = ttk.Combobox(midi, textvariable=self.midi_port_var, width=32, state="readonly")
        self.midi_port_combo.grid(row=0, column=1, padx=6, pady=8, sticky="w")
        ttk.Button(midi, text="Refresh", command=self.refresh_midi_ports).grid(row=0, column=2, padx=6, pady=8)

        ttk.Button(midi, text="Open", command=self.connect_midi).grid(row=0, column=3, padx=6, pady=8)
        ttk.Button(midi, text="Create SDK Port", command=self.open_virtual_midi).grid(row=0, column=4, padx=6, pady=8)
        ttk.Button(midi, text="Close", command=self.disconnect_midi).grid(row=0, column=5, padx=6, pady=8)

        ttk.Label(midi, text="Ch 1-32").grid(row=1, column=0, padx=6, pady=(0, 8), sticky="w")
        ttk.Entry(midi, textvariable=self.midi_channel_var, width=6).grid(row=1, column=1, padx=6, pady=(0, 8), sticky="w")
        ttk.Label(midi, text="CC").grid(row=1, column=2, padx=6, pady=(0, 8), sticky="w")
        ttk.Entry(midi, textvariable=self.midi_cc_var, width=6).grid(row=1, column=3, padx=6, pady=(0, 8), sticky="w")
        ttk.Label(midi, text="Mode").grid(row=2, column=0, padx=6, pady=(0, 8), sticky="w")
        ttk.Checkbutton(
            midi,
            text="Fader-touch note (selects ch)",
            variable=self.select_on_touch_var,
        ).grid(row=2, column=2, columnspan=2, padx=6, pady=(0, 8), sticky="w")
        self.midi_mode_combo = ttk.Combobox(
            midi,
            textvariable=self.midi_mode_var,
            width=14,
            state="readonly",
            values=("MCU Pitchbend", "CC"),
        )
        self.midi_mode_combo.grid(row=2, column=1, padx=6, pady=(0, 8), sticky="w")
        ttk.Label(midi, textvariable=self.midi_status_var).grid(row=1, column=4, columnspan=2, padx=8, pady=(0, 8), sticky="w")

        log_frame = ttk.LabelFrame(self.root, text="Serial Log")
        log_frame.pack(fill="both", expand=True, padx=10, pady=(6, 10))
        self.log = scrolledtext.ScrolledText(log_frame, height=16, wrap="word")
        self.log.pack(fill="both", expand=True, padx=8, pady=8)
        self.log.configure(state="disabled")

    def refresh_ports(self) -> None:
        if list_ports is None:
            self.port_combo["values"] = ()
            return

        ports = sorted([p.device for p in list_ports.comports()])
        self.port_combo["values"] = ports

        if ports:
            if self.port_var.get() not in ports:
                self.port_var.set(ports[0])
        else:
            self.port_var.set("")

    def refresh_midi_ports(self) -> None:
        if mido is None:
            self.midi_port_combo["values"] = ()
            self.midi_port_var.set("")
            self.midi_status_var.set("MIDI: install mido python-rtmidi")
            return

        try:
            ports = sorted(mido.get_output_names())
        except Exception as exc:
            self.midi_port_combo["values"] = ()
            self.midi_port_var.set("")
            self.midi_status_var.set(f"MIDI error: {exc}")
            return

        self.midi_port_combo["values"] = ports
        if ports:
            if self.midi_port_var.get() not in ports:
                self.midi_port_var.set(ports[0])
        else:
            self.midi_port_var.set("")

    def connect(self) -> None:
        if serial is None:
            messagebox.showerror("Missing dependency", "Please install pyserial:\n\npip install pyserial")
            return

        port = self.port_var.get().strip()
        if not port:
            messagebox.showwarning("No port", "Select a COM port first.")
            return

        try:
            baud = int(self.baud_var.get().strip())
        except ValueError:
            messagebox.showwarning("Invalid baud", "Baud must be a number.")
            return

        try:
            self.ser = serial.Serial(port=port, baudrate=baud, timeout=0.02)
        except SerialException as exc:
            messagebox.showerror("Connection failed", str(exc))
            self.ser = None
            return

        time.sleep(1.6)  # UNO reset settle time
        self.status_var.set(f"Connected: {port} @ {baud}")
        self.connect_btn.configure(state="disabled")
        self.disconnect_btn.configure(state="normal")
        self.append_log(f"Connected to {port} @ {baud}")
        self.send_named("?")
        self.send_named("read")

    def connect_midi(self) -> None:
        if mido is None:
            messagebox.showerror("Missing dependency", "Install MIDI libs:\n\npip install mido python-rtmidi")
            return

        name = self.midi_port_var.get().strip()
        if not name:
            messagebox.showwarning("No MIDI port", "Select a MIDI output port first.")
            return

        self.disconnect_midi()
        try:
            self.midi_out = mido.open_output(name)
        except Exception as exc:
            messagebox.showerror("MIDI open failed", str(exc))
            self.midi_out = None
            self.midi_status_var.set("MIDI: Disconnected")
            return

        self.last_midi_cc_value = None
        self.midi_status_var.set(f"MIDI: {name}")
        self.append_log(f"MIDI open: {name}")

    def open_virtual_midi(self) -> None:
        self.disconnect_midi()
        try:
            for idx, name in enumerate(self.MIDI_PORT_NAMES):
                port = TeVirtualMIDIOut(
                    name,
                    on_data=lambda d, i=idx: self.on_virtual_midi_in(i, d),
                )
                port.open()
                self.midi_ports.append(port)
        except Exception as exc:
            messagebox.showerror("SDK virtual MIDI failed", str(exc))
            self.disconnect_midi()
            self.midi_status_var.set("MIDI: Disconnected")
            return

        self.last_midi_cc_value = None
        self.midi_status_var.set("MIDI SDK: " + " + ".join(self.MIDI_PORT_NAMES))
        self.append_log("MIDI SDK ports created: " + ", ".join(self.MIDI_PORT_NAMES))
        self.refresh_midi_ports()

    def on_virtual_midi_in(self, port_index: int, data: bytes) -> None:
        # LV1 identity request -> reply on the same port with the FIT identity.
        if data == bytes([0xF0, 0x7E, 0x7F, 0x06, 0x01, 0xF7]):
            resp = bytes([0xF0, 0x7E, 0x7F, 0x06, 0x02, 0x00, 0x00, 0x74, 0x3C, 0x1C, 0xF7])
            if 0 <= port_index < len(self.midi_ports):
                self.midi_ports[port_index].send_raw(resp)
            self._threadsafe_log(f"<< [port {port_index + 1}] LV1 identity request; sent FIT identity")
            return

        # Fader position from LV1: pitchbend E0..EF -> local fader 0..15 on this
        # port. Global channel = port_index * 16 + local + 1 (two ports => 32).
        if len(data) == 3 and 0xE0 <= data[0] <= 0xEF:
            local = data[0] & 0x0F
            lsb = data[1] & 0x7F
            msb = data[2] & 0x7F
            pb14 = (msb << 7) | lsb
            pos = max(0, min(1023, int(round((pb14 / 16383.0) * 1023.0))))
            global_ch = port_index * self.CHANNELS_PER_PORT + local + 1
            self._handle_lv1_fader(global_ch, pos)
            return

    def _handle_lv1_fader(self, global_ch: int, pos_0_1023: int) -> None:
        # Only react to the channel currently assigned to the motor fader.
        # Read the cached channel (set on the main thread) — never read Tk
        # variables from this background MIDI read thread.
        if global_ch != self._channel_cache:
            return

        now_ms = time.monotonic() * 1000.0
        if self.last_lv1_target_value is not None:
            if abs(pos_0_1023 - self.last_lv1_target_value) < 2 and (now_ms - self.last_lv1_target_ms) < 120:
                return

        self.last_lv1_target_value = pos_0_1023
        self.last_lv1_target_ms = now_ms
        self.root.after(0, lambda p=pos_0_1023: self._apply_lv1_target(p))

    def _apply_lv1_target(self, pos_0_1023: int) -> None:
        if not self.is_connected():
            return
        if self._touch_echo_active:
            # User is physically holding the motor fader; the DAW was told via
            # fader-touch, so ignore any stray automation it still sends.
            return
        self.slider_var.set(pos_0_1023)
        if not self.user_typing_target:
            self.target_entry_var.set(str(pos_0_1023))
        self.send_slider_target(pos_0_1023, force=True)
        self.append_log(f"<< LV1 fader -> target {pos_0_1023}")

    def disconnect_midi(self) -> None:
        if self.midi_out is not None:
            try:
                self.midi_out.close()
            except Exception:
                pass
        for port in self.midi_ports:
            try:
                port.close()
            except Exception:
                pass
        self.midi_ports = []
        self.midi_out = None
        self.last_midi_cc_value = None
        self._touch_echo_active = False
        self.midi_status_var.set("MIDI: Disconnected")

    def _have_midi(self) -> bool:
        return bool(self.midi_ports) or self.midi_out is not None

    def _bank_local(self, global_ch: int):
        # Map a 1..32 channel to (bank port index, local fader 0..15).
        g = max(1, min(32, global_ch)) - 1
        return g // self.CHANNELS_PER_PORT, g % self.CHANNELS_PER_PORT

    def _send_midi_bytes(self, data: bytes, bank: int = 0) -> None:
        if not self._have_midi():
            return
        try:
            if self.midi_ports:
                if 0 <= bank < len(self.midi_ports):
                    self.midi_ports[bank].send_raw(data)
            elif self.midi_out is not None and mido is not None and bank == 0:
                self.midi_out.send(mido.Message.from_bytes(list(data)))
        except Exception as exc:
            self.append_log(f"MIDI send error: {exc}")
            self.disconnect_midi()

    def _send_fit_fader_touch(self, on: bool) -> None:
        # FIT fader-touch note = 0x60 + local fader index (0..15), sent on the
        # bank port that owns the channel. This note also makes LV1 select the
        # channel, so it doubles as channel-select.
        bank, local = self._bank_local(self._channel_cache)
        note = 0x60 + local
        velocity = 0x7F if on else 0x00
        self._send_midi_bytes(bytes([0x90, note, velocity]), bank)

    def send_midi_from_pos(self, pos_0_1023: int, force: bool = False) -> None:
        if not self._have_midi():
            return

        bank, local = self._bank_local(self._channel_cache)
        if self._mode_cache == "MCU Pitchbend":
            pb_val = max(0, min(16383, int(round((pos_0_1023 / 1023.0) * 16383.0))))
            if not force and pb_val == self.last_midi_cc_value:
                return
            self._send_midi_bytes(bytes([0xE0 | local, pb_val & 0x7F, (pb_val >> 7) & 0x7F]), bank)
            self.last_midi_cc_value = pb_val
        else:
            try:
                cc = max(0, min(127, int(self.midi_cc_var.get().strip())))
            except (ValueError, AttributeError):
                cc = 1
            midi_val = max(0, min(127, int(round((pos_0_1023 / 1023.0) * 127.0))))
            if not force and midi_val == self.last_midi_cc_value:
                return
            self._send_midi_bytes(bytes([0xB0 | local, cc, midi_val]), bank)
            self.last_midi_cc_value = midi_val

    def _maybe_echo_to_lv1(self, pos_0_1023: int, touched: bool) -> None:
        if not self._have_midi():
            return

        # Generic CC mode: stream position changes continuously.
        if self._mode_cache != "MCU Pitchbend":
            self.send_midi_from_pos(pos_0_1023)
            return

        # FIT/MCU mode (real motor-fader behavior): only report position to the
        # DAW while the user physically touches the fader. While the motor merely
        # chases a DAW command, stay silent so the DAW never gets its own lagging
        # position back and fights the move.
        if touched and not self._touch_echo_active:
            self._touch_echo_active = True
            self._send_touch_note_active = bool(self.select_on_touch_var.get())
            if self._send_touch_note_active:
                # Touch note also selects the channel in LV1.
                self._send_fit_fader_touch(True)
            self.send_midi_from_pos(pos_0_1023, force=True)
        elif touched and self._touch_echo_active:
            self.send_midi_from_pos(pos_0_1023)
        elif (not touched) and self._touch_echo_active:
            self.send_midi_from_pos(pos_0_1023, force=True)
            if self._send_touch_note_active:
                self._send_fit_fader_touch(False)
            self._touch_echo_active = False

    def disconnect(self) -> None:
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self.status_var.set("Disconnected")
        self.connect_btn.configure(state="normal")
        self.disconnect_btn.configure(state="disabled")
        self.append_log("Disconnected")

    def is_connected(self) -> bool:
        return self.ser is not None and getattr(self.ser, "is_open", False)

    def send_command(self, cmd: str) -> None:
        if not self.is_connected():
            self.append_log("Not connected")
            return
        try:
            self.ser.write((cmd.strip() + "\n").encode("utf-8"))
            self.append_log(f">> {cmd.strip()}")
        except Exception as exc:
            self.append_log(f"Write error: {exc}")
            self.disconnect()

    def send_named(self, cmd: str) -> None:
        if cmd == "min":
            self.slider_var.set(0)
            self.target_entry_var.set("0")
        elif cmd == "center":
            self.slider_var.set(512)
            self.target_entry_var.set("512")
        elif cmd == "max":
            self.slider_var.set(1023)
            self.target_entry_var.set("1023")
        self.send_command(cmd)

    def send_manual_target(self) -> None:
        text = self.target_entry_var.get().strip()
        try:
            value = int(text)
        except ValueError:
            messagebox.showwarning("Invalid target", "Target must be 0..1023.")
            return

        if not 0 <= value <= 1023:
            messagebox.showwarning("Invalid target", "Target must be between 0 and 1023.")
            return

        self.slider_var.set(value)
        self.send_slider_target(value, force=True)

    def on_slider_change(self, value: str) -> None:
        int_value = int(float(value))
        if not self.user_typing_target:
            self.target_entry_var.set(str(int_value))
        if self.user_dragging_slider:
            self.send_slider_target(int_value)

    def on_target_focus_in(self, _event) -> None:
        self.user_typing_target = True

    def on_target_focus_out(self, _event) -> None:
        self.user_typing_target = False

    def send_command_text(self) -> None:
        cmd = self.command_entry_var.get().strip()
        if not cmd:
            return
        self.send_command(cmd)
        self.command_entry_var.set("")

    def on_slider_press(self, _event) -> None:
        self.user_dragging_slider = True

    def on_slider_release(self, _event) -> None:
        self.user_dragging_slider = False
        self.send_slider_target(self.slider_var.get(), force=True)

    def send_slider_target(self, value: int, force: bool = False) -> None:
        now_ms = time.monotonic() * 1000.0
        if not force:
            if self.last_slider_sent_value is not None and abs(value - self.last_slider_sent_value) < 2:
                return
            if value == self.last_slider_sent_value and (now_ms - self.last_slider_send_ms) < 120:
                return
            if (now_ms - self.last_slider_send_ms) < self.slider_send_interval_ms:
                return

        self.last_slider_sent_value = value
        self.last_slider_send_ms = now_ms
        self.send_command(str(value))

    def append_log(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"[{stamp}] {text}\n")
        # Trim so the Text widget never grows unbounded (which slows Tk badly).
        line_count = int(self.log.index("end-1c").split(".")[0])
        if line_count > self.log_max_lines:
            self.log.delete("1.0", f"{line_count - self.log_max_lines}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _handle_incoming_line(self, line: str) -> None:
        # Don't render routine streaming telemetry (every "pos=" line) to the log
        # box — it floods Tk and progressively stalls the UI. Still parse it; only
        # log notable events (banner, faults, stopped, identity, etc.).
        if "pos=" not in line:
            self.append_log(f"<< {line}")
        pairs = KV_RE.findall(line)
        if not pairs:
            return

        data = {k: int(v) for k, v in pairs}
        if "moving" in data:
            self.last_moving = data["moving"]

        touched = bool(data.get("touch", 0))
        if "touch" in data:
            self.touch_var.set(str(data["touch"]))

        if "pos" in data:
            pos = data["pos"]
            self.current_pos_var.set(str(pos))
            self.last_pos_value = pos
            self._maybe_echo_to_lv1(pos, touched)
            # Only follow hardware position when not actively executing a command.
            if not self.user_dragging_slider and self.last_moving == 0 and self.slider_var.get() != pos:
                self.slider_var.set(pos)

        if "target" in data:
            target = data["target"]
            self.target_pos_var.set(str(target))
            if not self.user_dragging_slider and not self.user_typing_target:
                self.target_entry_var.set(str(target))

    def poll_serial(self) -> None:
        if self.is_connected():
            try:
                while self.ser.in_waiting:
                    raw = self.ser.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="ignore").strip()
                    if line:
                        self._handle_incoming_line(line)
            except Exception as exc:
                self.append_log(f"Read error: {exc}")
                self.disconnect()

        self.root.after(self.poll_interval_ms, self.poll_serial)

    def on_close(self) -> None:
        self.disconnect_midi()
        self.disconnect()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = MotorFaderGUI(root)
    app.refresh_midi_ports()
    # Auto-create SDK virtual port on startup.
    try:
        app.open_virtual_midi()
    except Exception:
        pass
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
