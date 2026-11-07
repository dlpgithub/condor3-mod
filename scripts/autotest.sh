#!/bin/bash
# Helpers for driving Condor from the shell (uses the remote control thread in the mod: postfx/cmd.txt)
#   autotest.sh launch            start Condor (kills a running one)
#   autotest.sh cmd "line" ...    send command lines (see src/postfx_hook.c) and wait for them to be consumed
#   autotest.sh shot out.png      screenshot of the whole screen, scaled to 1200 px wide
GAME=/Users/lennard/Condor3
export CX_ROOT=/Applications/CrossOver.app/Contents/SharedSupport/CrossOver
case "$1" in
  launch)
    pkill -f Condor.exe; sleep 2
    cd $GAME && (nohup $CX_ROOT/bin/wine --bottle Condor3 Condor.exe > /tmp/condor_run.log 2>&1 &)
    sleep 20 ;;
  cmd)
    shift; printf '%s\n' "$@" > $GAME/postfx/cmd.txt
    for i in $(seq 1 120); do [ -f $GAME/postfx/cmd.txt ] || break; sleep 0.5; done; sleep 1 ;;
  shot)
    screencapture -x /tmp/_shot.png && sips -Z 1200 /tmp/_shot.png --out "$2" >/dev/null ;;
esac
case "$1" in
  startflight)   # launch, open Free Flight, press Start flight, wait until the frame hook runs
    "$0" launch; "$0" cmd "mclick 137 212"; sleep 14; "$0" cmd "mclick 991 668"
    for i in $(seq 1 40); do sleep 3; "$0" cmd "state" >/dev/null; grep -q "flying=1" <(tail -3 $GAME/postfx/postfx.log) && break; done
    sleep 3 ;;
esac
