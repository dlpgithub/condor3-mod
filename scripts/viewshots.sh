#!/bin/bash
# viewshots.sh <views_file> [wait_s] : like the second half of flytest.sh, but for a game that is already in flight (paused).
# For every line "<shotname> <west> <north> <alt>": move the glider there (sim stays paused), wait <wait_s> seconds (so streamed
# autogen can follow, default 0), redraw and take a screenshot into <game>/postfx/shots/<shotname>.png.
GAME=/Users/lennard/Condor3; cd $GAME || exit 1
W=${2:-0}
shot(){ screencapture -x /tmp/_s.png && sips -Z ${2:-1100} /tmp/_s.png --out postfx/shots/$1.png >/dev/null; }
goto(){  # absolute move: read the current position first (the glider drifts while the sim runs)
  echo "move 0 0 0" > postfx/cmd.txt; sleep 1.2
  d=$(grep "move:" postfx/postfx.log | tail -1 | awk -v w=$1 -v n=$2 -v z=$3 '{printf "%.2f %.2f %.2f", w-$(NF-2), n-$(NF-1), z-$NF}')
  echo "move $d" > postfx/cmd.txt; sleep 1.2
}
while read name tw tn tz; do
  [ -z "$name" ] && continue
  goto $tw $tn $tz
  sleep $W                      # paused: streamed autogen follows the new position (it runs per rendered frame)
  printf 'kkey P\nkkey P\n' > postfx/cmd.txt; sleep 3.2; shot $name
done < "$1"
grep "move:" postfx/postfx.log | tail -1
