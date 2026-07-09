#!/bin/bash
# Double-clickable launcher: opens Terminal and runs the optimizer
# interactively so non-terminal folks can use it too.
cd "$(dirname "$0")"
./optimize-mac.sh
echo
read -r -p "Press Return to close this window."
