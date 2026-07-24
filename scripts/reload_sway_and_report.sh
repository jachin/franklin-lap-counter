#!/bin/sh
# Reload sway config and report the current output mode (run as franklin on the Pi).
SWAYSOCK=$(ls /run/user/*/sway-ipc.*.sock 2>/dev/null | head -1)
export SWAYSOCK
echo "SWAYSOCK=$SWAYSOCK"
swaymsg reload
sleep 2
swaymsg -t get_outputs --raw | grep -E '"name"|"current_mode"|"width"|"height"|"refresh"' | head -20
