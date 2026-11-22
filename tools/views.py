#!/usr/bin/env python3
"""views.py name:airport:x:y[:h] ...  -> postfx/views.txt for scripts/flytest.sh (camera = glider position in airport-local coordinates,
h = height above the airport ground plane, default 0.8 m; the view direction is always the glider's heading)"""
import sys, os
import make_demo as m
aps = {a.name[0].lower(): a for a in m.AIRPORTS}
out = []
for arg in sys.argv[1:]:
    p = arg.split(':'); ap = aps[p[1][0].lower()]; x, y = float(p[2]), float(p[3]); h = float(p[4]) if len(p) > 4 else 0.8
    w, n = ap.to_world(x, y); out.append(f'{p[0]} {w:.1f} {n:.1f} {ap.h0+float(ap.z(x, y))+h:.2f}')
open(os.path.expanduser('~/mnt/Condor3/postfx/views.txt'), 'w').write('\n'.join(out)+'\n'); print('\n'.join(out))
