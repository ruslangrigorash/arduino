#!/bin/bash
#
# De-Feedback Mac Optimizer
# -------------------------
# Applies the "De-Feedback low-latency Mac" settings guide automatically.
# Based on the settings shared by the De-Feedback dev team (Devin Sheets):
# Mac M4 Mini / macOS Tahoe / Logic Pro / Dante DVS / RedNet TNX / Scarlett.
#
# Usage:
#   ./optimize-mac.sh            interactive (asks before each section)
#   ./optimize-mac.sh --yes      apply everything without asking
#   ./optimize-mac.sh --dry-run  show what would be done, change nothing
#
# Everything that macOS exposes to the command line is automated.
# Settings that Apple only allows through the System Settings UI are
# printed as a manual checklist at the end.

set -o pipefail

DRY_RUN=0
ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --yes|-y)  ASSUME_YES=1 ;;
    --help|-h)
      grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -20
      exit 0
      ;;
  esac
done

if [[ "$(uname)" != "Darwin" ]]; then
  echo "This tool only runs on macOS." >&2
  exit 1
fi

LOG_DIR="$HOME/Library/Logs/DeFeedbackOptimizer"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/optimize-$(date +%Y%m%d-%H%M%S).log"

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
note()  { printf '  \033[33m%s\033[0m\n' "$*"; }
log()   { echo "$*" >> "$LOG_FILE"; }

# Run a command, echoing it first. Honors --dry-run. Never aborts the
# whole script on failure (some keys differ between macOS versions).
run() {
  echo "  \$ $*"
  log "RUN: $*"
  if [[ $DRY_RUN -eq 0 ]]; then
    "$@" 2>&1 | sed 's/^/    /' | tee -a "$LOG_FILE"
  fi
}

# defaults write wrapper that records the previous value in the log so
# you can manually revert later if needed.
set_default() {  # set_default [sudo] <domain> <key> <type> <value...>
  local use_sudo=""
  if [[ "$1" == "sudo" ]]; then use_sudo="sudo"; shift; fi
  local domain="$1" key="$2"; shift 2
  local old
  old=$($use_sudo defaults read "$domain" "$key" 2>/dev/null || echo "<unset>")
  log "PREV: $domain $key = $old"
  run $use_sudo defaults write "$domain" "$key" "$@"
}

ask_section() {
  echo
  bold "== $1 =="
  if [[ $ASSUME_YES -eq 1 ]]; then return 0; fi
  read -r -p "   Apply this section? [Y/n] " reply
  [[ "$reply" =~ ^[Nn] ]] && return 1
  return 0
}

bold "De-Feedback Mac Optimizer"
echo "Log (with previous values for reverting): $LOG_FILE"
[[ $DRY_RUN -eq 1 ]] && note "DRY RUN - nothing will actually be changed."

if [[ $DRY_RUN -eq 0 ]]; then
  echo
  echo "Some sections need administrator rights; you may be asked for your password."
  sudo -v || { echo "sudo is required for the system-level sections." >&2; }
fi

# ---------------------------------------------------------------- Wi-Fi
if ask_section "Wi-Fi: off"; then
  WIFI_DEV=$(networksetup -listallhardwareports | awk '/Wi-Fi|AirPort/{getline; print $2; exit}')
  if [[ -n "$WIFI_DEV" ]]; then
    run networksetup -setairportpower "$WIFI_DEV" off
  else
    note "No Wi-Fi interface found - skipping."
  fi
fi

# ------------------------------------------------------------ Bluetooth
if ask_section "Bluetooth: off"; then
  if command -v blueutil >/dev/null 2>&1; then
    run blueutil -p 0
  else
    set_default sudo /Library/Preferences/com.apple.Bluetooth ControllerPowerState -int 0
    run sudo pkill -HUP bluetoothd
    note "Tip: 'brew install blueutil' gives more reliable Bluetooth control."
  fi
fi

# ------------------------------------------------- Network service order
if ask_section "Network: Ethernet first in service order"; then
  # Build a new order with all Ethernet-ish services on top.
  ORDERED=()
  ETHERNET_FIRST=()
  REST=()
  while IFS= read -r svc; do
    svc="${svc#\*}"
    [[ -z "$svc" ]] && continue
    if [[ "$svc" == *Ethernet* || "$svc" == *LAN* || "$svc" == *Dante* || "$svc" == *RedNet* ]]; then
      ETHERNET_FIRST+=("$svc")
    else
      REST+=("$svc")
    fi
  done < <(networksetup -listallnetworkservices | tail -n +2)
  ORDERED=("${ETHERNET_FIRST[@]}" "${REST[@]}")
  if [[ ${#ETHERNET_FIRST[@]} -gt 0 ]]; then
    run networksetup -ordernetworkservices "${ORDERED[@]}"
  else
    note "No Ethernet service found - skipping service order."
  fi
fi

# ------------------------------------------- Ethernet hardware settings
if ask_section "Ethernet: 1000baseT full-duplex (no flow control), MTU 1500"; then
  ETH_DEV=$(networksetup -listallhardwareports | awk '/^Hardware Port: (Ethernet|.*LAN)/{getline; print $2; exit}')
  if [[ -n "$ETH_DEV" ]]; then
    run networksetup -setMTU "$ETH_DEV" 1500
    run sudo networksetup -setmedia "$ETH_DEV" "1000baseT" "full-duplex"
    note "If setmedia fails on your adapter, set Speed/Duplex manually in"
    note "System Settings > Network > Ethernet > Hardware (Configure: Manually)."
  else
    note "No Ethernet interface found - skipping."
  fi
  note "AVB/EAV mode has no command line switch - see manual checklist."
fi

# --------------------------------------------------------------- Firewall
if ask_section "Firewall: off"; then
  run sudo /usr/libexec/ApplicationFirewall/socketfilterfw --setglobalstate off
fi

# ----------------------------------------------------------------- Energy
if ask_section "Energy: never sleep, no low power, restart after power failure"; then
  run sudo pmset -a lowpowermode 0    # Low Power Mode: off
  run sudo pmset -a sleep 0           # Prevent automatic sleeping: on
  run sudo pmset -a displaysleep 0    # Turn display off when inactive: Never
  run sudo pmset -a disksleep 0       # Put hard disks to sleep: off
  run sudo pmset -a womp 0            # Wake for network access: off
  run sudo pmset -a autorestart 1     # Start up after power failure: on
  run sudo pmset -a powernap 0        # Power Nap: off
fi

# -------------------------------------------------------- Software Update
if ask_section "Software Update: all automatic updates off"; then
  set_default sudo /Library/Preferences/com.apple.SoftwareUpdate AutomaticCheckEnabled -bool false
  set_default sudo /Library/Preferences/com.apple.SoftwareUpdate AutomaticDownload -bool false
  set_default sudo /Library/Preferences/com.apple.SoftwareUpdate AutomaticallyInstallMacOSUpdates -bool false
  set_default sudo /Library/Preferences/com.apple.SoftwareUpdate ConfigDataInstall -bool false
  set_default sudo /Library/Preferences/com.apple.SoftwareUpdate CriticalUpdateInstall -bool false
  set_default sudo /Library/Preferences/com.apple.commerce AutoUpdate -bool false
fi

# ----------------------------------------------------- AirDrop / AirPlay
if ask_section "AirDrop: No One / AirPlay Receiver: off"; then
  set_default com.apple.sharingd DiscoverableMode -string "Off"
  run killall sharingd
  # Yes, Apple really did misspell "Receiver" in this key.
  set_default com.apple.controlcenter AirplayRecieverEnabled -bool false
  note "Continuity / iPhone Mirroring toggles are UI-only - see manual checklist."
fi

# --------------------------------------------------------------- Spotlight
if ask_section "Spotlight: indexing off on all volumes"; then
  run sudo mdutil -a -i off
fi

# --------------------------------------------------- Siri / Apple Intelligence
if ask_section "Siri: off"; then
  set_default com.apple.assistant.support "Assistant Enabled" -bool false
  set_default com.apple.Siri StatusMenuVisible -bool false
  note "Apple Intelligence itself must be disabled in System Settings - see checklist."
fi

# ------------------------------------------------------- Desktop and Dock
if ask_section "Desktop & Dock: indicators on, close windows on quit, no hot corners, Stage Manager off, widgets off"; then
  set_default com.apple.dock show-process-indicators -bool true
  set_default NSGlobalDomain NSQuitAlwaysKeepsWindows -bool false
  for corner in tl tr bl br; do
    set_default com.apple.dock "wvous-${corner}-corner" -int 1     # 1 = disabled
    set_default com.apple.dock "wvous-${corner}-modifier" -int 0
  done
  set_default com.apple.WindowManager GloballyEnabled -bool false       # Stage Manager off
  set_default com.apple.WindowManager StandardHideDesktopIcons -bool false  # Show items on Desktop
  set_default com.apple.WindowManager StandardHideWidgets -bool true        # Desktop widgets off
  set_default com.apple.dock mru-spaces -bool false
  set_default NSGlobalDomain _HIHideMenuBar -bool false   # Never auto-hide menu bar
  run killall Dock
fi

# ------------------------------------------------- Wallpaper / Screen saver
if ask_section "Wallpaper: solid black / Screen saver: never"; then
  BLACK_PNG="/Users/Shared/defeedback-black.png"
  if [[ $DRY_RUN -eq 0 ]]; then
    osascript -l JavaScript >>"$LOG_FILE" 2>&1 <<'JXA'
ObjC.import("AppKit");
const size = $.NSMakeSize(64, 64);
const img = $.NSImage.alloc.initWithSize(size);
img.lockFocus;
$.NSColor.blackColor.setFill;
$.NSRectFill($.NSMakeRect(0, 0, 64, 64));
img.unlockFocus;
const rep = $.NSBitmapImageRep.imageRepWithData(img.TIFFRepresentation);
rep.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $.NSDictionary.dictionary)
   .writeToFileAtomically("/Users/Shared/defeedback-black.png", true);
JXA
  fi
  echo "  (generated solid black image at $BLACK_PNG)"
  run osascript -e "tell application \"System Events\" to set picture of every desktop to \"$BLACK_PNG\""
  run defaults -currentHost write com.apple.screensaver idleTime -int 0   # Start screen saver: Never
fi

# --------------------------------------------------------------- Lock Screen
if ask_section "Lock Screen: never require password after sleep/screensaver"; then
  set_default com.apple.screensaver askForPassword -int 0
  set_default com.apple.screensaver askForPasswordDelay -int 0
  note "Recent macOS may ignore these keys; if so set it in"
  note "System Settings > Lock Screen (see manual checklist)."
fi

# ------------------------------------------------- Privacy: analytics etc.
if ask_section "Privacy: analytics & improvements off, Location Services off"; then
  set_default sudo "/Library/Application Support/CrashReporter/DiagnosticMessagesHistory.plist" AutoSubmit -bool false
  set_default sudo "/Library/Application Support/CrashReporter/DiagnosticMessagesHistory.plist" ThirdPartyDataSubmit -bool false
  run sudo defaults write /var/db/locationd/Library/Preferences/ByHost/com.apple.locationd LocationServicesEnabled -int 0
  run sudo pkill locationd
  note "If Location Services stays on (SIP protects it on some versions),"
  note "turn it off in System Settings > Privacy & Security."
fi

# ------------------------------------------------------------ Users & login
if ask_section "Users: Guest account off"; then
  run sudo sysadminctl -guestAccount off
fi

if ask_section "Automatically log in as this user after restart (asks for your login password)"; then
  if [[ $DRY_RUN -eq 0 ]]; then
    echo "  \$ sudo sysadminctl -autologin set -userName $USER -password <hidden>"
    sudo sysadminctl -autologin set -userName "$USER" -password - 2>&1 | sed 's/^/    /'
  else
    echo "  \$ sudo sysadminctl -autologin set -userName $USER -password <hidden>"
  fi
  note "Requires FileVault to be OFF (which this guide recommends anyway)."
fi

# ----------------------------------------------------------- Manual checklist
echo
bold "== Done. Manual checklist (Apple gives no command line for these) =="
cat <<'CHECKLIST'

  System Settings:
  [ ] Network > Ethernet > Hardware > AVB/EAV mode: off
  [ ] General > AirDrop & Continuity: Continuity, Widgets & iPhone Mirroring: off
  [ ] General > Login Items & Extensions: App Background Activity - audio apps ON,
      Extensions > Sharing: all off
  [ ] Accessibility: turn off everything possible
  [ ] Appearance: "Tint window background with wallpaper" off
  [ ] Apple Intelligence & Siri: all off (Siri toggle is set, verify AI is off)
  [ ] Displays > Advanced: all off; Night Shift: off
  [ ] Keyboard/Desktop & Dock > Shortcuts: all set to "-"
  [ ] Notifications: Show previews Never, all app notifications off
  [ ] Focus > Do Not Disturb: ON, no notifications allowed, delete all
      schedules and other Focuses, "Share across devices" off
  [ ] Screen Time: off
  [ ] Lock Screen: verify "Require password" is Never and
      "Turn display off when inactive" is Never
  [ ] Privacy & Security > FileVault: off, Lockdown Mode: off,
      Background Security Improvements: off, Advanced: all off
  [ ] Login password: set one (blank passwords cause issues)

  RedNet TNX only:
  [ ] Boot into macOS Recovery > Startup Security Utility > "Reduced Security"

  Logic Pro (Settings > ...):
  [ ] General > Startup action: Open Most Recent Project
  [ ] Audio > Processing threads: 4
  [ ] Audio > Process Buffer Range: Small
  [ ] Audio > Multithreading: Playback & Live Tracks
  [ ] Audio > Summing: Standard Precision (32-bit)
  [ ] Project: 48 kHz sample rate, I/O buffer 32 samples

  Auto-boot into Logic:
  [ ] Run ./install-login-script.sh (in this folder) - it compiles and
      installs the "Start Logic" login app, then follow its instructions
      to allow it in Privacy & Security > Accessibility and Automation.

CHECKLIST
echo "A full log, including previous values of changed settings, is at:"
echo "  $LOG_FILE"
echo
echo "Reboot when you're done with the checklist. Enjoy - free, per the BOSS MAN."
