#!/usr/bin/env python3
"""Condor 3 object file (.c3d) reader/writer.  Layout from ObjectEditor.exe 'Save to C3D' (@0x5218bc), verified on all Slovenia3 files.

all little endian; ShortString = u8 length + chars
  'C3D'                                  3 bytes magic (no version byte)
  u32 ngroups, ngroups * (u32 first_object, u32 object_count)       (editor writes 1 group: (0, nobj))
  u32 nobj
  per scene object:
      ShortString name
      u32 first_vertex, u32 vertex_count, u32 first_index, u32 index_count       (index values are ABSOLUTE vertex numbers)
      ShortString texture path ('' = untextured, relative to landscape folder or airport folder, e.g. 'Textures\\Aigen\\Aigen_Grass.dds')
      4 x f32  material colour R,G,B,A  (white 1,1,1,1 for textured objects; asphalt ~0.8)
      3 x f32  material parameters (O-files: 0.5, 2.5, 0 ; G-files: 0,0,0; autogen: 0.01, 250, 0) -- probably specular/gloss, semantics not verified
  u32 nverts, nverts * 32 bytes: f32 x,y,z, f32 nx,ny,nz, f32 u,v     (metres, object local frame, Z up)
  u32 nidx,   nidx * u32 triangle list indices (counter-clockwise, normal +Z for a ground polygon)
"""
import struct, sys
import numpy as np

def _pstr(d, o):
    n = d[o]; return d[o+1:o+1+n].decode('latin1'), o+1+n

def parse(d):
    assert d[:3] == b'C3D', 'not a C3D file'
    o = 3
    ng, = struct.unpack_from('<I', d, o); o += 4
    groups = [struct.unpack_from('<II', d, o+8*i) for i in range(ng)]; o += 8*ng
    nobj, = struct.unpack_from('<I', d, o); o += 4
    objs = []
    for _ in range(nobj):
        name, o = _pstr(d, o)
        fv, nv, fi, ni = struct.unpack_from('<IIII', d, o); o += 16
        tex, o = _pstr(d, o)
        col = struct.unpack_from('<4f', d, o); o += 16
        par = struct.unpack_from('<3f', d, o); o += 12
        objs.append(dict(name=name, first_vertex=fv, vertex_count=nv, first_index=fi, index_count=ni, texture=tex, color=col, params=par))
    nv, = struct.unpack_from('<I', d, o); o += 4
    verts = np.frombuffer(d, dtype='<f4', count=nv*8, offset=o).reshape(nv, 8); o += nv*32
    ni, = struct.unpack_from('<I', d, o); o += 4
    idx = np.frombuffer(d, dtype='<u4', count=ni, offset=o); o += ni*4
    assert o == len(d), f'{len(d)-o} trailing bytes'
    return dict(groups=groups, objects=objs, vertices=verts, indices=idx)

def _ps(s):
    b = s.encode('latin1'); assert len(b) < 256
    return bytes([len(b)]) + b

def write(c):
    out = bytearray(b'C3D')
    out += struct.pack('<I', len(c['groups']))
    for g in c['groups']: out += struct.pack('<II', *g)
    out += struct.pack('<I', len(c['objects']))
    for ob in c['objects']:
        out += _ps(ob['name']) + struct.pack('<IIII', ob['first_vertex'], ob['vertex_count'], ob['first_index'], ob['index_count'])
        out += _ps(ob['texture']) + struct.pack('<4f', *ob['color']) + struct.pack('<3f', *ob['params'])
    v = np.ascontiguousarray(c['vertices'], dtype='<f4'); i = np.ascontiguousarray(c['indices'], dtype='<u4')
    out += struct.pack('<I', len(v)) + v.tobytes() + struct.pack('<I', len(i)) + i.tobytes()
    return bytes(out)

def build(objects):
    """objects: list of dict(name, texture, color, params, vertices (n,8) float32, indices (m,) local 0-based) -> c3d dict"""
    V = []; I = []; objs = []; fv = 0; fi = 0
    for ob in objects:
        v = np.asarray(ob['vertices'], np.float32); ix = np.asarray(ob['indices'], np.uint32)
        objs.append(dict(name=ob['name'], first_vertex=fv, vertex_count=len(v), first_index=fi, index_count=len(ix),
                         texture=ob.get('texture', ''), color=ob.get('color', (1, 1, 1, 1)), params=ob.get('params', (0, 0, 0))))
        V.append(v); I.append(ix + fv); fv += len(v); fi += len(ix)
    return dict(groups=[(0, len(objs))], objects=objs, vertices=np.concatenate(V), indices=np.concatenate(I))

if __name__ == '__main__':
    for p in sys.argv[1:]:
        d = open(p, 'rb').read(); c = parse(d)
        print(p, len(c['objects']), 'objects', len(c['vertices']), 'verts', len(c['indices'])//3, 'tris', 'roundtrip', write(c) == d)
