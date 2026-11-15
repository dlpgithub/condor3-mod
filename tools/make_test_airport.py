#!/usr/bin/env python3
"""Create a minimal custom airport 'Testfield' in a landscape using only our own tools (c3d.py, apt.py).
usage: make_test_airport.py <landscape_dir> <lat> <lon> <elev> <direction_deg>
G file: one flat Asphalt strip 900x50 m (Z=0 at runway centre, runway axis = local +X), untextured.
O file: red box at local y=-60 and blue box at local y=+60 (to find the handedness of the local frame), grey hangar-ish block."""
import sys, os, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c3d, apt

def quad(x0, y0, x1, y1, z=0.0):
    v = np.array([[x0, y0, z, 0, 0, 1, 0, 0], [x1, y0, z, 0, 0, 1, 1, 0], [x1, y1, z, 0, 0, 1, 1, 1], [x0, y1, z, 0, 0, 1, 0, 1]], np.float32)
    return v, np.array([0, 1, 2, 0, 2, 3], np.uint32)

def box(cx, cy, w, d, h):
    """axis aligned box, base on z=0, CCW outward faces"""
    x0, x1, y0, y1 = cx-w/2, cx+w/2, cy-d/2, cy+d/2
    faces = [((0, 0, 1), [(x0, y0, h), (x1, y0, h), (x1, y1, h), (x0, y1, h)]),
             ((0, 0, -1), [(x0, y1, 0), (x1, y1, 0), (x1, y0, 0), (x0, y0, 0)]),
             ((0, -1, 0), [(x0, y0, 0), (x1, y0, 0), (x1, y0, h), (x0, y0, h)]),
             ((0, 1, 0), [(x1, y1, 0), (x0, y1, 0), (x0, y1, h), (x1, y1, h)]),
             ((1, 0, 0), [(x1, y0, 0), (x1, y1, 0), (x1, y1, h), (x1, y0, h)]),
             ((-1, 0, 0), [(x0, y1, 0), (x0, y0, 0), (x0, y0, h), (x0, y1, h)])]
    V = []; I = []
    for n, pts in faces:
        b = len(V)
        for k, p in enumerate(pts): V.append([*p, *n, k in (1, 2), k in (2, 3)])
        I += [b, b+1, b+2, b, b+2, b+3]
    return np.array(V, np.float32), np.array(I, np.uint32)

def main(ld, lat, lon, elev, ddir, name='Testfield'):
    os.makedirs(f'{ld}/Airports', exist_ok=True)
    objs_g = []
    v, i = quad(-600, -300, 600, 300, z=-0.3)
    objs_g.append(dict(name='Grass3D', texture='Textures\\Testfield\\Grass.dds', color=(1, 1, 1, 1), params=(0, 0, 0), vertices=v, indices=i))
    v, i = quad(-450, -25, 450, 25, z=0.2)
    objs_g.append(dict(name='Asphalt', texture='Textures\\Testfield\\Muck.dds', color=(1, 1, 1, 1), params=(0, 0, 0), vertices=v, indices=i))
    g = c3d.build(objs_g)
    open(f'{ld}/Airports/{name}G.c3d', 'wb').write(c3d.write(g))
    objs = []
    for nm, cx, cy, h, col in (('RedBox', 0, -60, 8, (1, 0, 0, 1)), ('BlueBox', 0, 60, 8, (0, 0, 1, 1)), ('Block', 150, 90, 8, (0.6, 0.6, 0.6, 1)),
                               ('PoleCentre', 0, 0, 30, (1, 1, 0, 1)), ('PoleMinusX', -450, 0, 20, (0, 1, 0, 1)), ('PolePlusX', 450, 0, 20, (1, 0, 1, 1))):
        v, i = box(cx, cy, 20 if h == 8 else 3, 10 if h == 8 else 3, h)
        objs.append(dict(name=nm, texture='', color=col, params=(0.5, 2.5, 0), vertices=v, indices=i))
    open(f'{ld}/Airports/{name}O.c3d', 'wb').write(c3d.write(c3d.build(objs)))
    rec = apt.pack_one(name, lat, lon, elev, ddir, 900, 50, asphalt=True, freq=123.5)
    open(f'{ld}/{os.path.basename(ld)}.apt', 'wb').write(rec)
    print('wrote', name, 'G/O and', os.path.basename(ld)+'.apt')

if __name__ == '__main__':
    a = sys.argv
    main(a[1], float(a[2]), float(a[3]), float(a[4]), float(a[5]))
