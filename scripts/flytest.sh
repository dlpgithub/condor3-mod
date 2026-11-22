#!/bin/bash
# flytest.sh [views_file] : start Condor with the pilot's current Flightplan.fpl (Free Flight), wait for the simulation, pause,
# then visit the camera positions of the views file (lines: "<shotname> <west> <north> <alt>", absolute world position) and take screenshots
# into <game>/postfx/shots/<shotname>.png. Uses the remote-control thread of the mod (postfx/cmd.txt).
GAME=/Users/lennard/Condor3; cd $GAME || exit 1
export CX_ROOT=/Applications/CrossOver.app/Contents/SharedSupport/CrossOver
shot(){ screencapture -x /tmp/_s.png && sips -Z ${2:-1100} /tmp/_s.png --out postfx/shots/$1.png >/dev/null; }
pkill -f Condor.exe; sleep 4
(nohup $CX_ROOT/bin/wine --bottle Condor3 Condor.exe > /tmp/condor_run.log 2>&1 &)
sleep 25; echo "mclick 137 212" > postfx/cmd.txt; sleep 12; shot planner
echo "mclick 991 668" > postfx/cmd.txt; sleep 5
for i in $(seq 1 40); do sleep 3; grep -q "ENTERED SIMULATION" Logs/Logfile.txt && break; pgrep -f Condor.exe >/dev/null || { echo "CONDOR DIED"; tail -5 Logs/Logfile.txt; exit 1; }; done
sleep 4; printf 'kkey P\nmove 0 0 0\n' > postfx/cmd.txt; sleep 2.5; shot start
# views file lines: "<shotname> <west> <north> <alt>" = ABSOLUTE world position (x west, y north, z up, metres).
# The glider drifts whenever the sim runs, so the current position is read back before every move.
[ -f "$1" ] && while read name tw tn tz; do
  [ -z "$name" ] && continue
  echo "move 0 0 0" > postfx/cmd.txt; sleep 1.2
  d=$(grep "move:" postfx/postfx.log | tail -1 | awk -v w=$tw -v n=$tn -v z=$tz '{printf "%.2f %.2f %.2f", w-$(NF-2), n-$(NF-1), z-$NF}')
  printf 'move %s\nkkey P\nkkey P\n' "$d" > postfx/cmd.txt; sleep 3.2; shot $name
done < "$1"
grep "move:" postfx/postfx.log | tail -2; tail -2 Logs/Logfile.txt
