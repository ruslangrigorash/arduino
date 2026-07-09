# De-Feedback Mac Optimizer

One-shot tool that applies the official De-Feedback low-latency Mac settings
guide (v1.1.4 plugin, Mac M4 Mini / macOS Tahoe / Logic Pro) automatically —
free, per the BOSS MAN.

Reference results from the guide (M4 Mini base model, Logic Pro @ 48 kHz,
32-sample buffer): up to 8 mono De-Feedback instances with no issues,
12–16 if the machine is left alone while passing audio.
Total system latency: Dante DVS @ 4 ms → 6.8 ms, RedNet TNX @ 0.25 ms → 2.4 ms,
Focusrite Scarlett → 4.0 ms.

## What's in here

| File | Purpose |
|---|---|
| `optimize-mac.sh` | Main script. Applies every setting macOS exposes to the command line, then prints a checklist of the few UI-only leftovers. |
| `De-Feedback Optimizer.command` | Double-clickable version of the above for non-terminal users. |
| `start-logic.applescript` | The auto-boot script from the guide (launches Logic Pro and brings it frontmost). |
| `install-login-script.sh` | Compiles the AppleScript into `/Applications/Start Logic.app` and adds it to Login Items. |

## Usage

```bash
cd defeedback-mac-optimizer
chmod +x optimize-mac.sh install-login-script.sh   # first time only

./optimize-mac.sh --dry-run   # see what it would do
./optimize-mac.sh             # interactive, asks per section
./optimize-mac.sh --yes       # apply everything, no questions

./install-login-script.sh     # optional: auto-boot into Logic Pro
```

You'll be asked for your administrator password (needed for pmset, firewall,
software update, etc.). Reboot after running.

Every run writes a log to `~/Library/Logs/DeFeedbackOptimizer/`, including the
**previous value of each changed setting**, so you can revert anything by hand.

## What gets automated

- **Wi-Fi off**, **Bluetooth off**, **Firewall off**
- **Network**: Ethernet moved to the top of the service order; Ethernet
  configured 1000baseT full-duplex (no flow control), MTU 1500
- **Energy**: Low Power Mode off, never sleep, disks never sleep,
  Wake for network access off, auto-restart after power failure on, Power Nap off
- **Software Update**: all automatic checking/downloading/installing off
- **AirDrop** set to No One, **AirPlay Receiver** off
- **Spotlight** indexing off on all volumes
- **Siri** off
- **Desktop & Dock**: open-app indicators on, close windows when quitting on,
  all Hot Corners cleared, Stage Manager off, desktop widgets off,
  menu bar never auto-hides
- **Wallpaper** set to solid black, **screen saver** never starts
- **Lock Screen**: never require password after sleep/screen saver
- **Privacy**: analytics/diagnostics sharing off, Location Services off
- **Users**: Guest account off, optional automatic login as your admin user

## What's still manual (Apple provides no command line for these)

The script prints this checklist at the end, but in short: AVB/EAV mode,
Continuity/iPhone Mirroring, Login Items background-activity toggles,
Accessibility/Appearance tweaks, Apple Intelligence, Displays > Advanced,
Notifications, Focus/Do Not Disturb, Screen Time, FileVault/Lockdown Mode,
and all the **Logic Pro settings** (Startup action: Open Most Recent Project;
Processing threads: 4; Process Buffer Range: Small; Multithreading:
Playback & Live Tracks; Summing: Standard Precision 32-bit).

RedNet TNX users: you must also boot into macOS Recovery and select
**Reduced Security** in Startup Security Utility — that cannot be scripted.

## Auto-boot into Logic Pro

`./install-login-script.sh` builds `Start Logic.app` from the guide's
AppleScript and adds it to Login Items. Then:

1. System Settings → Privacy & Security → **Accessibility**: enable *Start Logic*.
2. System Settings → Privacy & Security → **Automation**: allow *Start Logic*
   to control *System Events*.

If you ever edit the script and reinstall, **remove and re-add** it in
Accessibility, or macOS will silently block it (as the guide warns).

## Notes / safety

- Nothing is destructive; the log records old values for manual rollback.
- Some `defaults` keys move between macOS versions. The script never aborts on
  a failed key — it keeps going and anything it couldn't set is covered by the
  checklist. Verify the checklist items after a reboot.
- Turning off the firewall, FileVault, and the lock-screen password trades
  security for performance. Do this on a dedicated show machine, not your
  personal laptop.
