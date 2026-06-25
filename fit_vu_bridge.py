"""
FIT VU Meter Bridge
Fullscreen borderless VU meter display (3840x200) for two FIT controllers.
Displays channel strips with VU metering from ASIO, channel pictures,
OSC gain rotary (placeholder), and MIDI scribble data from Waves LV1.

Usage:
  1. Install teVirtualMIDI SDK (creates virtual ports automatically)
  2. Run this script
  3. Press F1 to open config, set FIT hardware ports, ASIO device
  4. Click any strip to configure its picture, ASIO mapping, etc.
  5. Press Escape to quit
"""

import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import ctypes
import ctypes.wintypes

import numpy as np
import rtmidi
os.environ["SD_ENABLE_ASIO"] = "1"
import sounddevice as sd

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QPushButton, QComboBox, QFileDialog, QDialog,
    QGridLayout, QLineEdit, QSpinBox, QGroupBox, QScrollArea,
    QSizePolicy, QCheckBox, QFrame, QMessageBox,
)
from PyQt5.QtCore import Qt, QTimer, QRect, QSize, pyqtSignal, QPropertyAnimation, QEasingCurve, pyqtProperty
from PyQt5.QtGui import (
    QPainter, QColor, QPen, QPixmap, QFont, QPalette,
    QLinearGradient, QRadialGradient, QBrush,
)

try:
    from pythonosc import dispatcher as osc_dispatcher
    from pythonosc import osc_server
    HAS_OSC = True
except ImportError:
    HAS_OSC = False

import socket
import struct

# ── MTX Gain Reader ───────────────────────────────────────────────────────

MTX_IP       = "169.254.64.223"
MTX_PORT     = 51001
MTX_LOCAL_IP = "169.254.26.146"
MTX_FADER_MIN = -20
MTX_FADER_MAX =  70


def _mtx_ch_to_inp_lo(ch: int) -> int:
    """Physical 1-indexed channel → Nexus inp_lo byte."""
    board = (ch - 1) // 8
    slot  = (ch - 1) % 8
    return 0x65 + board * 0x20 + slot


def _mtx_inp_lo_to_ch(inp_lo: int) -> int:
    """Nexus inp_lo byte → physical 1-indexed channel, or -1 if invalid."""
    offset = inp_lo - 0x65
    board  = offset // 0x20
    slot   = offset % 0x20
    if slot > 7:
        return -1
    return board * 8 + slot + 1


def _mtx_build_req_inputs() -> bytes:
    """COMM_REQ_INPUTS (0x0034) — triggers full gain dump from MTX."""
    payload = bytes([0x81, 0x01, 0x00, 0x00, 0x4d, 0x53, 0x47, 0x5f])
    hdr = bytearray(32)
    hdr[0:4] = b"MTX5"
    struct.pack_into("<I", hdr, 4, 4)
    hdr[8:12] = bytes([0x00, 0x01, 0x00, 0x04])
    struct.pack_into("<I", hdr, 12, 0x0A)
    struct.pack_into(">H", hdr, 16, 0x4200)
    hdr[18] = 0x01
    msg = b"MSG_" + struct.pack("<I", 2) + struct.pack(">I", 0x00000034) + payload
    return bytes(hdr) + msg


def _mtx_build_set_fader(seq: int, channel: int, gain_db: int) -> bytes:
    """COMM_SET_INPUTS (0x002E) — set one channel fader. channel is 1-indexed."""
    gain_byte = gain_db & 0xFF
    header = bytearray(32)
    header[0:4] = b'MTX5'
    header[4:8] = struct.pack('<I', 4)
    header[8:12] = bytes([0x00, 0x01, 0x00, 0x04])
    header[12:16] = struct.pack('<I', 0x0a)
    struct.pack_into('>H', header, 16, seq & 0xFFFF)
    header[18] = 0x02
    msg1 = bytearray(24)
    msg1[0:4] = b'MSG_'
    msg1[4:8] = struct.pack('<I', 3)
    msg1[8:12] = bytes([0x0a, 0x00, 0x00, 0x3e])
    msg1[12:16] = bytes([0x81, 0x00, 0x01, 0x00])
    msg1[23] = 0x65
    msg2 = bytearray(24)
    msg2[0:4] = b'MSG_'
    msg2[4:8] = struct.pack('<I', 3)
    msg2[8:12] = bytes([0x05, 0x00, 0x00, 0x2e])
    msg2[12] = 0x81
    msg2[15] = _mtx_ch_to_inp_lo(channel)
    msg2[16] = 0x01
    msg2[17] = 0xff
    msg2[18] = gain_byte
    return bytes(header + msg1 + msg2)


def _mtx_decode_inputs(udp: bytes) -> list:
    """Parse COMM_THE_INPUTS (0x0035) from one UDP packet.
    Returns list of (physical_channel_1indexed, fader_dB).
    Scans for MSG_ magic bytes instead of trusting the wc field.
    """
    if len(udp) < 32 or udp[:4] != b"MTX5":
        return []
    positions = []
    pos = 32
    while pos + 12 <= len(udp):
        if udp[pos:pos+4] == b"MSG_":
            positions.append(pos)
            pos += 4
        else:
            pos += 1
    positions.append(len(udp))
    results = []
    for i, start in enumerate(positions[:-1]):
        end = positions[i + 1]
        cmd = struct.unpack_from(">I", udp, start + 8)[0] & 0xFFFF
        if cmd != 0x0035:
            continue
        pl = udp[start + 12: end]
        if len(pl) < 2:
            continue
        params = pl[2:]
        p = 0
        while p + 3 <= len(params):
            if params[p:p+4] == b"MSG_":
                break
            inp_hi = params[p]; inp_lo = params[p+1]; flags = params[p+2]
            k = flags & 0x0F; p += 3
            if p + k > len(params):
                break
            opt = params[p: p+k]; p += k
            param_type = (flags >> 4) & 0x0F
            if len(opt) >= 1 and param_type in (0, 4) and inp_hi == 0x00:
                ch = _mtx_inp_lo_to_ch(inp_lo)
                db = opt[0] if opt[0] < 128 else opt[0] - 256
                if 1 <= ch <= 64 and MTX_FADER_MIN <= db <= MTX_FADER_MAX:
                    results.append((ch, db))
    return results


def _mtx_db_to_frac(db: int) -> float:
    """Convert fader dB (-20..+70) to 0.0..1.0 for vpot LED ring."""
    return (db - MTX_FADER_MIN) / (MTX_FADER_MAX - MTX_FADER_MIN)


def _mtx_frac_to_db(frac: float) -> int:
    """Convert 0.0..1.0 vpot fraction back to dB."""
    return int(round(MTX_FADER_MIN + frac * (MTX_FADER_MAX - MTX_FADER_MIN)))


class MTXGainReader:
    """Background thread: requests all gains on startup, then listens for live updates.
    Calls on_gain(physical_ch, db) whenever a channel gain is received.
    """

    def __init__(self, on_gain, local_ip=MTX_LOCAL_IP,
                 mtx_ip=MTX_IP, mtx_port=MTX_PORT):
        self._on_gain  = on_gain
        self._local_ip = local_ip
        self._mtx_addr = (mtx_ip, mtx_port)
        self._sock     = None
        self._seq      = 0x1000
        self._stop     = threading.Event()
        self._thread   = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._stop.clear()
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass

    def restart(self, local_ip, mtx_ip, mtx_port):
        """Stop the current thread and start a fresh one with new addresses."""
        self.stop()
        if self._thread.is_alive():
            self._thread.join(timeout=2)
        self._local_ip = local_ip
        self._mtx_addr = (mtx_ip, int(mtx_port))
        self._stop     = threading.Event()
        self._thread   = threading.Thread(target=self._run, daemon=True)
        self._last_rx  = 0.0
        self.start()

    @property
    def last_rx(self):
        return getattr(self, "_last_rx", 0.0)

    def send_fader(self, channel: int, db: int):
        """Send a fader set command from any thread."""
        if self._sock is None:
            return
        db = max(MTX_FADER_MIN, min(MTX_FADER_MAX, db))
        pkt = _mtx_build_set_fader(self._seq, channel, db)
        self._seq = (self._seq + 1) & 0xFFFF
        try:
            self._sock.sendto(pkt, self._mtx_addr)
        except OSError:
            pass

    def _run(self):
        while not self._stop.is_set():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.bind((self._local_ip, 0))
                sock.settimeout(0.5)
                self._sock = sock
            except OSError as e:
                print(f"[MTXGainReader] socket error: {e}, retrying in 5s...")
                self._stop.wait(5)
                continue

            last_req = 0.0
            while not self._stop.is_set():
                if time.monotonic() - last_req >= 20.0:
                    try:
                        sock.sendto(_mtx_build_req_inputs(), self._mtx_addr)
                    except OSError:
                        break
                    last_req = time.monotonic()
                try:
                    data, _ = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                for ch, db in _mtx_decode_inputs(data):
                    self._last_rx = time.monotonic()
                    self._on_gain(ch, db)

            try:
                sock.close()
            except Exception:
                pass
            self._sock = None
            if not self._stop.is_set():
                print("[MTXGainReader] connection lost, retrying in 5s...")
                self._stop.wait(5)

# ── Paths ─────────────────────────────────────────────────────────────────

if getattr(sys, 'frozen', False):
    SCRIPT_DIR = os.path.dirname(sys.executable)
    BUNDLE_DIR = sys._MEIPASS
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    BUNDLE_DIR = SCRIPT_DIR
CONFIG_FILE = os.path.join(SCRIPT_DIR, "fit_vu_bridge_config.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "fit_vu_traffic.log")
PICTURES_DIR = os.path.join(SCRIPT_DIR, "channel_pictures")
PRESETS_DIR = os.path.join(SCRIPT_DIR, "channel_presets")

os.makedirs(PICTURES_DIR, exist_ok=True)
os.makedirs(PRESETS_DIR, exist_ok=True)

# ── Layout Constants ──────────────────────────────────────────────────────

TARGET_WIDTH = 3840
TARGET_HEIGHT = 200
STRIP_WIDTH = 101
NUM_CH = 16
STRIPS_PER_MIXER = 17
TOTAL_STRIPS = STRIPS_PER_MIXER * 2
MIXER_WIDTH = STRIPS_PER_MIXER * STRIP_WIDTH
GAP_WIDTH = (TARGET_WIDTH - 2 * MIXER_WIDTH) // 2
CHARS_PER_STRIP = 35

# ── Colors ────────────────────────────────────────────────────────────────

BG = "#181825"
STRIP_BG = "#1e1e2e"
STRIP_BG_SEL = "#2a2a40"
FG = "#cdd6f4"
FG_DIM = "#6c7086"
ACCENT = "#89b4fa"
GREEN = "#a6e3a1"
RED = "#f38ba8"
YELLOW = "#f9e2af"
OVERLAY = "#45475a"
VU_BG = "#11111b"
VU_GREEN = "#a6e3a1"
VU_YELLOW = "#f9e2af"
VU_RED = "#f38ba8"

COLOR_PALETTE = {
    0x00: "#7a9e9e", 0x01: "#3c5a6e", 0x02: "#4cb050", 0x03: "#1a8a6e",
    0x04: "#9edce6", 0x05: "#8890ae", 0x06: "#cca070", 0x07: "#d66080",
    0x08: "#1060c0", 0x09: "#3090a0", 0x0A: "#00e8e8", 0x0B: "#1a5a6a",
    0x0C: "#9098c0", 0x0D: "#1a8a8a", 0x0E: "#708878", 0x0F: "#50e880",
    0x10: "#7030a0", 0x11: "#c08898", 0x12: "#f0d850", 0x13: "#e02020",
    0x14: "#f0f0f0", 0x15: "#a01040", 0x16: "#80f020", 0x17: "#909090",
    0x18: "#f07820", 0x19: "#f020c0", 0x1A: "#c8a0d0", 0x1B: "#e070a0",
    0x1C: "#a09060", 0x1D: "#9878a0", 0x1E: "#883888", 0x1F: "#484088",
}


def color_for_code(code):
    return COLOR_PALETTE.get(code, OVERLAY)


# ── UI Helpers ────────────────────────────────────────────────────────────

class AnimatedButton(QPushButton):
    """QPushButton with a colour-flash animation on press and optional
    auto-restore label (e.g. "✓ Saved" for 1 s then back to original)."""

    def __init__(self, text, parent=None,
                 flash_color=None, flash_ms=180,
                 confirm_text=None, confirm_ms=1000):
        super().__init__(text, parent)
        self._base_text   = text
        self._flash_color = flash_color or ACCENT   # hex string
        self._flash_ms    = flash_ms
        self._confirm_text = confirm_text
        self._confirm_ms  = confirm_ms
        self._orig_ss     = ""
        self._flash_timer  = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._end_flash)
        self._confirm_timer = QTimer(self)
        self._confirm_timer.setSingleShot(True)
        self._confirm_timer.timeout.connect(self._end_confirm)

    def flash(self, color=None, confirm_text=None):
        """Trigger a flash manually (called after an action succeeds/fails)."""
        col = color or self._flash_color
        self._orig_ss = self.styleSheet()
        self.setStyleSheet(
            f"QPushButton {{ background: {col}; color: #1e1e2e; "
            f"border: none; padding: 6px 16px; border-radius: 3px; font-weight: bold; }}"
        )
        if confirm_text or self._confirm_text:
            self.setText(confirm_text or self._confirm_text)
            self._confirm_timer.start(self._confirm_ms)
        self._flash_timer.start(self._flash_ms)

    def flash_error(self):
        self.flash(color=RED)

    def _end_flash(self):
        self.setStyleSheet(self._orig_ss)

    def _end_confirm(self):
        self.setText(self._base_text)

    def mousePressEvent(self, event):
        # Brief darken on physical press (always, regardless of action result)
        self._orig_ss = self.styleSheet()
        self.setStyleSheet(
            f"QPushButton {{ background: {OVERLAY}; color: {ACCENT}; "
            f"border: 1px solid {ACCENT}; padding: 6px 16px; "
            f"border-radius: 3px; font-weight: bold; }}"
        )
        self._flash_timer.start(self._flash_ms)
        super().mousePressEvent(event)


class StatusDot(QLabel):
    """Small coloured dot indicator.  Call set_ok(True/False) to update."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(14, 14)
        self._ok = None
        self.set_ok(False)

    def set_ok(self, ok: bool):
        if ok == self._ok:
            return
        self._ok = ok
        col = GREEN if ok else RED
        self.setStyleSheet(
            f"background: {col}; border-radius: 7px; border: 1px solid #000;"
        )
        self.setToolTip("Connected" if ok else "Not connected")


# ── Display Detection (Windows) ──────────────────────────────────────────

def find_strip_display():
    """Find the 3840x200 monitor. Returns (x, y, w, h)."""
    monitors = []

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool,
        ctypes.wintypes.HMONITOR,
        ctypes.wintypes.HDC,
        ctypes.POINTER(ctypes.wintypes.RECT),
        ctypes.wintypes.LPARAM,
    )

    def _cb(hMon, hdc, lprc, _data):
        r = lprc.contents
        monitors.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
        return True

    ctypes.windll.user32.EnumDisplayMonitors(
        None, None, MONITORENUMPROC(_cb), 0
    )

    for x, y, w, h in monitors:
        if w == TARGET_WIDTH and h == TARGET_HEIGHT:
            return (x, y, w, h)

    # Fallback: first non-primary, or primary
    if len(monitors) > 1:
        return monitors[1]
    if monitors:
        return monitors[0]
    return (0, 0, TARGET_WIDTH, TARGET_HEIGHT)


# ── VU Engine ─────────────────────────────────────────────────────────────

# User-calibrated VU anchors (rms_db -> bar fraction).
# These points come from live measurements on the target mixer.
VU_DB_POINTS = [
    (-88.0, 0.00),  # floor
    (-39.0, 0.25),  # 1/4
    (-23.0, 0.50),  # mid
    (-13.0, 0.75),  # 3/4
    ( -3.8, 1.00),  # clip
]


def _db_to_norm(db: float) -> float:
    """Piecewise-linear mapping through measured mixer calibration points."""
    if db <= VU_DB_POINTS[0][0]:
        return VU_DB_POINTS[0][1]
    if db >= VU_DB_POINTS[-1][0]:
        return VU_DB_POINTS[-1][1]

    for i in range(len(VU_DB_POINTS) - 1):
        x0, y0 = VU_DB_POINTS[i]
        x1, y1 = VU_DB_POINTS[i + 1]
        if x0 <= db <= x1:
            t = (db - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)

    return 0.0

class VUEngine:
    """Captures ASIO input levels using sounddevice."""

    def __init__(self):
        self.stream = None
        self.device = None
        self.num_channels = 0
        self.rms_levels = {}
        self.peak_levels = {}
        self.peak_hold = {}
        self._rms_env = {}
        self._peak_env = {}
        self._bar_last_peak_time = {}
        self._lock = threading.Lock()
        self.running = False

    def get_audio_devices(self):
        devices = []
        try:
            api_priority = ["asio", "windows wasapi", "windows wdm-ks", "mme"]
            host_apis = sd.query_hostapis()
            for prio_name in api_priority:
                for api in host_apis:
                    if api["name"].lower().startswith(prio_name):
                        for dev_idx in api["devices"]:
                            dev = sd.query_devices(dev_idx)
                            if dev["max_input_channels"] > 0:
                                tag = api["name"].split()[0]
                                devices.append(
                                    (dev_idx, f"[{tag}] {dev['name']}",
                                     dev["max_input_channels"])
                                )
        except Exception:
            pass
        return devices

    def get_asio_devices(self):
        return self.get_audio_devices()

    def start(self, device_name=None):
        self.stop()
        devices = self.get_asio_devices()
        if not devices:
            return False

        dev_idx, max_ch = None, 0
        if device_name:
            for idx, name, ch in devices:
                if name == device_name:
                    dev_idx, max_ch = idx, ch
                    break
        if dev_idx is None:
            dev_idx, _, max_ch = devices[0]

        self.device = dev_idx
        self.num_channels = max_ch
        try:
            self.stream = sd.InputStream(
                device=dev_idx,
                channels=max_ch,
                samplerate=48000,
                blocksize=256,
                callback=self._audio_cb,
                dtype="float32",
            )
            self.stream.start()
            self.running = True
            return True
        except Exception as e:
            print(f"VU Engine start error: {e}")
            return False

    def stop(self):
        self.running = False
        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    @staticmethod
    def _env_step(prev: float, inp: float, dt: float,
                  attack_s: float, release_s: float) -> float:
        """Envelope follower with separate attack/release (MusicDSP-style)."""
        tau = attack_s if inp > prev else release_s
        if tau <= 0.0:
            return inp
        coef = math.exp(-dt / tau)
        return coef * prev + (1.0 - coef) * inp

    def _audio_cb(self, indata, frames, time_info, status):
        now = time.time()
        dt = frames / 48000.0
        vu_db_offset = 0.0
        rms_release_s = 1.2  # slower fall

        with self._lock:
            for ch in range(indata.shape[1]):
                samples = indata[:, ch]
                peak = float(np.max(np.abs(samples)))
                rms = float(np.sqrt(np.mean(samples ** 2)))
                rms_db = 20.0 * math.log10(max(rms, 1e-10)) + vu_db_offset
                peak_db = 20.0 * math.log10(max(peak, 1e-10)) + vu_db_offset

                rms_norm = _db_to_norm(rms_db)
                peak_norm = _db_to_norm(peak_db)

                # Main bar follows PEAK (better transient/crackle capture).
                prev_rms = self.rms_levels.get(ch, peak_norm)
                if peak_norm >= prev_rms:
                    self.rms_levels[ch] = peak_norm
                else:
                    self.rms_levels[ch] = self._env_step(prev_rms, peak_norm, dt, 0.0, rms_release_s)
                self.peak_levels[ch] = peak_norm

                self.peak_hold[ch] = (peak_norm, now)

    def get_level(self, asio_ch):
        with self._lock:
            rms = self.rms_levels.get(asio_ch, 0.0)
            peak = self.peak_levels.get(asio_ch, 0.0)
            hold = self.peak_hold.get(asio_ch, (0.0, 0.0))[0]
        return rms, peak, hold


# ── OSC Listener (placeholder) ───────────────────────────────────────────

class OSCListener:
    """Placeholder OSC listener for gain values."""

    def __init__(self):
        self.server = None
        self.thread = None
        self.values = {}
        self._lock = threading.Lock()
        self.running = False

    def start(self, port=9000):
        if not HAS_OSC:
            return False
        self.stop()
        try:
            disp = osc_dispatcher.Dispatcher()
            disp.set_default_handler(self._handler)
            self.server = osc_server.ThreadingOSCUDPServer(
                ("0.0.0.0", port), disp
            )
            self.thread = threading.Thread(
                target=self.server.serve_forever, daemon=True
            )
            self.thread.start()
            self.running = True
            return True
        except Exception as e:
            print(f"OSC start error: {e}")
            return False

    def stop(self):
        self.running = False
        if self.server:
            try:
                self.server.shutdown()
            except Exception:
                pass
            self.server = None
            self.thread = None

    def _handler(self, address, *args):
        if args:
            with self._lock:
                try:
                    self.values[address] = float(args[0])
                except (ValueError, TypeError):
                    pass

    def get_value(self, address):
        if not address:
            return None
        with self._lock:
            return self.values.get(address)


# ── Channel Data Model ───────────────────────────────────────────────────

class ChannelData:
    def __init__(self, idx, mixer_id):
        self.idx = idx
        self.mixer_id = mixer_id
        self.name = ""
        self.ch_id = ""
        self.input_src = ""
        self.param = ""
        self.selected = False
        self.mute = False
        self.solo = False
        self.color_code = -1
        self.is_master = idx == NUM_CH

        self.asio_ch = -1
        self.asio_ch_r = -1
        self.stereo = False
        self.vu_rms = 0.0
        self.vu_peak = 0.0
        self.vu_hold = 0.0
        self.vu_rms_r = 0.0
        self.vu_peak_r = 0.0
        self.vu_hold_r = 0.0

        self.picture_path = ""
        self.picture_pixmap = None

        self.osc_address = ""
        self.osc_gain = None

        self.color_override = None


# ── Strip Widget ──────────────────────────────────────────────────────────

class StripWidget(QWidget):
    """Custom-painted channel strip (100 x 200 px)."""

    COLOR_BAR_H = 20
    NAME_H = 16
    PICTURE_H = 75
    ROTARY_H = 52
    LCD_H = 16
    LCD_ROWS = 3

    def __init__(self, channel_data, parent=None):
        super().__init__(parent)
        self.data = channel_data
        self.setFixedSize(STRIP_WIDTH, TARGET_HEIGHT)

        self._name_font = QFont("Consolas", 16, QFont.Bold)
        self._lcd_font = QFont("Consolas", 11)
        self._rotary_font = QFont("Consolas", 8)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing, False)
        d = self.data
        w, h = self.width(), self.height()

        if d.is_master:
            bg = QColor("#2e1e1e") if not d.selected else QColor("#3a2020")
        else:
            bg = QColor(STRIP_BG_SEL if d.selected else STRIP_BG)
        p.fillRect(0, 0, w, h, bg)

        border = QColor(YELLOW) if d.selected else (
            QColor("#5a2a2a") if d.is_master else QColor(OVERLAY))
        p.setPen(QPen(border, 1))
        p.drawRect(0, 0, w - 1, h - 1)

        # VU meter on the right side, full height
        vu_total_w = 16
        vu_x_start = w - vu_total_w - 2
        vu_top = 2
        vu_bottom = h - 2
        vu_h = vu_bottom - vu_top

        if d.stereo:
            bar_w = 7
            gap = 2
            self._draw_vu_bar(p, vu_x_start, vu_top, bar_w, vu_h,
                              d.vu_rms, d.vu_hold)
            self._draw_vu_bar(p, vu_x_start + bar_w + gap, vu_top, bar_w, vu_h,
                              d.vu_rms_r, d.vu_hold_r)
        else:
            self._draw_vu_bar(p, vu_x_start, vu_top, vu_total_w, vu_h,
                              d.vu_rms, d.vu_hold)

        # Left portion for everything else
        lw = w - vu_total_w - 4
        y = 0

        # ── color bar with channel name inside ──
        left_x = 1
        content_w = lw - 1
        ch_color = self._strip_color()
        p.fillRect(left_x, 1, content_w, self.COLOR_BAR_H, ch_color)
        name = d.name or ("Mstr" if d.is_master else f"Ch{d.idx + 1}")
        p.setFont(self._name_font)
        p.setPen(QColor("#000000"))
        p.drawText(QRect(left_x, 1, content_w, self.COLOR_BAR_H), Qt.AlignCenter | Qt.TextDontClip, name)
        y += self.COLOR_BAR_H + 2

        # ── picture with liquid glass effect ──
        pic_w = content_w
        pic_h = self.PICTURE_H
        pic_rect = QRect(left_x, y, pic_w, pic_h)

        # Black base
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#0a0a0a"))
        p.drawRect(pic_rect)

        # Draw the image if present
        if d.picture_pixmap and not d.picture_pixmap.isNull():
            p.drawPixmap(pic_rect, d.picture_pixmap)

        # Glass shine - top highlight (bright white fade)
        shine_h = pic_h // 3
        shine_grad = QLinearGradient(left_x, y, left_x, y + shine_h)
        shine_grad.setColorAt(0.0, QColor(255, 255, 255, 90))
        shine_grad.setColorAt(0.4, QColor(255, 255, 255, 40))
        shine_grad.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(QBrush(shine_grad))
        p.drawRect(QRect(left_x, y, pic_w, shine_h))

        # Bottom reflection (subtle bright edge)
        refl_h = pic_h // 5
        refl_grad = QLinearGradient(left_x, y + pic_h - refl_h, left_x, y + pic_h)
        refl_grad.setColorAt(0.0, QColor(255, 255, 255, 0))
        refl_grad.setColorAt(0.7, QColor(255, 255, 255, 15))
        refl_grad.setColorAt(1.0, QColor(255, 255, 255, 35))
        p.setBrush(QBrush(refl_grad))
        p.drawRect(QRect(left_x, y + pic_h - refl_h, pic_w, refl_h))

        # Shining border edges
        edge_grad_l = QLinearGradient(left_x, y, left_x + 3, y)
        edge_grad_l.setColorAt(0.0, QColor(255, 255, 255, 50))
        edge_grad_l.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(QBrush(edge_grad_l))
        p.drawRect(QRect(left_x, y, 3, pic_h))

        edge_grad_r = QLinearGradient(left_x + pic_w - 3, y, left_x + pic_w, y)
        edge_grad_r.setColorAt(0.0, QColor(255, 255, 255, 0))
        edge_grad_r.setColorAt(1.0, QColor(255, 255, 255, 50))
        p.setBrush(QBrush(edge_grad_r))
        p.drawRect(QRect(left_x + pic_w - 3, y, 3, pic_h))

        # Top edge bright line
        p.setPen(QPen(QColor(255, 255, 255, 70), 1))
        p.drawLine(left_x, y, left_x + pic_w, y)

        # Bottom edge subtle line
        p.setPen(QPen(QColor(255, 255, 255, 25), 1))
        p.drawLine(left_x, y + pic_h - 1, left_x + pic_w, y + pic_h - 1)

        y += pic_h + 2

        # ── LCD info (3 rows: ch_id, input_src, param) ──
        row_h = self.LCD_H
        lcd_total = row_h * self.LCD_ROWS
        lcd_y = h - lcd_total - 2
        p.setFont(self._lcd_font)
        lcd_lines = [
            (d.ch_id or "", QColor(YELLOW) if d.selected else QColor(FG_DIM)),
            (d.input_src or "", QColor(FG_DIM)),
            (d.param or "", QColor(FG_DIM)),
        ]
        for text, color in lcd_lines:
            p.setPen(color)
            p.drawText(QRect(2, lcd_y, lw - 2, row_h), Qt.AlignCenter, text)
            lcd_y += row_h

        # ── 3D rotary encoder with LED ring — just above LCD ──
        rot_y = h - lcd_total - 2 - self.ROTARY_H
        rot_cx = lw // 2
        rot_cy = rot_y + self.ROTARY_H // 2
        led_r = 22
        knob_r = 16
        gain_frac = max(0.0, min(1.0, d.osc_gain)) if d.osc_gain is not None else 0.0
        gain_db   = int(round(MTX_FADER_MIN + gain_frac * (MTX_FADER_MAX - MTX_FADER_MIN)))

        start_deg = 225
        total_deg = 270
        sweep_deg = gain_frac * total_deg   # how far the arc goes

        arc_rect  = QRect(rot_cx - led_r, rot_cy - led_r, led_r * 2, led_r * 2)
        track_rect = QRect(rot_cx - led_r + 1, rot_cy - led_r + 1,
                           (led_r - 1) * 2, (led_r - 1) * 2)

        # ── dim track (full 270° background) ──
        p.setPen(QPen(QColor(30, 35, 30), 3))
        p.setBrush(Qt.NoBrush)
        p.drawArc(arc_rect, int(start_deg * 16), int(-total_deg * 16))

        # ── lit arc (exact same sweep as needle) ──
        if sweep_deg > 0.5:
            # colour: green → yellow → red as gain increases
            if gain_frac < 0.70:
                t = gain_frac / 0.70
                arc_color = QColor(int(20 + t * 60), int(140 + t * 20), 20)
            else:
                t = (gain_frac - 0.70) / 0.30
                arc_color = QColor(int(80 + t * 175), int(160 - t * 140), 10)
            p.setPen(QPen(arc_color, 3))
            p.drawArc(arc_rect, int(start_deg * 16), int(-sweep_deg * 16))

            # subtle glow
            glow_rect = QRect(rot_cx - led_r - 1, rot_cy - led_r - 1,
                              (led_r + 1) * 2, (led_r + 1) * 2)
            p.setPen(QPen(QColor(arc_color.red(), arc_color.green(), arc_color.blue(), 35), 5))
            p.drawArc(glow_rect, int(start_deg * 16), int(-sweep_deg * 16))

        # Knob body — 3D gradient
        knob_grad = QRadialGradient(rot_cx - 2, rot_cy - 2, knob_r * 1.5)
        knob_grad.setColorAt(0.0, QColor(100, 100, 120))
        knob_grad.setColorAt(0.4, QColor(60, 60, 78))
        knob_grad.setColorAt(1.0, QColor(25, 25, 35))
        p.setPen(QPen(QColor(80, 80, 100), 1))
        p.setBrush(QBrush(knob_grad))
        p.drawEllipse(rot_cx - knob_r, rot_cy - knob_r, knob_r * 2, knob_r * 2)

        # Knob highlight
        shine_grad = QRadialGradient(rot_cx - 2, rot_cy - 2, knob_r)
        shine_grad.setColorAt(0.0, QColor(255, 255, 255, 55))
        shine_grad.setColorAt(0.5, QColor(255, 255, 255, 12))
        shine_grad.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(shine_grad))
        p.drawEllipse(rot_cx - knob_r, rot_cy - knob_r, knob_r * 2, knob_r * 2)

        # dB value in centre of knob
        if d.osc_gain is not None:
            db_str = f"{gain_db:+d}"
            p.setFont(QFont("Consolas", 7, QFont.Bold))
            p.setPen(QColor(220, 220, 220, 220))
            p.drawText(QRect(rot_cx - knob_r, rot_cy - knob_r,
                             knob_r * 2, knob_r * 2),
                       Qt.AlignCenter, db_str)

        # Indicator line on knob — same angle formula as LED arc
        if d.osc_gain is not None:
            # start_deg=225 at frac=0, sweeps -270° to reach -45 at frac=1
            angle = math.radians(start_deg - gain_frac * total_deg)
            lx1 = rot_cx + int((knob_r - 5) * math.cos(angle))
            ly1 = rot_cy - int((knob_r - 5) * math.sin(angle))
            lx2 = rot_cx + int((knob_r - 1) * math.cos(angle))
            ly2 = rot_cy - int((knob_r - 1) * math.sin(angle))
            p.setPen(QPen(QColor(255, 255, 255, 200), 1))
            p.drawLine(lx1, ly1, lx2, ly2)

        p.end()

    def _draw_vu_bar(self, p, x, top, bar_w, bar_h, rms, hold):
        p.fillRect(x, top, bar_w, bar_h, QColor(VU_BG))
        if rms > 0.001:
            rms_h = int(bar_h * min(rms, 1.0))
            for py_off in range(rms_h):
                frac = py_off / max(bar_h, 1)
                r, g, b = self._vu_color(frac)
                draw_y = top + bar_h - 1 - py_off
                p.fillRect(x + 1, draw_y, bar_w - 2, 1, QColor(r, g, b))

    @staticmethod
    def _vu_color(frac):
        # Scale: 0.0 = -60 dBFS, 1.0 = -18 dBFS (0 VU)
        # frac maps pixel position bottom→top
        # green  : 0.0 – 0.57  (-60 to -36 dBFS)
        # yellow : 0.57 – 0.79 (-36 to -27 dBFS)
        # orange : 0.79 – 0.93 (-27 to -21 dBFS)
        # red    : 0.93 – 1.0  (-21 to -18 dBFS and above = clipping zone)
        if frac < 0.30:
            t = frac / 0.30
            return int(20 - 20 * t), int(60 + 140 * t), 200
        elif frac < 0.57:
            t = (frac - 0.30) / 0.27
            return 0, int(200 + 20 * t), int(200 - 160 * t)
        elif frac < 0.79:
            t = (frac - 0.57) / 0.22
            return int(180 * t), int(220 - 20 * t), int(40 - 20 * t)
        elif frac < 0.93:
            t = (frac - 0.79) / 0.14
            return int(180 + 65 * t), int(200 - 140 * t), int(20 - 10 * t)
        else:
            t = (frac - 0.93) / 0.07
            return 255, int(60 - 60 * t), 10

    def _strip_color(self):
        d = self.data
        if d.color_override:
            return QColor(d.color_override)
        if d.color_code >= 0:
            return QColor(color_for_code(d.color_code))
        if d.is_master:
            return QColor("#c03030")
        return QColor(OVERLAY)


# ── USB MIDI Device Discovery ─────────────────────────────────────────────

_FIT_VID_PID = "VID_1ACC&PID_1A91"

def _query_usb_midi_devices():
    """Query WMI for FIT USB devices. Returns list of dicts:
    [{'serial': '00000AE8', 'vid_pid': 'VID_1ACC&PID_1A91', 'device_id': '...'}, ...]"""
    try:
        ps = (
            'Get-CimInstance Win32_PnPEntity'
            ' | Where-Object { $_.DeviceID -like "*VID_1ACC&PID_1A91*"'
            ' -and $_.DeviceID -notlike "*&MI_*" }'
            ' | Select-Object -ExpandProperty DeviceID'
        )
        result = subprocess.run(
            ["powershell", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, text=True, timeout=10,
        )
        devices = []
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\\")
            if len(parts) >= 3:
                devices.append({
                    "serial": parts[2],
                    "vid_pid": parts[1] if len(parts) > 1 else "",
                    "device_id": line,
                })
        return devices
    except Exception:
        return []


def _find_midi_ports_for_serial(serial):
    """Given a USB serial, find the matching rtmidi in/out port names.
    The MIDI sub-ports share the parent's serial via the MIDII_ WMI entries."""
    try:
        ps = (
            'Get-CimInstance Win32_PnPEntity'
            ' | Where-Object { $_.DeviceID -like "*MIDII_*" -or $_.DeviceID -like "*MIDIOUT_*" }'
            ' | Select-Object Name, DeviceID'
            ' | Format-Table -AutoSize -Wrap'
        )
        result = subprocess.run(
            ["powershell", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None, None

    midi_child_ids = []
    try:
        ps2 = (
            f'Get-CimInstance Win32_PnPEntity'
            f' | Where-Object {{ $_.DeviceID -like "*VID_1ACC&PID_1A91&MI_00*" }}'
            f' | Select-Object -ExpandProperty DeviceID'
        )
        result2 = subprocess.run(
            ["powershell", "-ExecutionPolicy", "Bypass", "-Command", ps2],
            capture_output=True, text=True, timeout=10,
        )
        for line in result2.stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            midi_child_ids.append(line)
    except Exception:
        pass

    return None, None


def _resolve_serial_to_ports(serial):
    """Given a FIT serial number, find its current rtmidi in and out port names."""
    tmp_in = rtmidi.MidiIn()
    tmp_out = rtmidi.MidiOut()
    in_ports = tmp_in.get_ports()
    out_ports = tmp_out.get_ports()
    del tmp_in, tmp_out

    all_usb = _query_usb_midi_devices()
    fit_serials = [d["serial"] for d in all_usb]

    fit_in_ports = [p for p in in_ports if "FIT" in p.upper()]
    fit_out_ports = [p for p in out_ports if "FIT" in p.upper()]

    fit_in_ports.sort()
    fit_out_ports.sort()

    if serial not in fit_serials:
        return None, None

    idx = sorted(fit_serials).index(serial)

    ports_per_device = max(1, len(fit_in_ports) // max(1, len(fit_serials)))
    port_start = idx * ports_per_device

    in_name = fit_in_ports[port_start] if port_start < len(fit_in_ports) else None
    out_name = fit_out_ports[port_start] if port_start < len(fit_out_ports) else None

    return in_name, out_name


def _list_fit_devices():
    """Return list of currently connected FIT devices with serial and port info.
    Each entry: {'serial': str, 'in_port': str|None, 'out_port': str|None}"""
    devices = _query_usb_midi_devices()
    result = []
    for dev in devices:
        in_port, out_port = _resolve_serial_to_ports(dev["serial"])
        result.append({
            "serial": dev["serial"],
            "in_port": in_port,
            "out_port": out_port,
        })
    return result


# ── MIDI Bridge Unit ──────────────────────────────────────────────────────

class MIDIBridgeUnit:
    """Bridges one FIT controller to LV1 via teVirtualMIDI."""

    VPORT_NAMES = {0: "FIT Bridge 1", 1: "FIT Bridge 2"}

    def __init__(self, mixer_id, on_lv1_msg, on_vpot_turn=None):
        self.mixer_id = mixer_id
        self.on_lv1_msg = on_lv1_msg
        self.on_vpot_turn = on_vpot_turn
        self.fit_in = None
        self.fit_out = None
        self.vport = None
        self._vport_name = ""
        self.running = False
        self.log_f = None
        self.trim_mode = False
        self._user_left_trim_at = 0.0
        self._trim_toggle_pending = False
        self._connected_in  = None
        self._connected_out = None
        self._last_reopen_try = 0.0
        self._usb_serial = ""

    def init_vport(self, serial=""):
        """Create (or recreate) the virtual port. Call with serial once known."""
        self._usb_serial = serial
        if serial:
            name = f"FIT Bridge {self.mixer_id + 1} [{serial}]"
        else:
            name = self.VPORT_NAMES.get(self.mixer_id, f"FIT Bridge {self.mixer_id + 1}")
        if self._vport_name == name and self.vport:
            return
        if self.vport:
            self.vport.close()
            self.vport = None
        self._vport_name = name
        self._create_vport()

    @staticmethod
    def _clean_port_name(name):
        return re.sub(r"\s+\d+$", "", name)

    @staticmethod
    def list_midi_ports():
        tmp_in = rtmidi.MidiIn()
        tmp_out = rtmidi.MidiOut()
        ins = tmp_in.get_ports()
        outs = tmp_out.get_ports()
        del tmp_in, tmp_out
        return list(ins), list(outs)

    def _open_in(self, name):
        if not name or name == "(none)":
            return None
        tmp = rtmidi.MidiIn()
        ports = tmp.get_ports()
        del tmp
        for i, p in enumerate(ports):
            if p == name:
                port = rtmidi.MidiIn()
                port.ignore_types(sysex=False, timing=True, active_sense=True)
                port.open_port(i)
                return port
        return None

    def _open_out(self, name):
        if not name or name == "(none)":
            return None
        tmp = rtmidi.MidiOut()
        ports = tmp.get_ports()
        del tmp
        for i, p in enumerate(ports):
            if p == name:
                port = rtmidi.MidiOut()
                port.open_port(i)
                return port
        return None

    def _create_vport(self):
        from virtual_midi import VirtualMIDIPort
        port_name = self._vport_name or self.VPORT_NAMES.get(
            self.mixer_id, f"FIT Bridge {self.mixer_id + 1}")
        self.vport = VirtualMIDIPort(port_name, on_data=self._lv1_to_fit_vport)
        try:
            self.vport.open()
            print(f"M{self.mixer_id + 1}: Virtual port '{port_name}' created")
        except OSError as e:
            print(f"M{self.mixer_id + 1}: VirtualMIDI error: {e}")
            self.vport = None

    def connect(self, fit_in_name, fit_out_name):
        # Already connected with required handles alive — do nothing at all.
        want_in = bool(fit_in_name and fit_in_name != "(none)")
        want_out = bool(fit_out_name and fit_out_name != "(none)")
        if (self._connected_in == fit_in_name
                and self._connected_out == fit_out_name
                and (not want_in or self.fit_in is not None)
                and (not want_out or self.fit_out is not None)):
            self.running = True
            return
        # Port names changed — close old ones and open new ones.
        # Do NOT close the vport; LV1 must never see it disappear.
        self._close_fit_ports()
        if self.log_f:
            self.log_f.close()
            self.log_f = None

        self.fit_in = self._open_in(fit_in_name)
        self.fit_out = self._open_out(fit_out_name)
        if self.fit_in:
            self.fit_in.set_callback(self._fit_to_lv1)
        self.log_f = open(LOG_FILE, "a", encoding="utf-8")
        self.running = True
        self._connected_in  = fit_in_name
        self._connected_out = fit_out_name
        self._last_reopen_try = 0.0

    def _close_fit_ports(self):
        for p in [self.fit_in, self.fit_out]:
            if p:
                try:
                    if hasattr(p, "cancel_callback"):
                        p.cancel_callback()
                    p.close_port()
                except Exception:
                    pass
        self.fit_in = self.fit_out = None

    def disconnect(self):
        self.running = False
        self._connected_in  = None
        self._connected_out = None
        self._close_fit_ports()
        if self.log_f:
            self.log_f.close()
            self.log_f = None

    def close_vport(self):
        if self.vport:
            self.vport.close()
            self.vport = None

    def _log(self, text):
        if self.log_f:
            ts = time.strftime("%H:%M:%S")
            self.log_f.write(f"[{ts}] M{self.mixer_id + 1} {text}\n")
            self.log_f.flush()

    def _hex(self, data):
        return " ".join(f"{b:02X}" for b in data)

    def send_trim_toggle(self):
        """Send note bang 0x50 to LV1 to toggle into trim mode."""
        if self._trim_toggle_pending:
            return
        if self.vport:
            self.vport.send([0x90, 0x50, 0x7F])
            self.vport.send([0x90, 0x50, 0x00])
            self._trim_toggle_pending = True
            self._log("AUTO: sent trim toggle (90 50 7F / 90 50 00)")

    def _fit_to_lv1(self, msg_dt, _):
        msg = msg_dt[0]
        if not msg:
            return
        self._log(f"FIT\u2192LV1: {self._hex(msg)}")

        # User manually pressed mode toggle button — record timestamp
        if (len(msg) >= 3 and msg[0] == 0x90 and msg[1] == 0x50
                and msg[2] == 0x7F and self.trim_mode):
            self._user_left_trim_at = time.monotonic()

        if (self.trim_mode and len(msg) >= 3
                and (msg[0] & 0xF0) == 0xB0
                and 0x10 <= msg[1] <= 0x1F):
            strip_idx = msg[1] - 0x10
            raw_val = msg[2]
            delta = raw_val if raw_val <= 0x3F else -(raw_val - 0x40)
            if self.on_vpot_turn:
                self.on_vpot_turn(self.mixer_id, strip_idx, delta)
            return

        if self.vport:
            self.vport.send(list(msg))

    def _reopen_fit_ports(self):
        in_name = self._connected_in or ""
        out_name = self._connected_out or ""
        self._close_fit_ports()
        self.fit_in = self._open_in(in_name)
        self.fit_out = self._open_out(out_name)
        if self.fit_in:
            self.fit_in.set_callback(self._fit_to_lv1)

    def ensure_fit_ports(self, min_interval_s=5.0):
        if not self.running:
            return
        serial = getattr(self, "_usb_serial", "")
        if not serial:
            in_name = self._connected_in or ""
            out_name = self._connected_out or ""
            want_in = bool(in_name and in_name != "(none)")
            want_out = bool(out_name and out_name != "(none)")
            missing_in = want_in and self.fit_in is None
            missing_out = want_out and self.fit_out is None
            if not (missing_in or missing_out):
                return
            now = time.monotonic()
            if now - self._last_reopen_try < min_interval_s:
                return
            self._last_reopen_try = now
            self._log("FIT port missing, attempting reopen")
            self._reopen_fit_ports()
            return
        if self.fit_in is not None and self.fit_out is not None:
            return
        now = time.monotonic()
        if now - self._last_reopen_try < min_interval_s:
            return
        self._last_reopen_try = now
        self._log(f"FIT port missing, scanning for serial {serial}")
        in_port, out_port = _resolve_serial_to_ports(serial)
        if in_port or out_port:
            self._close_fit_ports()
            self.fit_in = self._open_in(in_port) if in_port else None
            self.fit_out = self._open_out(out_port) if out_port else None
            if self.fit_in:
                self.fit_in.set_callback(self._fit_to_lv1)
            self._connected_in = in_port
            self._connected_out = out_port
            self._log(f"Reconnected via serial: in={in_port}, out={out_port}")

    def _lv1_to_fit_vport(self, data):
        msg = list(data)
        if not msg:
            return
        self._log(f"LV1\u2192FIT: {self._hex(msg)}")
        if self.fit_out:
            try:
                self.fit_out.send_message(msg)
            except Exception:
                # Keep LV1 virtual port alive; only heal the FIT hardware side.
                self._log("FIT out send failed, reopening FIT ports")
                self._reopen_fit_ports()
        self.on_lv1_msg(self.mixer_id, msg)


# ── Configuration Window ──────────────────────────────────────────────────

class ChannelTile(QWidget):
    """Small clickable tile used in the channel grid."""

    clicked = pyqtSignal(int, int)          # mixer_id, strip_idx
    right_clicked = pyqtSignal(int, int)    # mixer_id, strip_idx
    TILE_W = 88
    TILE_H = 108

    _BAR_H   = 16   # colour bar + name row
    _FOOT_H  = 14   # bottom info area — single line, enough for pt-7 font
    _PAD     = 1    # border inset

    def __init__(self, data, parent=None):
        super().__init__(parent)
        self.data = data
        self.setFixedSize(self.TILE_W, self.TILE_H)
        self.setCursor(Qt.PointingHandCursor)
        self._selected = False
        self._name_font  = QFont("Segoe UI", 8, QFont.Bold)
        self._label_font = QFont("Segoe UI", 7)

    def set_selected(self, v):
        self._selected = v
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.data.mixer_id, self.data.idx)
        elif event.button() == Qt.RightButton:
            self.right_clicked.emit(self.data.mixer_id, self.data.idx)
        super().mousePressEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        d = self.data
        w, h = self.width(), self.height()
        pad = self._PAD

        # background
        p.fillRect(0, 0, w, h,
                   QColor("#2a2a40") if self._selected else QColor("#1e1e2e"))

        # border
        p.setPen(QPen(QColor(YELLOW) if self._selected else QColor(OVERLAY), 1))
        p.drawRect(0, 0, w - 1, h - 1)

        # ── colour bar ──────────────────────────────────────────────────
        if d.color_override:
            bar_col = QColor(d.color_override)
        elif d.color_code >= 0:
            bar_col = QColor(color_for_code(d.color_code))
        elif d.is_master:
            bar_col = QColor("#c03030")
        else:
            bar_col = QColor(OVERLAY)

        bar_rect_x = pad + 1
        bar_rect_w = w - 2 - 2 * pad
        p.fillRect(bar_rect_x, pad + 1, bar_rect_w, self._BAR_H, bar_col)

        name = d.name or ("Mstr" if d.is_master else f"Ch{d.idx + 1}")
        p.setFont(self._name_font)
        # pick black or white text based on bar brightness
        try:
            lum = 0.299 * bar_col.red() + 0.587 * bar_col.green() + 0.114 * bar_col.blue()
        except Exception:
            lum = 128
        p.setPen(QColor("#000000") if lum > 140 else QColor("#ffffff"))
        p.drawText(
            QRect(bar_rect_x, pad + 1, bar_rect_w, self._BAR_H),
            Qt.AlignCenter, name,
        )

        # ── picture ─────────────────────────────────────────────────────
        pic_y = pad + 1 + self._BAR_H + 1
        pic_h = h - pic_y - self._FOOT_H - pad
        pic_x = pad + 1
        pic_w = w - 2 - 2 * pad
        if d.picture_pixmap and pic_h > 4:
            scaled = d.picture_pixmap.scaled(
                pic_w, pic_h,
                Qt.KeepAspectRatioByExpanding,
                Qt.SmoothTransformation,
            )
            # centre-crop
            sx = (scaled.width()  - pic_w) // 2
            sy = (scaled.height() - pic_h) // 2
            p.drawPixmap(
                QRect(pic_x, pic_y, pic_w, pic_h),
                scaled,
                QRect(max(sx, 0), max(sy, 0), pic_w, pic_h),
            )
        else:
            p.fillRect(pic_x, pic_y, pic_w, max(pic_h, 0), QColor("#11111b"))

        # ── footer label (single line) ───────────────────────────────────
        foot_y = h - self._FOOT_H - 1   # -1 keeps 1 px above the border
        p.fillRect(pad + 1, foot_y, w - 2 - 2 * pad, self._FOOT_H, QColor("#11111b"))

        p.setFont(self._label_font)
        p.setPen(QColor(FG))

        asio_lbl    = (f"Ch {d.asio_ch + 1}" if d.asio_ch >= 0 else "Auto") + (" S" if d.stereo else "")
        footer_text = asio_lbl
        p.drawText(
            QRect(bar_rect_x, foot_y, bar_rect_w, self._FOOT_H),
            Qt.AlignCenter | Qt.AlignVCenter, footer_text,
        )


class PatchCell(QWidget):
    """Tiny clickable dot for the ASIO routing matrix."""

    clicked = pyqtSignal(int, int, bool, int)  # mixer_id, strip_idx, is_right, asio_ch

    _SZ = 22

    def __init__(self, mixer_id, strip_idx, is_right, asio_ch, parent=None):
        super().__init__(parent)
        self.mixer_id = mixer_id
        self.strip_idx = strip_idx
        self.is_right = is_right
        self.asio_ch = asio_ch
        self._active = False
        self.setFixedSize(self._SZ, self._SZ)
        self.setCursor(Qt.PointingHandCursor)

    def set_active(self, v):
        if v != self._active:
            self._active = v
            self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.mixer_id, self.strip_idx,
                              self.is_right, self.asio_ch)
        super().mousePressEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        sz = self._SZ
        cx, cy = sz // 2, sz // 2
        r = 7
        if self._active:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(ACCENT))
            p.drawEllipse(cx - r, cy - r, r * 2, r * 2)
        else:
            p.setPen(QPen(QColor(OVERLAY), 1))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(cx - r, cy - r, r * 2, r * 2)
        p.end()


class PatchRowLabel(QWidget):
    """Compact label for one row of the patch grid (color bar + name)."""

    stereo_toggled = pyqtSignal(int, int, bool)  # mixer_id, strip_idx, new_stereo

    _W = 120
    _H = 22
    _BAR_W = 6

    def __init__(self, data, is_right=False, parent=None):
        super().__init__(parent)
        self.data = data
        self.is_right = is_right
        self.setFixedSize(self._W, self._H)
        self.setCursor(Qt.PointingHandCursor)
        self._font = QFont("Segoe UI", 7, QFont.Bold)

    def contextMenuEvent(self, event):
        if not self.is_right:
            new_st = not self.data.stereo
            self.stereo_toggled.emit(self.data.mixer_id, self.data.idx, new_st)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        d = self.data
        w, h = self.width(), self.height()

        p.fillRect(0, 0, w, h, QColor("#1e1e2e"))

        if d.color_override:
            bar_col = QColor(d.color_override)
        elif d.color_code >= 0:
            bar_col = QColor(color_for_code(d.color_code))
        elif d.is_master:
            bar_col = QColor("#c03030")
        else:
            bar_col = QColor(OVERLAY)
        p.fillRect(0, 0, self._BAR_W, h, bar_col)

        p.setFont(self._font)
        p.setPen(QColor(FG))
        name = d.name or ("Mstr" if d.is_master else f"Ch{d.idx + 1}")
        suffix = ""
        if self.is_right:
            suffix = " R"
        elif d.stereo:
            suffix = " L"
        p.drawText(QRect(self._BAR_W + 4, 0, w - self._BAR_W - 8, h),
                    Qt.AlignVCenter | Qt.AlignLeft, name + suffix)
        p.end()


class PatchGrid(QWidget):
    """ASIO routing matrix: rows = strips, columns = ASIO channels."""

    patch_changed = pyqtSignal()

    def __init__(self, app_ref, parent=None):
        super().__init__(parent)
        self.app_ref = app_ref
        self._cells: list[PatchCell] = []
        self._row_labels: list[PatchRowLabel] = []
        self._header_labels: list[QLabel] = []

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._scroll)

        self._container = QWidget()
        self._grid = QGridLayout(self._container)
        self._grid.setContentsMargins(2, 2, 2, 2)
        self._grid.setSpacing(1)
        self._scroll.setWidget(self._container)

        self._num_asio = 0
        self._content_h = 0
        self.rebuild()

    def sizeHint(self):
        if self._content_h > 0:
            sb_h = self._scroll.horizontalScrollBar().sizeHint().height()
            return QSize(super().sizeHint().width(), self._content_h + sb_h + 6)
        return super().sizeHint()

    def rebuild(self):
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        self._cells.clear()
        self._row_labels.clear()
        self._header_labels.clear()

        num_asio = self.app_ref.vu_engine.num_channels
        if num_asio <= 0:
            num_asio = 64
        self._num_asio = num_asio

        hdr_font = QFont("Segoe UI", 6)

        corner = QLabel("")
        corner.setFixedSize(PatchRowLabel._W, 18)
        self._grid.addWidget(corner, 0, 0)

        for ch in range(num_asio):
            lbl = QLabel(str(ch + 1))
            lbl.setFont(hdr_font)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setFixedSize(PatchCell._SZ, 18)
            lbl.setStyleSheet(f"color: {FG_DIM}; background: transparent;")
            self._grid.addWidget(lbl, 0, ch + 1)
            self._header_labels.append(lbl)

        grid_row = 1
        for mixer_id in (0, 1):
            strips = self.app_ref.mixer_data.get(mixer_id, [])
            for s in strips:
                rl = PatchRowLabel(s, is_right=False)
                rl.stereo_toggled.connect(self._on_stereo_toggle)
                self._grid.addWidget(rl, grid_row, 0)
                self._row_labels.append(rl)

                for ch in range(num_asio):
                    cell = PatchCell(mixer_id, s.idx, False, ch)
                    cell.clicked.connect(self._on_cell_clicked)
                    self._grid.addWidget(cell, grid_row, ch + 1)
                    self._cells.append(cell)
                    if s.asio_ch == ch:
                        cell.set_active(True)
                grid_row += 1

                if s.stereo:
                    rl_r = PatchRowLabel(s, is_right=True)
                    self._grid.addWidget(rl_r, grid_row, 0)
                    self._row_labels.append(rl_r)
                    for ch in range(num_asio):
                        cell = PatchCell(mixer_id, s.idx, True, ch)
                        cell.clicked.connect(self._on_cell_clicked)
                        self._grid.addWidget(cell, grid_row, ch + 1)
                        self._cells.append(cell)
                        if s.asio_ch_r == ch:
                            cell.set_active(True)
                    grid_row += 1

            if mixer_id == 0:
                sep = QFrame()
                sep.setFrameShape(QFrame.HLine)
                sep.setStyleSheet(f"color: {OVERLAY};")
                self._grid.addWidget(sep, grid_row, 0, 1, num_asio + 1)
                grid_row += 1

        row_h = PatchCell._SZ + self._grid.spacing()
        hdr_h = 18 + self._grid.spacing()
        self._content_h = hdr_h + (grid_row - 1) * row_h + 4

    def _on_cell_clicked(self, mixer_id, strip_idx, is_right, asio_ch):
        d = self.app_ref.get_channel_data(mixer_id, strip_idx)
        if not d:
            return
        if is_right:
            if d.asio_ch_r == asio_ch:
                d.asio_ch_r = -1
            else:
                d.asio_ch_r = asio_ch
        else:
            if d.asio_ch == asio_ch:
                d.asio_ch = -1
            else:
                d.asio_ch = asio_ch
        self.app_ref.save_channel_config(d)
        self._sync_same_name(d)
        self.refresh()
        self.patch_changed.emit()

    def _sync_same_name(self, source):
        if not source.name:
            return
        for mid in (0, 1):
            for s in self.app_ref.mixer_data.get(mid, []):
                if s is source or s.name != source.name:
                    continue
                s.asio_ch = source.asio_ch
                s.asio_ch_r = source.asio_ch_r
                s.stereo = source.stereo

    def _on_stereo_toggle(self, mixer_id, strip_idx, new_stereo):
        d = self.app_ref.get_channel_data(mixer_id, strip_idx)
        if not d:
            return
        d.stereo = new_stereo
        self.app_ref.save_channel_config(d)
        self._sync_same_name(d)
        self.rebuild()
        self.patch_changed.emit()

    def refresh(self):
        for cell in self._cells:
            d = self.app_ref.get_channel_data(cell.mixer_id, cell.strip_idx)
            if not d:
                continue
            if cell.is_right:
                cell.set_active(d.asio_ch_r == cell.asio_ch)
            else:
                cell.set_active(d.asio_ch == cell.asio_ch)
        for rl in self._row_labels:
            rl.update()


class ConfigWindow(QDialog):
    """Settings popup displayed on the primary monitor."""

    def __init__(self, app_ref, parent=None):
        super().__init__(parent)
        self.app_ref = app_ref
        self.setWindowTitle("FIT VU Bridge \u2014 Configuration")
        self.setWindowFlags(self.windowFlags() | Qt.WindowMinMaxButtonsHint)
        self.setMinimumWidth(1060)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self._apply_style()

        self.current_mixer = 0
        self.current_strip = 0
        self._tiles: dict[tuple, ChannelTile] = {}
        self._build_ui()

        self._follow_timer = QTimer(self)
        self._follow_timer.timeout.connect(self._follow_tick)

    def _apply_style(self):
        self.setStyleSheet(f"""
            QDialog {{ background: {BG}; color: {FG}; }}
            QGroupBox {{
                background: #24243a; border: 1px solid {OVERLAY};
                border-radius: 4px; margin-top: 14px; padding-top: 18px;
                color: {ACCENT}; font-weight: bold;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin; left: 10px; padding: 0 4px;
            }}
            QLabel {{ color: {FG}; }}
            QComboBox, QLineEdit, QSpinBox {{
                background: #1e1e2e; color: {FG}; border: 1px solid {OVERLAY};
                padding: 4px; border-radius: 3px;
            }}
            QScrollArea {{ border: none; background: transparent; }}
            QPushButton {{
                background: {OVERLAY}; color: {FG}; border: none;
                padding: 6px 16px; border-radius: 3px; font-weight: bold;
            }}
            QPushButton:hover {{ background: {ACCENT}; color: #1e1e2e; }}
        """)

    # ── build ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 8)
        root.setSpacing(8)

        # ── MIDI ports (collapsible, collapsed by default) ────────────
        toggle_ss = f"""
            QPushButton {{
                background: #24243a; color: {ACCENT}; border: 1px solid {OVERLAY};
                border-radius: 4px; padding: 6px 12px; font-weight: bold;
                text-align: left;
            }}
            QPushButton:hover {{ background: {OVERLAY}; }}
        """
        self._midi_toggle = QPushButton("▶  MIDI Ports")
        self._midi_toggle.setStyleSheet(toggle_ss)
        self._midi_toggle.clicked.connect(lambda: self._toggle_section(
            self._midi_toggle, self._midi_container, "MIDI Ports"))
        root.addWidget(self._midi_toggle)

        self._midi_container = QWidget()
        midi_lay = QGridLayout(self._midi_container)
        midi_lay.setSpacing(6)
        self._fit_combos: dict[int, QComboBox] = {}
        self._fit_serial_labels: dict[int, QLabel] = {}
        self._fit_dots: dict[int, StatusDot] = {}

        for mid in (1, 2):
            mixer_id = mid - 1
            vport_name = MIDIBridgeUnit.VPORT_NAMES.get(mixer_id, f"FIT Bridge {mid}")
            row = mixer_id

            dot = StatusDot()
            self._fit_dots[mixer_id] = dot

            lbl = QLabel(f"FIT {mid}  (LV1: \"{vport_name}\"):")
            combo = QComboBox()
            combo.setMinimumWidth(250)
            self._fit_combos[mixer_id] = combo

            serial_lbl = QLabel("")
            serial_lbl.setStyleSheet(f"color: {FG_DIM}; font-size: 10px;")
            self._fit_serial_labels[mixer_id] = serial_lbl

            midi_lay.addWidget(dot, row, 0)
            midi_lay.addWidget(lbl, row, 1)
            midi_lay.addWidget(combo, row, 2)
            midi_lay.addWidget(serial_lbl, row, 3)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self._btn_refresh  = AnimatedButton("⟳  Scan Devices",   flash_color=ACCENT,
                                             confirm_text="⟳  Scanned", confirm_ms=800)
        self._btn_connect  = AnimatedButton("▶  Connect All",    flash_color=GREEN,
                                             confirm_text="▶  Connected",  confirm_ms=1000)
        self._btn_disconn  = AnimatedButton("■  Disconnect All", flash_color=RED,
                                             confirm_text="■  Disconnected", confirm_ms=1000)
        self._btn_refresh.clicked.connect(self._do_refresh_ports)
        self._btn_connect.clicked.connect(self._do_connect_midi)
        self._btn_disconn.clicked.connect(self._do_disconnect_midi)
        self._chk_auto_trim = QCheckBox("Auto Trim/Gain Toggle")
        self._chk_auto_trim.setToolTip("Automatically switch back to Trim/Gain mode when Pan or other mode is detected")
        self._chk_auto_trim.setChecked(self.app_ref.config.get("auto_trim_toggle", True))
        self._chk_auto_trim.stateChanged.connect(self._on_auto_trim_changed)
        for b in (self._btn_refresh, self._btn_connect, self._btn_disconn):
            btn_row.addWidget(b)
        btn_row.addWidget(self._chk_auto_trim)
        btn_row.addStretch()
        midi_lay.addLayout(btn_row, 2, 0, 1, 4)
        self._midi_container.setVisible(False)
        root.addWidget(self._midi_container)

        # ── Virtual Ports (collapsible, collapsed by default) ─────────
        self._vport_toggle = QPushButton("▶  Virtual MIDI Ports")
        self._vport_toggle.setStyleSheet(toggle_ss)
        self._vport_toggle.clicked.connect(lambda: self._toggle_section(
            self._vport_toggle, self._vport_container, "Virtual MIDI Ports"))
        root.addWidget(self._vport_toggle)

        self._vport_container = QWidget()
        vport_lay = QVBoxLayout(self._vport_container)
        vport_lay.setContentsMargins(0, 4, 0, 0)
        vport_lay.setSpacing(4)

        self._vport_list_widget = QVBoxLayout()
        vport_lay.addLayout(self._vport_list_widget)

        add_row = QHBoxLayout()
        add_row.setSpacing(6)
        self._vport_name_input = QLineEdit()
        self._vport_name_input.setPlaceholderText("Enter port name (e.g. Stream Deck)")
        self._vport_name_input.setMinimumWidth(250)
        add_row.addWidget(self._vport_name_input)
        self._btn_add_vport = AnimatedButton("+  Add Port", flash_color=GREEN,
                                              confirm_text="✓  Added", confirm_ms=800)
        self._btn_add_vport.clicked.connect(self._do_add_vport)
        add_row.addWidget(self._btn_add_vport)
        add_row.addStretch()
        vport_lay.addLayout(add_row)

        self._vport_container.setVisible(False)
        root.addWidget(self._vport_container)
        self._rebuild_vport_list()

        # ── ASIO + Stagetec (collapsible, collapsed by default) ──────
        self._asio_toggle = QPushButton("▶  ASIO / Stagetec")
        self._asio_toggle.setStyleSheet(toggle_ss)
        self._asio_toggle.clicked.connect(lambda: self._toggle_section(
            self._asio_toggle, self._asio_container, "ASIO / Stagetec"))
        root.addWidget(self._asio_toggle)

        self._asio_container = QWidget()
        mid_row = QHBoxLayout(self._asio_container)
        mid_row.setContentsMargins(0, 4, 0, 0)
        mid_row.setSpacing(8)

        asio_grp = QGroupBox("ASIO Device")
        asio_lay = QHBoxLayout(asio_grp)
        asio_lay.addWidget(QLabel("Device:"))
        self.asio_combo = QComboBox()
        self.asio_combo.setMinimumWidth(240)
        asio_lay.addWidget(self.asio_combo)
        self.asio_combo.currentIndexChanged.connect(self._asio_device_changed)
        mid_row.addWidget(asio_grp, 3)

        stg_grp = QGroupBox("Stagetec / MTX")
        stg_lay = QGridLayout(stg_grp)
        stg_lay.setSpacing(6)

        stg_cfg = self.app_ref.config.get("stagetec", {})
        stg_lay.addWidget(QLabel("MTX IP:"), 0, 0)
        self._stg_ip = QLineEdit(stg_cfg.get("mtx_ip", MTX_IP))
        self._stg_ip.setMinimumWidth(130)
        stg_lay.addWidget(self._stg_ip, 0, 1)

        stg_lay.addWidget(QLabel("MTX Port:"), 0, 2)
        self._stg_port = QSpinBox()
        self._stg_port.setRange(1, 65535)
        self._stg_port.setValue(int(stg_cfg.get("mtx_port", MTX_PORT)))
        self._stg_port.setMinimumWidth(70)
        stg_lay.addWidget(self._stg_port, 0, 3)

        stg_lay.addWidget(QLabel("Local IP:"), 1, 0)
        self._stg_local = QLineEdit(stg_cfg.get("local_ip", MTX_LOCAL_IP))
        stg_lay.addWidget(self._stg_local, 1, 1)

        stg_dot_row = QHBoxLayout()
        self._stg_dot = StatusDot()
        stg_dot_row.addWidget(self._stg_dot)
        stg_dot_row.addWidget(QLabel("Live"))
        stg_dot_row.addStretch()
        stg_lay.addLayout(stg_dot_row, 1, 2, 1, 2)

        self._btn_stg_apply = AnimatedButton("Apply & Restart",
                                              flash_color=GREEN,
                                              confirm_text="✓ Restarted",
                                              confirm_ms=1200)
        self._btn_stg_apply.clicked.connect(self._do_stagetec_apply)
        stg_lay.addWidget(self._btn_stg_apply, 2, 0, 1, 4)
        mid_row.addWidget(stg_grp, 3)
        self._asio_container.setVisible(False)
        root.addWidget(self._asio_container)

        # ── channel tile grid ─────────────────────────────────────────────
        tile_grp = QGroupBox("Channel Grid")
        tile_outer = QVBoxLayout(tile_grp)
        tile_outer.setContentsMargins(6, 18, 6, 6)

        # 4 rows × 9 columns per mixer, two mixers side by side
        COLS = 9
        tile_container = QWidget()
        tile_container.setStyleSheet(f"background: {BG};")
        tile_grid = QGridLayout(tile_container)
        tile_grid.setContentsMargins(4, 4, 4, 4)
        tile_grid.setSpacing(4)

        for mixer_id in (0, 1):
            col_offset = mixer_id * (COLS + 1)   # +1 for the divider column
            if mixer_id == 1:
                for r in range(4):
                    sep = QFrame()
                    sep.setFrameShape(QFrame.VLine)
                    sep.setStyleSheet(f"color: {OVERLAY};")
                    tile_grid.addWidget(sep, r, COLS)

            for strip_idx in range(STRIPS_PER_MIXER):
                row = strip_idx // COLS
                col = col_offset + (strip_idx % COLS)
                d = self.app_ref.mixer_data[mixer_id][strip_idx]
                tile = ChannelTile(d)
                tile.clicked.connect(self._tile_browse_picture)
                tile.right_clicked.connect(self._tile_browse_preset)
                self._tiles[(mixer_id, strip_idx)] = tile
                tile_grid.addWidget(tile, row, col)

        tile_outer.addWidget(tile_container)
        root.addWidget(tile_grp)

        # ── ASIO patch grid (collapsible) ────────────────────────────────
        self._patch_toggle = QPushButton("▶  ASIO Patch Grid  (right-click row label for stereo)")
        self._patch_toggle.setStyleSheet(f"""
            QPushButton {{
                background: #24243a; color: {ACCENT}; border: 1px solid {OVERLAY};
                border-radius: 4px; padding: 6px 12px; font-weight: bold;
                text-align: left;
            }}
            QPushButton:hover {{ background: {OVERLAY}; }}
        """)
        # ── ASIO patch grid (collapsible) ────────────────────────────────
        self._patch_toggle.clicked.connect(self._toggle_patch_grid)
        root.addWidget(self._patch_toggle)

        self._patch_container = QWidget()
        patch_outer = QVBoxLayout(self._patch_container)
        patch_outer.setContentsMargins(0, 4, 0, 0)
        self.patch_grid = PatchGrid(self.app_ref)
        self.patch_grid.patch_changed.connect(self._on_patch_changed)
        patch_outer.addWidget(self.patch_grid)
        self._patch_container.setVisible(False)
        root.addWidget(self._patch_container)

        # ── footer ────────────────────────────────────────────────────────
        sep_line = QFrame()
        sep_line.setFrameShape(QFrame.HLine)
        sep_line.setStyleSheet(f"color: {OVERLAY};")
        root.addWidget(sep_line)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        footer.addStretch()
        self._btn_load = AnimatedButton("📂  Load",
                                         flash_color=ACCENT,
                                         confirm_text="✓  Loaded",
                                         confirm_ms=1200)
        self._btn_load.setMinimumWidth(120)
        self._btn_load.clicked.connect(self._do_load)
        footer.addWidget(self._btn_load)
        self._btn_save = AnimatedButton("💾  Save",
                                         flash_color=GREEN,
                                         confirm_text="✓  Saved",
                                         confirm_ms=1200)
        self._btn_save.setMinimumWidth(120)
        self._btn_save.clicked.connect(self._do_save)
        footer.addWidget(self._btn_save)
        self._btn_save_as = AnimatedButton("💾  Save As…",
                                            flash_color=GREEN,
                                            confirm_text="✓  Saved",
                                            confirm_ms=1200)
        self._btn_save_as.setMinimumWidth(120)
        self._btn_save_as.clicked.connect(self._do_save_as)
        footer.addWidget(self._btn_save_as)
        self._loaded_config_path = None
        root.addLayout(footer)

    # ── public ─────────────────────────────────────────────────────────────

    def show_for_strip(self, mixer_id, strip_idx):
        self.current_mixer = mixer_id
        self.current_strip = strip_idx
        self._refresh_ports()
        self._refresh_asio()
        if not self._follow_timer.isActive():
            self._follow_timer.start(200)
        scr = QApplication.primaryScreen()
        if scr:
            g = scr.availableGeometry()
            self.setGeometry(g)
        self.showMaximized()
        self.raise_()

    # ── tile ───────────────────────────────────────────────────────────────

    def _tile_browse_picture(self, mixer_id, strip_idx):
        d = self.app_ref.get_channel_data(mixer_id, strip_idx)
        if not d:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Channel Picture", PICTURES_DIR,
            "Images (*.png *.jpg *.jpeg *.bmp *.gif)",
        )
        if path:
            dest = os.path.join(PICTURES_DIR, os.path.basename(path))
            if os.path.normcase(os.path.abspath(path)) != os.path.normcase(os.path.abspath(dest)):
                shutil.copy2(path, dest)
            d.picture_path = dest
            d.picture_pixmap = _load_pixmap(dest)
            self.app_ref.save_channel_config(d)

    def _tile_browse_preset(self, mixer_id, strip_idx):
        d = self.app_ref.get_channel_data(mixer_id, strip_idx)
        if not d:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Preset", PRESETS_DIR,
            "Images (*.png *.jpg *.jpeg *.bmp *.gif)",
        )
        if path:
            d.picture_path = path
            d.picture_pixmap = _load_pixmap(path)
            self.app_ref.save_channel_config(d)

    # ── tick (200 ms) ──────────────────────────────────────────────────────

    def _follow_tick(self):
        if not self.isVisible():
            return
        # FIT status dots
        for mid_idx, dot in self._fit_dots.items():
            b = self.app_ref.midi_bridges.get(mid_idx)
            ok = b is not None and b.fit_in is not None and b.fit_out is not None
            dot.set_ok(ok)
        # Stagetec live dot (received in last 35 s)
        age = time.monotonic() - self.app_ref._mtx_reader.last_rx
        self._stg_dot.set_ok(age < 35.0)
        # refresh tiles and patch grid (picture / name changes)
        for tile in self._tiles.values():
            tile.update()
        if self._patch_container.isVisible():
            self.patch_grid.refresh()

    # ── internal ───────────────────────────────────────────────────────────

    def _toggle_section(self, btn, container, label):
        container.setVisible(not container.isVisible())
        arrow = "▼" if container.isVisible() else "▶"
        btn.setText(f"{arrow}  {label}")
        self._fit_to_content()

    def _toggle_patch_grid(self):
        expanding = not self._patch_container.isVisible()
        self._patch_container.setVisible(expanding)
        arrow = "▼" if expanding else "▶"
        self._patch_toggle.setText(f"{arrow}  ASIO Patch Grid  (right-click row label for stereo)")
        if expanding:
            self.patch_grid.refresh()
        self._fit_to_content()

    def _fit_to_content(self):
        self.setMinimumHeight(0)
        self.setMaximumHeight(16777215)
        QApplication.processEvents()
        hint = self.sizeHint()
        scr = QApplication.primaryScreen()
        max_h = scr.availableGeometry().height() - 20 if scr else 2000
        h = min(hint.height(), max_h)
        self.resize(self.width(), h)
        self._center_on_screen()

    def _center_on_screen(self):
        scr = QApplication.primaryScreen()
        if not scr:
            return
        g = scr.availableGeometry()
        x = g.x() + (g.width() - self.width()) // 2
        y = g.y() + (g.height() - self.height()) // 2
        self.move(max(x, g.x()), max(y, g.y()))

    def _position_on_primary(self):
        self.adjustSize()
        scr = QApplication.primaryScreen()
        if scr:
            g = scr.availableGeometry()
            x = g.x() + (g.width() - self.width()) // 2
            y = g.y() + (g.height() - self.height()) // 2
            self.move(x, y)

    def _refresh_ports(self):
        self._cached_fit_devices = _list_fit_devices()
        cfg = self.app_ref.config.get("midi", {})
        for mixer_id, combo in self._fit_combos.items():
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("(none)", "")
            for dev in self._cached_fit_devices:
                label = f"FIT  [serial: {dev['serial']}]"
                if dev["in_port"]:
                    label += f"  —  {dev['in_port']}"
                combo.addItem(label, dev["serial"])
            saved_serial = cfg.get(f"fit{mixer_id + 1}_serial", "")
            if saved_serial:
                for i in range(combo.count()):
                    if combo.itemData(i) == saved_serial:
                        combo.setCurrentIndex(i)
                        break
            self._fit_serial_labels[mixer_id].setText(
                f"saved: {saved_serial}" if saved_serial else "")
            combo.blockSignals(False)

    def _refresh_asio(self):
        self.asio_combo.blockSignals(True)
        self.asio_combo.clear()
        for idx, name, ch in self.app_ref.vu_engine.get_asio_devices():
            self.asio_combo.addItem(f"{name} ({ch}ch)", idx)
        saved = self.app_ref.config.get("asio", {}).get("device", "")
        if saved:
            for i in range(self.asio_combo.count()):
                if saved in self.asio_combo.itemText(i):
                    self.asio_combo.setCurrentIndex(i)
                    break
        self.asio_combo.blockSignals(False)

    # ── button handlers ────────────────────────────────────────────────────

    def _do_refresh_ports(self):
        self._refresh_ports()
        self._btn_refresh.flash()

    def _do_connect_midi(self):
        cfg_midi = self.app_ref.config.get("midi", {})
        for mixer_id, combo in self._fit_combos.items():
            serial = combo.currentData()
            cfg_midi[f"fit{mixer_id + 1}_serial"] = serial or ""
            self._fit_serial_labels[mixer_id].setText(
                f"saved: {serial}" if serial else "")
        self.app_ref.config["midi"] = cfg_midi
        self.app_ref.save_config()
        try:
            self.app_ref.connect_midi()
            self._btn_connect.flash(color=GREEN, confirm_text="▶  Connected")
        except Exception as e:
            self._btn_connect.flash(color=RED, confirm_text="✗  Error")
            QMessageBox.warning(self, "MIDI Connection Error", str(e))

    def _do_disconnect_midi(self):
        self.app_ref.disconnect_midi()
        self._btn_disconn.flash(color=RED, confirm_text="■  Disconnected")

    def _on_auto_trim_changed(self, state):
        enabled = state == Qt.Checked
        self.app_ref.config["auto_trim_toggle"] = enabled
        self.app_ref.save_config()

    def _rebuild_vport_list(self):
        while self._vport_list_widget.count():
            item = self._vport_list_widget.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                while item.layout().count():
                    sub = item.layout().takeAt(0)
                    if sub.widget():
                        sub.widget().deleteLater()

        port_names = self.app_ref.config.get("extra_vports", [])
        for i, name in enumerate(port_names):
            row = QHBoxLayout()
            row.setSpacing(6)
            lbl = QLabel(f"  ●  {name}")
            lbl.setStyleSheet(f"color: {FG}; font-size: 12px;")
            row.addWidget(lbl)
            rm_btn = AnimatedButton("✕  Remove", flash_color=RED,
                                     confirm_text="✓  Removed", confirm_ms=600)
            rm_btn.setFixedWidth(100)
            rm_btn.clicked.connect(lambda checked, idx=i: self._do_remove_vport(idx))
            row.addWidget(rm_btn)
            row.addStretch()
            container = QWidget()
            container.setLayout(row)
            self._vport_list_widget.addWidget(container)

    def _do_add_vport(self):
        name = self._vport_name_input.text().strip()
        if not name:
            return
        ports = self.app_ref.config.get("extra_vports", [])
        if name in ports:
            QMessageBox.warning(self, "Duplicate", f"Port '{name}' already exists.")
            return
        ports.append(name)
        self.app_ref.config["extra_vports"] = ports
        self.app_ref.save_config()
        self.app_ref._create_extra_vports()
        self._vport_name_input.clear()
        self._rebuild_vport_list()
        self._btn_add_vport.flash(color=GREEN, confirm_text="✓  Added")

    def _do_remove_vport(self, idx):
        ports = self.app_ref.config.get("extra_vports", [])
        if 0 <= idx < len(ports):
            ports.pop(idx)
            self.app_ref.config["extra_vports"] = ports
            self.app_ref.save_config()
            self.app_ref._create_extra_vports()
            self._rebuild_vport_list()

    def _do_stagetec_apply(self):
        ip    = self._stg_ip.text().strip()
        port  = self._stg_port.value()
        local = self._stg_local.text().strip()
        self.app_ref.config["stagetec"] = {
            "mtx_ip": ip, "mtx_port": port, "local_ip": local,
        }
        self.app_ref.save_config()
        try:
            self.app_ref._mtx_reader.restart(local, ip, port)
            self._btn_stg_apply.flash(color=GREEN, confirm_text="✓ Restarted")
        except Exception:
            self._btn_stg_apply.flash(color=RED, confirm_text="✗ Error")

    def _asio_device_changed(self):
        text = self.asio_combo.currentText()
        if not text:
            return
        name = text.rsplit(" (", 1)[0] if " (" in text else text
        self.app_ref.config.setdefault("asio", {})["device"] = name
        self.app_ref.vu_engine.start(name)
        self.patch_grid.rebuild()

    def _do_load(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Configuration", SCRIPT_DIR,
            "JSON Config (*.json)")
        if not path:
            return
        try:
            with open(path, "r") as f:
                loaded = json.load(f)
            self.app_ref.config = loaded
            self.app_ref.save_config()
            self.app_ref._load_channel_configs()
            self._refresh_ports()
            self._refresh_asio()
            self.patch_grid.rebuild()
            self._loaded_config_path = path
            self._btn_load.flash(color=ACCENT, confirm_text="✓  Loaded")
        except Exception as e:
            QMessageBox.warning(self, "Load Error", f"Failed to load config:\n{e}")

    def _do_save(self):
        if not self._loaded_config_path:
            self._do_save_as()
            return
        try:
            self.app_ref.save_config()
            with open(self._loaded_config_path, "w") as f:
                json.dump(self.app_ref.config, f, indent=2)
            self._btn_save.flash(color=GREEN, confirm_text="✓  Saved")
        except Exception as e:
            QMessageBox.warning(self, "Save Error", f"Failed to save config:\n{e}")

    def _do_save_as(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Configuration As", SCRIPT_DIR,
            "JSON Config (*.json)")
        if not path:
            return
        try:
            self.app_ref.save_config()
            with open(path, "w") as f:
                json.dump(self.app_ref.config, f, indent=2)
            self._loaded_config_path = path
            self._btn_save_as.flash(color=GREEN, confirm_text="✓  Saved")
        except Exception as e:
            QMessageBox.warning(self, "Save Error", f"Failed to save config:\n{e}")

    def _on_patch_changed(self):
        pass

    def closeEvent(self, event):
        self._follow_timer.stop()
        super().closeEvent(event)


def _load_pixmap(path):
    if path and os.path.exists(path):
        pm = QPixmap(path)
        if not pm.isNull():
            pic_w = STRIP_WIDTH - 22 - 2
            return pm.scaled(pic_w, StripWidget.PICTURE_H, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    return None


# ── Main Window ───────────────────────────────────────────────────────────

class StripDisplay(QMainWindow):
    """Borderless fullscreen display on the 3840x200 monitor."""

    midi_msg = pyqtSignal(int, list)

    def __init__(self):
        super().__init__()

        self.config = self._load_config()

        self.mixer_data = {
            0: [ChannelData(i, 0) for i in range(STRIPS_PER_MIXER)],
            1: [ChannelData(i, 1) for i in range(STRIPS_PER_MIXER)],
        }

        self.vu_engine = VUEngine()
        self.osc_listener = OSCListener()
        self.midi_bridges = {
            0: MIDIBridgeUnit(0, self._on_lv1_msg, self._on_vpot_turn),
            1: MIDIBridgeUnit(1, self._on_lv1_msg, self._on_vpot_turn),
        }
        cfg_midi = self.config.get("midi", {})
        for mixer_id in (0, 1):
            serial = cfg_midi.get(f"fit{mixer_id + 1}_serial", "")
            self.midi_bridges[mixer_id].init_vport(serial)

        self._extra_vports: list = []
        self._create_extra_vports()

        self.strip_widgets = {}
        self.config_window = None
        self._gain_cache = {}
        self._mtx_channel_gains: dict[int, int] = {}  # physical_ch → dB
        self._vpot_last_touched: dict[int, float] = {}  # physical_ch → monotonic time
        self._last_midi_port_check = 0.0

        self._mtx_gain_seq = 0x2000

        self._build_ui()
        self._position_window()
        self._load_channel_configs()

        self.midi_msg.connect(self._parse_lv1_msg)

        self._timer = QTimer()
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)

        if self.config.get("midi"):
            self.connect_midi()

        saved_asio = self.config.get("asio", {}).get("device", "")
        self._vu_retries = 0
        self._vu_max_retries = 10
        if saved_asio:
            if not self.vu_engine.start(saved_asio):
                self._start_vu_retry_timer(saved_asio)

        # Start MTX reader with saved or default addresses
        stg = self.config.get("stagetec", {})
        self._mtx_reader = MTXGainReader(
            on_gain=self._on_mtx_gain,
            local_ip=stg.get("local_ip", MTX_LOCAL_IP),
            mtx_ip=stg.get("mtx_ip", MTX_IP),
            mtx_port=int(stg.get("mtx_port", MTX_PORT)),
        )
        self._mtx_reader.start()

    def _start_vu_retry_timer(self, device_name):
        self._vu_retry_device = device_name
        self._vu_retry_timer = QTimer()
        self._vu_retry_timer.setSingleShot(True)
        self._vu_retry_timer.timeout.connect(self._vu_retry_tick)
        self._vu_retry_timer.start(2000)

    def _vu_retry_tick(self):
        self._vu_retries += 1
        sd._terminate()
        sd._initialize()
        if self.vu_engine.start(self._vu_retry_device):
            print(f"VU Engine started on retry {self._vu_retries}")
            return
        if self._vu_retries < self._vu_max_retries:
            self._vu_retry_timer.start(2000)
        else:
            print("VU Engine: gave up after max retries, select device manually in settings")

    # ── UI ──

    def _build_ui(self):
        central = QWidget()
        central.setStyleSheet(f"background: {BG};")
        self.setCentralWidget(central)

        layout = QHBoxLayout(central)
        layout.setContentsMargins(13, 0, 0, 0)
        layout.setSpacing(0)

        for mixer_id in (0, 1):
            if mixer_id == 1:
                gap = QWidget()
                gap.setFixedWidth(250)
                gap.setStyleSheet(f"background: {BG};")
                gap_mid_layout = QVBoxLayout(gap)
                gap_mid_layout.setContentsMargins(0, 0, 0, 0)
                gap_mid_layout.setSpacing(2)
                gap_mid_layout.addStretch()

                lv1_lbl = QLabel("LV1")
                lv1_lbl.setAlignment(Qt.AlignCenter)
                lv1_lbl.setStyleSheet(f"""
                    color: {ACCENT}; background: transparent;
                    font-family: 'Segoe UI'; font-size: 60px; font-weight: bold;
                    letter-spacing: 6px;
                """)
                gap_mid_layout.addWidget(lv1_lbl)

                powered_lbl = QLabel("powered by")
                powered_lbl.setAlignment(Qt.AlignCenter)
                powered_lbl.setStyleSheet(f"""
                    color: {FG_DIM}; background: transparent;
                    font-family: 'Segoe UI'; font-size: 14px;
                """)
                gap_mid_layout.addWidget(powered_lbl)

                stg_lbl = QLabel("STAGETEC")
                stg_lbl.setAlignment(Qt.AlignCenter)
                stg_lbl.setStyleSheet(f"""
                    color: {FG}; background: transparent;
                    font-family: 'Segoe UI'; font-size: 22px; font-weight: bold;
                    letter-spacing: 5px;
                """)
                gap_mid_layout.addWidget(stg_lbl)

                gap_mid_layout.addStretch()
                layout.addWidget(gap)

            for i in range(STRIPS_PER_MIXER):
                sw = StripWidget(self.mixer_data[mixer_id][i])
                layout.addWidget(sw)
                self.strip_widgets[(mixer_id, i)] = sw

        trailing_gap = QWidget()
        trailing_gap.setStyleSheet(f"background: {BG};")
        trailing_gap.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        gap_layout = QVBoxLayout(trailing_gap)
        gap_layout.setContentsMargins(0, 20, 0, 20)
        gap_layout.setSpacing(8)

        gap_layout.addStretch()

        settings_btn = AnimatedButton("\u2699 Settings", flash_color=ACCENT)
        settings_btn.setFixedSize(120, 50)
        settings_btn.setStyleSheet(f"""
            QPushButton {{
                background: {OVERLAY}; color: {FG}; border: none;
                padding: 4px 8px; border-radius: 4px; font-size: 12px; font-weight: bold;
            }}
            QPushButton:hover {{ background: {ACCENT}; color: #1e1e2e; }}
        """)
        settings_btn.clicked.connect(self._open_settings)
        gap_layout.addWidget(settings_btn, 0, Qt.AlignHCenter)

        quit_btn = AnimatedButton("\u2716 Quit", flash_color=RED)
        quit_btn.setFixedSize(120, 50)
        quit_btn.setStyleSheet(f"""
            QPushButton {{
                background: {OVERLAY}; color: {FG}; border: none;
                padding: 4px 8px; border-radius: 4px; font-size: 12px; font-weight: bold;
            }}
            QPushButton:hover {{ background: {RED}; color: #1e1e2e; }}
        """)
        quit_btn.clicked.connect(self.close)
        gap_layout.addWidget(quit_btn, 0, Qt.AlignHCenter)

        self._windowed_btn = AnimatedButton("⊞ Window", flash_color=ACCENT)
        self._windowed_btn.setFixedSize(120, 50)
        self._windowed_btn.setStyleSheet(f"""
            QPushButton {{
                background: {OVERLAY}; color: {FG}; border: none;
                padding: 4px 8px; border-radius: 4px; font-size: 12px; font-weight: bold;
            }}
            QPushButton:hover {{ background: {ACCENT}; color: #1e1e2e; }}
        """)
        self._windowed_btn.clicked.connect(self._toggle_windowed)
        gap_layout.addWidget(self._windowed_btn, 0, Qt.AlignHCenter)
        self._is_windowed = False

        gap_layout.addStretch()

        layout.addWidget(trailing_gap)

    def _position_window(self):
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        x, y, w, h = find_strip_display()
        self.setGeometry(x, y, w, h)

    def _toggle_windowed(self):
        self._is_windowed = not self._is_windowed
        if self._is_windowed:
            self.setWindowFlags(Qt.Window)
            self.setMinimumSize(800, 180)
            self.resize(1200, 200)
            self._windowed_btn.setText("⊟ Fullscreen")
            self.show()
        else:
            self._position_window()
            self._windowed_btn.setText("⊞ Window")
            self.show()

    def _open_settings(self):
        if not self.config_window:
            self.config_window = ConfigWindow(self, parent=None)
        self.config_window.show_for_strip(0, 0)


    # ── MIDI ──

    def connect_midi(self):
        cfg = self.config.get("midi", {})
        errors = []
        for mixer_id in (0, 1):
            mid = mixer_id + 1
            serial = cfg.get(f"fit{mid}_serial", "")
            if not serial:
                continue
            self.midi_bridges[mixer_id].init_vport(serial)
            try:
                in_port, out_port = _resolve_serial_to_ports(serial)
                if in_port or out_port:
                    self.midi_bridges[mixer_id].connect(
                        in_port or "", out_port or "",
                    )
                    self.midi_bridges[mixer_id]._usb_serial = serial
                else:
                    print(f"FIT {mid}: serial {serial} not found, will retry")
                    self.midi_bridges[mixer_id]._usb_serial = serial
                    self.midi_bridges[mixer_id].running = True
            except Exception as e:
                errors.append(f"FIT {mid}: {e}")
        if errors:
            QMessageBox.warning(
                self, "MIDI Connection Error",
                "Failed to connect:\n\n" + "\n".join(errors),
            )

    def disconnect_midi(self):
        for b in self.midi_bridges.values():
            b.disconnect()

    def _create_extra_vports(self):
        from virtual_midi import VirtualMIDIPort
        self._close_extra_vports()
        port_names = self.config.get("extra_vports", [])
        for name in port_names:
            if not name:
                continue
            try:
                vp = VirtualMIDIPort(name)
                vp.open()
                self._extra_vports.append(vp)
                print(f"Extra virtual port '{name}' created")
            except OSError as e:
                print(f"Extra virtual port '{name}' failed: {e}")

    def _close_extra_vports(self):
        for vp in self._extra_vports:
            try:
                vp.close()
            except Exception:
                pass
        self._extra_vports.clear()

    def _on_lv1_msg(self, mixer_id, msg):
        self.midi_msg.emit(mixer_id, msg)

    def _parse_lv1_msg(self, mixer_id, msg):
        if not msg:
            return
        status = msg[0]
        strips = self.mixer_data[mixer_id]

        # SysEx
        if status == 0xF0 and len(msg) > 4 and msg[1:4] == [0x00, 0x00, 0x74]:
            self._parse_sysex(mixer_id, msg)
            return

        kind = status & 0xF0

        # NoteOff ch2 (0x81): strip LED color
        if status == 0x81 and len(msg) >= 3:
            strip_idx = msg[1]
            if strip_idx < STRIPS_PER_MIXER:
                strips[strip_idx].color_code = msg[2]

        # Note On — solo indicators
        if kind == 0x90 and len(msg) >= 3:
            note, vel = msg[1], msg[2]
            if 0x10 <= note <= 0x17:
                idx = note - 0x10
                if idx < STRIPS_PER_MIXER:
                    strips[idx].solo = vel > 0

    def _parse_sysex(self, mixer_id, msg):
        if len(msg) < 8 or msg[5] != 0x1A:
            return
        strips = self.mixer_data[mixer_id]
        cmd = msg[6]

        if cmd != 0x01:
            return
        subcmd = msg[7] if len(msg) > 7 else 0

        if subcmd == 0x04 and len(msg) > 14:
            self._parse_full_scribble(strips, msg)
            self._detect_trim_mode(mixer_id, strips)
        elif subcmd == 0x00 and len(msg) >= 19:
            self._parse_partial_update(strips, msg)
            self._detect_trim_mode(mixer_id, strips)

    def _parse_full_scribble(self, strips, msg):
        raw = msg[11:-1] if msg[-1] == 0xF7 else msg[11:]
        text = bytes(raw).decode("ascii", errors="replace")

        for i in range(STRIPS_PER_MIXER):
            offset = i * CHARS_PER_STRIP
            if offset + CHARS_PER_STRIP > len(text):
                break
            chunk = text[offset:offset + CHARS_PER_STRIP]
            s = strips[i]
            old_name = s.name

            if chunk[:6] == "Select":
                s.selected = True
                s.name = chunk[7:14].strip()
            else:
                s.selected = False
                s.name = chunk[7:14].strip()

            s.input_src = chunk[14:21].strip()
            s.ch_id = chunk[21:28].strip()
            s.param = chunk[28:35].strip()

            if s.name != old_name:
                old_proxy = type("D", (), {
                    "name": old_name, "osc_gain": s.osc_gain,
                    "picture_path": s.picture_path,
                    "color_override": s.color_override,
                    "osc_address": s.osc_address,
                    "stereo": s.stereo, "asio_ch_r": s.asio_ch_r,
                    "asio_ch": s.asio_ch,
                })()
                self._save_gain_to_cache(old_proxy)
                self.save_channel_config(old_proxy)
                self._apply_channel_config(s)

    def _parse_partial_update(self, strips, msg):
        raw_idx = msg[9]
        field = msg[10]
        text_bytes = msg[11:-1] if msg[-1] == 0xF7 else msg[11:]
        text = bytes(text_bytes[:7]).decode("ascii", errors="replace")

        if raw_idx == 0x7F:
            return
        strip_idx = raw_idx - 1
        if strip_idx < 0 or strip_idx >= STRIPS_PER_MIXER:
            return

        s = strips[strip_idx]
        old_name = s.name

        if field == 0x01:
            s.selected = text.strip().startswith("Select")
        elif field == 0x02:
            s.name = text.strip()
        elif field == 0x03:
            s.input_src = text.strip()
        elif field == 0x04:
            s.ch_id = text.strip()
        elif field == 0x05:
            s.param = text.strip()

        if s.name != old_name:
            old_proxy = type("D", (), {
                "name": old_name, "osc_gain": s.osc_gain,
                "picture_path": s.picture_path,
                "color_override": s.color_override,
                "osc_address": s.osc_address,
                "stereo": s.stereo, "asio_ch_r": s.asio_ch_r,
                "asio_ch": s.asio_ch,
            })()
            self._save_gain_to_cache(old_proxy)
            self.save_channel_config(old_proxy)
            self._apply_channel_config(s)

    _TRIM_GRACE_SEC = 60

    def _detect_trim_mode(self, mixer_id, strips):
        bridge = self.midi_bridges[mixer_id]
        has_any_param = False
        for s in strips:
            if s.param:
                has_any_param = True
                if s.param.lower().startswith(("trim", "gain")):
                    bridge.trim_mode = True
                    bridge._trim_toggle_pending = False
                    return
        if not has_any_param:
            return
        bridge.trim_mode = False
        if not self.config.get("auto_trim_toggle", True):
            return
        if bridge._trim_toggle_pending:
            return
        if bridge._user_left_trim_at > 0:
            elapsed = time.monotonic() - bridge._user_left_trim_at
            if elapsed >= self._TRIM_GRACE_SEC:
                bridge.send_trim_toggle()
        else:
            bridge.send_trim_toggle()

    # ── MTX gain integration ──

    def _on_mtx_gain(self, physical_ch: int, db: int):
        """Called from MTXGainReader thread whenever a channel gain arrives."""
        # If user just turned this vpot, ignore MTX echo for 0.5 s
        if time.monotonic() - self._vpot_last_touched.get(physical_ch, 0) < 0.5:
            return
        self._mtx_channel_gains[physical_ch] = db
        frac = _mtx_db_to_frac(db)
        for mixer_id in (0, 1):
            for strip in self.mixer_data[mixer_id]:
                if self._strip_mtx_ch(strip) == physical_ch:
                    strip.osc_gain = frac

    def _strip_mtx_ch(self, strip) -> int:
        """Extract Stagetec physical channel number from strip scribble.
        LV1 sends e.g. name='1 MD 1' or 'MD 1' — number after 'MD' is the
        Stagetec mic input. Only matches if 'MD' prefix is present — no fallback,
        so strips without MD label are never touched by MTX gain updates.
        Returns -1 if not found.
        """
        for field in (strip.name, strip.input_src, strip.ch_id):
            if field:
                m = re.search(r"MD\s*(\d+)", field, re.IGNORECASE)
                if m:
                    return int(m.group(1))
        return -1

    # ── V-Pot interception ──

    def _on_vpot_turn(self, mixer_id, strip_idx, delta):
        """Handle intercepted V-Pot turn in trim mode — send to MTX."""
        strips = self.mixer_data.get(mixer_id, [])
        if not (0 <= strip_idx < len(strips)):
            return
        d = strips[strip_idx]
        ch = self._strip_mtx_ch(d)
        if ch <= 0:
            return

        # Use stored integer dB directly — avoids float round-trip drift
        current_db = self._mtx_channel_gains.get(ch,
                     _mtx_frac_to_db(d.osc_gain) if d.osc_gain is not None else 0)

        # delta is already ±1 per detent from FIT encoding — use it directly
        # cap large jumps (noise/acceleration) to ±3 dB per message
        step = max(-3, min(3, delta))
        new_db = max(MTX_FADER_MIN, min(MTX_FADER_MAX, current_db + step))

        # Update stored gains and visual immediately
        self._mtx_channel_gains[ch] = new_db
        self._vpot_last_touched[ch] = time.monotonic()
        d.osc_gain = _mtx_db_to_frac(new_db)
        self._save_gain_to_cache(d)
        self._mtx_reader.send_fader(ch, new_db)

    # ── channel config persistence ──

    def _save_gain_to_cache(self, data):
        if data.name and data.osc_gain is not None:
            self._gain_cache[data.name] = data.osc_gain

    def _apply_channel_config(self, data):
        if not data.name:
            return
        channels = self.config.get("channels", {})
        ch_cfg = channels.get(data.name, {})

        pic = ch_cfg.get("picture", "")
        data.picture_path = pic
        data.picture_pixmap = _load_pixmap(pic) if pic else None

        data.color_override = ch_cfg.get("color_override")
        data.osc_address = ch_cfg.get("osc_address", "")
        data.stereo = ch_cfg.get("stereo", False)
        data.asio_ch_r = ch_cfg.get("asio_ch_r", -1)

        asio_map = self.config.get("asio", {}).get("channel_map", {})
        data.asio_ch = asio_map.get(data.name, -1)

        data.osc_gain = self._gain_cache.get(data.name)

    def save_channel_config(self, data):
        if not data.name:
            return
        channels = self.config.setdefault("channels", {})
        channels[data.name] = {
            "picture": data.picture_path,
            "color_override": data.color_override,
            "osc_address": data.osc_address,
            "stereo": data.stereo,
            "asio_ch_r": data.asio_ch_r,
        }
        asio_map = self.config.setdefault("asio", {}).setdefault("channel_map", {})
        if data.asio_ch >= 0:
            asio_map[data.name] = data.asio_ch
        else:
            asio_map.pop(data.name, None)
        self.save_config()

    def get_channel_data(self, mixer_id, strip_idx):
        strips = self.mixer_data.get(mixer_id, [])
        if 0 <= strip_idx < len(strips):
            return strips[strip_idx]
        return None

    def _auto_map_asio(self, data):
        if data.asio_ch >= 0:
            return data.asio_ch
        name = (data.name or "").lower()
        m = re.search(r"(\d+)", name)
        if m:
            return int(m.group(1)) - 1
        return -1

    # ── tick ──

    def _tick(self):
        now = time.monotonic()
        if now - self._last_midi_port_check >= 5.0:
            for bridge in self.midi_bridges.values():
                bridge.ensure_fit_ports()
                if (self.config.get("auto_trim_toggle", True)
                        and not bridge.trim_mode
                        and not bridge._trim_toggle_pending
                        and bridge._user_left_trim_at > 0
                        and now - bridge._user_left_trim_at >= self._TRIM_GRACE_SEC):
                    bridge.send_trim_toggle()
            self._last_midi_port_check = now

        for mixer_id in (0, 1):
            for strip in self.mixer_data[mixer_id]:
                asio_ch = self._auto_map_asio(strip)
                if asio_ch >= 0 and self.vu_engine.running:
                    rms, peak, hold = self.vu_engine.get_level(asio_ch)
                    strip.vu_rms = rms
                    strip.vu_peak = peak
                    strip.vu_hold = hold
                    if strip.stereo and strip.asio_ch_r >= 0:
                        rms_r, peak_r, hold_r = self.vu_engine.get_level(strip.asio_ch_r)
                        strip.vu_rms_r = rms_r
                        strip.vu_peak_r = peak_r
                        strip.vu_hold_r = hold_r
                if strip.osc_address and self.osc_listener.running:
                    val = self.osc_listener.get_value(strip.osc_address)
                    if val is not None:
                        strip.osc_gain = val
                elif self._mtx_channel_gains:
                    # Apply stored MTX gain for this strip if name is known,
                    # but not while user is actively turning the vpot
                    ch = self._strip_mtx_ch(strip)
                    if (ch > 0 and ch in self._mtx_channel_gains
                            and time.monotonic() - self._vpot_last_touched.get(ch, 0) >= 0.5):
                        strip.osc_gain = _mtx_db_to_frac(self._mtx_channel_gains[ch])

                key = (mixer_id, strip.idx)
                if key in self.strip_widgets:
                    self.strip_widgets[key].update()

    # ── config I/O ──

    def _load_config(self):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def save_config(self):
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(self.config, f, indent=2)
        except OSError:
            pass

    def _load_channel_configs(self):
        for mixer_id in (0, 1):
            for strip in self.mixer_data[mixer_id]:
                self._apply_channel_config(strip)

    # ── events ──

    def closeEvent(self, event):
        if self.config_window:
            self.config_window.close()
            self.config_window = None
        self._mtx_reader.stop()
        self.vu_engine.stop()
        self.osc_listener.stop()
        self._close_extra_vports()
        self.save_config()
        QApplication.instance().quit()
        super().closeEvent(event)

    def keyPressEvent(self, event):
        super().keyPressEvent(event)


# ── Entry Point ───────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(BG))
    palette.setColor(QPalette.WindowText, QColor(FG))
    palette.setColor(QPalette.Base, QColor("#1e1e2e"))
    palette.setColor(QPalette.AlternateBase, QColor("#24243a"))
    palette.setColor(QPalette.Text, QColor(FG))
    palette.setColor(QPalette.Button, QColor(OVERLAY))
    palette.setColor(QPalette.ButtonText, QColor(FG))
    palette.setColor(QPalette.Highlight, QColor(ACCENT))
    app.setPalette(palette)

    window = StripDisplay()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
