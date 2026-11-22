#!/usr/bin/env python3
"""Crop a rectangle of tiles out of an existing Condor 3 landscape into a new, smaller landscape.
Proves we understand the layout: header/georeference shift, tile renaming, hash tables, tm3/bmp orientation.
usage: crop_landscape.py SRC_DIR DST_DIR tx0 ty0 nx ny   (DST_DIR name = new landscape name)
Tile (tx,ty): tx counts WEST from the east edge (UTM E = E0 - 90*x), ty counts NORTH from the south edge (N = N0 + 90*y)."""
import sys, os, shutil, struct, hashlib
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from terrain_hash import condor_hash
import utm

def main(src, dst, tx0, ty0, nx, ny):
    S = os.path.basename(src.rstrip('/')); D = os.path.basename(dst.rstrip('/'))
    os.makedirs(dst, exist_ok=True)
    for sub in ('HeightMaps/22.5m', 'ForestMaps', 'Textures', 'Autogen/Textures', 'Airports', 'Images'):
        os.makedirs(f'{dst}/{sub}', exist_ok=True)
    tiles = [(tx, ty) for tx in range(nx) for ty in range(ny)]
    # ini
    ini = open(f'{src}/{S}.ini', 'rb').read(); open(f'{dst}/{D}.ini', 'wb').write(ini)
    # trn
    raw = open(f'{src}/{S}.trn', 'rb').read()
    hdr = list(struct.unpack('<II5fII', raw[:36]))
    W, H = hdr[0], hdr[1]
    hts = np.frombuffer(raw[36:], dtype='<u2').reshape(W, H)
    sub = hts[tx0*64:(tx0+nx)*64, ty0*64:(ty0+ny)*64]
    E0, N0 = hdr[5], hdr[6]
    hdr[0], hdr[1] = nx*64, ny*64
    hdr[5], hdr[6] = E0 - tx0*5760.0, N0 + ty0*5760.0
    open(f'{dst}/{D}.trn', 'wb').write(struct.pack('<II5fII', *hdr) + np.ascontiguousarray(sub).astype('<u2').tobytes())
    # per-tile files
    tha, fha, oha = [], [], []
    def cp(a, b):
        if os.path.exists(a): shutil.copyfile(a, b); return True
        return False
    for tx, ty in tiles:
        sx, sy = tx0+tx, ty0+ty
        key = f'{tx*1000+ty:06d}'; idx = ny*tx+ty
        for ext_src, ext_dst in ((f'HeightMaps/h{sx:02d}{sy:02d}.tr3', f'HeightMaps/h{tx:02d}{ty:02d}.tr3'),
                                 (f'HeightMaps/22.5m/h{sx:02d}{sy:02d}.tr3f', f'HeightMaps/22.5m/h{tx:02d}{ty:02d}.tr3f'),
                                 (f'ForestMaps/{sx:02d}{sy:02d}.for', f'ForestMaps/{tx:02d}{ty:02d}.for')):
            cp(f'{src}/{ext_src}', f'{dst}/{ext_dst}')
        for cand in (f't{sx:02d}{sy:02d}.dds', f'T{sx:02d}{sy:02d}.dds'):
            if cp(f'{src}/Textures/{cand}', f'{dst}/Textures/t{tx:02d}{ty:02d}.dds'): break
        h = f'{dst}/HeightMaps/22.5m/h{tx:02d}{ty:02d}.tr3f'; h = h if os.path.exists(h) else f'{dst}/HeightMaps/h{tx:02d}{ty:02d}.tr3'
        tha.append(f'{key} {condor_hash(open(h,"rb").read(), idx)}' if os.path.exists(h) else key)
        f = f'{dst}/ForestMaps/{tx:02d}{ty:02d}.for'
        fha.append(f'{key} {condor_hash(open(f,"rb").read(), idx)}' if os.path.exists(f) else key)
        if cp(f'{src}/Autogen/o{sx:02d}{sy:02d}.c3d', f'{dst}/Autogen/o{tx:02d}{ty:02d}.c3d'):
            oha.append(f'{key} {hashlib.md5(open(f"{dst}/Autogen/o{tx:02d}{ty:02d}.c3d","rb").read()).hexdigest()}')
        else:
            oha.append(key)
    # shared texture dirs: referenced BY NAME from the .c3d files (not by tile index), so copied unchanged
    shutil.copytree(f'{src}/Autogen/Textures', f'{dst}/Autogen/Textures', dirs_exist_ok=True)
    shutil.copytree(f'{src}/Airports/Textures', f'{dst}/Airports/Textures', dirs_exist_ok=True)
    # empty.dds
    cp(f'{src}/Textures/empty.dds', f'{dst}/Textures/empty.dds')
    for name, lines in (('tha', tha), ('fha', fha), ('oha', oha)):
        open(f'{dst}/{D}.{name}', 'wb').write(('\r\n'.join(lines) + '\r\n').encode())
    shutil.copyfile(f'{src}/{S}.aha', f'{dst}/{D}.aha')
    for e in ('cup', 'air', 'obj'):
        cp(f'{src}/{S}.{e}', f'{dst}/{D}.{e}')
    shutil.copytree(f'{src}/World', f'{dst}/World', dirs_exist_ok=True)
    shutil.copytree(f'{src}/Images', f'{dst}/Images', dirs_exist_ok=True)
    # tm3: [y][x] x RGB
    tm = np.memmap(f'{src}/{S}.tm3', dtype=np.uint8, mode='r', shape=(H, W, 3))
    np.ascontiguousarray(tm[ty0*64:(ty0+ny)*64, tx0*64:(tx0+nx)*64]).tofile(f'{dst}/{D}.tm3')
    # bmp: north-up, west-left, 32bpp bottom-up; crop with same header layout
    b = open(f'{src}/{S}.bmp', 'rb').read()
    off = struct.unpack('<I', b[10:14])[0]; bw, bh = struct.unpack('<ii', b[18:26]); assert (bw, bh) == (W, H), (bw, bh)
    px = np.frombuffer(b, dtype=np.uint8, count=bh*bw*4, offset=off).reshape(bh, bw, 4)   # row 0 = south (bottom-up)
    ysl = slice(ty0*64, (ty0+ny)*64)                                      # south row index == y
    xsl = slice(bw-(tx0+nx)*64, bw-tx0*64)                                # west-left columns
    crop = np.ascontiguousarray(px[ysl, xsl])
    nh = bytearray(b[:off]); struct.pack_into('<I', nh, 2, off+crop.nbytes); struct.pack_into('<ii', nh, 18, nx*64, ny*64)
    struct.pack_into('<I', nh, 34, crop.nbytes)
    open(f'{dst}/{D}.bmp', 'wb').write(bytes(nh)+crop.tobytes())
    # apt: keep airports inside
    d = open(f'{src}/{S}.apt', 'rb').read(); out = b''; kept = []
    for i in range(0, len(d), 72):
        r = d[i:i+72]; name = r[1:1+r[0]].decode(); lat, lon, el = struct.unpack('<fff', r[0x24:0x30])
        E, N = utm.forward(lat, lon, 33)
        x = (hdr[5]-E)/90; y = (N-hdr[6])/90
        if 40 < x < nx*64-40 and 40 < y < ny*64-40:
            out += r; kept.append(name)
            for suf in ('.grs', 'G.c3d', 'O.c3d'): cp(f'{src}/Airports/{name}{suf}', f'{dst}/Airports/{name}{suf}')
    open(f'{dst}/{D}.apt', 'wb').write(out)
    print('airports kept:', kept)

if __name__ == '__main__':
    a = sys.argv; main(a[1], a[2], *map(int, a[3:7]))
