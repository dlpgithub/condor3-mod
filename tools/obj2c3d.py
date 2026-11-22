#!/usr/bin/env python3
"""Wavefront OBJ (+MTL) -> Condor C3D, same conventions as ObjectEditor's importer (disassembled, ObjectEditor.exe 0x51eabc).

usage: obj2c3d.py in.obj out.c3d [--wings] [--texdir Textures/Name/]

'Open OBJ (Blender)'  (export from Blender with Forward = Y, Up = Z, triangulated):
    C3D (x, y, z) = OBJ (y, x, z)        i.e. Blender +X becomes C3D +Y (airport: runway heading, world object: north at rotation 0),
                                         Blender +Y becomes C3D +X (airport: left of the heading, world object: west)
'Open OBJ (Wings)'    (Y-up export):  C3D (x, y, z) = OBJ (z, -x, y)
Both: normals mapped like positions, uv = (u, 1 - v), and the triangle order is reversed because the axis mapping is a reflection
(in C3D files the geometric normal of a triangle, computed naively from the stored coordinates, agrees with the stored normals).
MTL: Kd -> colour, d -> alpha, Ks -> Spec, Ns/100 -> Shiny, Ke -> Env, map_Kd -> texture (file name only, prefixed with --texdir).
One C3D scene object per OBJ object/material pair (multi-materials are not supported by the game).
"""
import sys, os
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c3d

def parse_mtl(path):
    mats = {}; cur = None
    if not os.path.exists(path): return mats
    for line in open(path, errors='replace'):
        p = line.split()
        if not p: continue
        if p[0] == 'newmtl': cur = mats.setdefault(' '.join(p[1:]), dict(Kd=(1, 1, 1), d=1.0, Ks=0.0, Ns=0.0, Ke=0.0, tex=''))
        elif cur is None: continue
        elif p[0] == 'Kd': cur['Kd'] = tuple(map(float, p[1:4]))
        elif p[0] == 'd': cur['d'] = float(p[1])
        elif p[0] == 'Ks': cur['Ks'] = float(p[1])
        elif p[0] == 'Ns': cur['Ns'] = float(p[1])
        elif p[0] == 'Ke': cur['Ke'] = float(p[1])
        elif p[0] == 'map_Kd': cur['tex'] = os.path.basename(' '.join(p[1:]).replace('\\', '/'))
    return mats

def convert(obj_path, wings=False, texdir=''):
    V = []; VT = []; VN = []; mats = {}; groups = {}; order = []
    name = 'Object'; mat = ''
    fix = (lambda a, b, c: (c, -a, b)) if wings else (lambda a, b, c: (b, a, c))
    for line in open(obj_path, errors='replace'):
        p = line.split()
        if not p: continue
        if p[0] == 'v': V.append(fix(*map(float, p[1:4])))
        elif p[0] == 'vn': VN.append(fix(*map(float, p[1:4])))
        elif p[0] == 'vt': VT.append((float(p[1]), 1.0-float(p[2])))
        elif p[0] in ('o', 'g'): name = ' '.join(p[1:]) or 'Object'
        elif p[0] == 'usemtl': mat = ' '.join(p[1:])
        elif p[0] == 'mtllib': mats.update(parse_mtl(os.path.join(os.path.dirname(obj_path), ' '.join(p[1:]))))
        elif p[0] == 'f':
            key = (name, mat)
            if key not in groups: groups[key] = dict(verts={}, idx=[]); order.append(key)
            g = groups[key]; corner = []
            for tok in p[1:]:
                q = (tok.split('/')+['', ''])[:3]
                k = (int(q[0]), int(q[1]) if q[1] else 0, int(q[2]) if q[2] else 0)
                corner.append(g['verts'].setdefault(k, len(g['verts'])))
            for i in range(1, len(corner)-1): g['idx'] += [corner[0], corner[i+1], corner[i]]     # fan, REVERSED order
    objs = []
    for key in order:
        g = groups[key]; m = mats.get(key[1], dict(Kd=(1, 1, 1), d=1.0, Ks=0.0, Ns=0.0, Ke=0.0, tex=''))
        vv = np.zeros((len(g['verts']), 8), np.float32)
        def pick(arr, i): return arr[i-1] if i > 0 else arr[i]
        for (iv, it, inn), n in g['verts'].items():
            vv[n, :3] = pick(V, iv)
            if inn: vv[n, 3:6] = pick(VN, inn)
            if it: vv[n, 6:8] = pick(VT, it)
        idx = np.array(g['idx'], np.uint32)
        if not any(k[2] for k in g['verts']):                                                    # no normals in the file: flat-ish vertex normals
            t = vv[:, :3][idx.reshape(-1, 3)]; fn = np.cross(t[:, 1]-t[:, 0], t[:, 2]-t[:, 0])
            for c in range(3): np.add.at(vv[:, 3:6], idx.reshape(-1, 3)[:, c], fn)
            vv[:, 3:6] /= np.maximum(np.linalg.norm(vv[:, 3:6], axis=1, keepdims=True), 1e-9)
        tex = (texdir + m['tex']) if m['tex'] else ''
        col = (1, 1, 1, m['d']) if tex and m['Kd'] == (0.8, 0.8, 0.8) else (*m['Kd'], m['d'])
        objs.append(dict(name=(key[0] if len([k for k in order if k[0] == key[0]]) == 1 else f'{key[0]}_{key[1]}')[:63], texture=tex, color=col,
                         params=(m['Ks'], m['Ns']/100.0, m['Ke']), vertices=vv, indices=idx))
    return c3d.build(objs)

if __name__ == '__main__':
    a = [x for x in sys.argv[1:] if not x.startswith('--')]
    td = sys.argv[sys.argv.index('--texdir')+1] if '--texdir' in sys.argv else ''
    if td in a: a.remove(td)
    c = convert(a[0], wings='--wings' in sys.argv, texdir=td)
    open(a[1], 'wb').write(c3d.write(c))
    for o in c['objects']: print(o['name'], o['vertex_count'], 'verts', o['index_count']//3, 'tris', o['texture'], [round(x, 2) for x in o['color']], o['params'])
