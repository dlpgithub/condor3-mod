#!/usr/bin/env python3
"""Generate a synthetic Condor 3 landscape from scratch (own heights, own tile textures, trees).
usage: make_landscape.py OUT_DIR NAME [SRC_LANDSCAPE_FOR_SHARED_ASSETS]
The terrain function H(x,y) (metres, x = west from the east edge, y = north from the south edge) is below."""
import sys, os, shutil, struct, hashlib
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from terrain_hash import condor_hash
import utm

NX, NY = 8, 8                 # tiles
E0, N0 = 470000.0, 5200000.0  # UTM33N easting of east edge / northing of south edge
ZONE = 33
TILE = 5760.0
CX, CY = NX*TILE/2, NY*TILE/2  # airport position (world metres)
BASE = 640.0

def smooth(t): t = np.clip(t, 0, 1); return t*t*(3-2*t)
def H(x, y):
    """terrain height in metres"""
    x = np.asarray(x, float); y = np.asarray(y, float)
    r = np.hypot(x-CX, y-CY)
    k = smooth((r-2500)/3500)                       # flat around the airport, hills beyond
    hills = 350*np.sin(x/1500.0)*np.cos(y/1900.0) + 250*np.sin((x+y)/2300.0) + 600
    ridge = 700*np.exp(-((x-CX*1.0+4000)/900.0)**2)  # a north-south ridge west of the airport (x larger = further west)
    return BASE + k*(hills*0.6+ridge)

# ---------- DXT1 ----------
def bc1_encode(img):  # img uint8 (h,w,3), h,w multiples of 4 (or padded)
    h, w, _ = img.shape
    ph, pw = (h+3)//4*4, (w+3)//4*4
    if (ph, pw) != (h, w): img = np.pad(img, ((0, ph-h), (0, pw-w), (0, 0)), mode='edge')
    b = img.reshape(ph//4, 4, pw//4, 4, 3).transpose(0, 2, 1, 3, 4).reshape(-1, 16, 3).astype(np.int32)
    out = np.empty((b.shape[0], 8), np.uint8)
    def to565(c): return ((c[..., 0]*31+127)//255 << 11) | ((c[..., 1]*63+127)//255 << 5) | ((c[..., 2]*31+127)//255)
    def from565(v): return np.stack([((v >> 11) & 31)*255//31, ((v >> 5) & 63)*255//63, (v & 31)*255//31], -1)
    for s in range(0, b.shape[0], 16384):
        blk = b[s:s+16384]
        c0 = to565(blk.max(1)); c1 = to565(blk.min(1))
        c0, c1 = np.maximum(c0, c1), np.minimum(c0, c1)
        p0, p1 = from565(c0), from565(c1)
        pal = np.stack([p0, p1, (2*p0+p1)//3, (p0+2*p1)//3], 1)             # (n,4,3)
        d = ((blk[:, :, None, :]-pal[:, None, :, :])**2).sum(-1)             # (n,16,4)
        idx = d.argmin(-1).astype(np.uint32)
        idx[(c0 == c1)[:, None].repeat(16, 1)] = 0
        bits = (idx << (2*np.arange(16, dtype=np.uint32))).sum(1).astype(np.uint32)
        o = out[s:s+16384]
        o[:, 0] = c0 & 255; o[:, 1] = c0 >> 8; o[:, 2] = c1 & 255; o[:, 3] = c1 >> 8
        for k in range(4): o[:, 4+k] = (bits >> (8*k)) & 255
    return out.tobytes()

def write_dds(path, img, hdr_template):
    """img (S,S,3) uint8, S power of two; writes DXT1 with full mip chain using a header template of the same size"""
    parts = [hdr_template]
    cur = img.astype(np.float32)
    while True:
        parts.append(bc1_encode(np.clip(cur+0.5, 0, 255).astype(np.uint8)))
        if cur.shape[0] == 1: break
        s = cur.shape[0]//2
        cur = cur.reshape(s, 2, s, 2, 3).mean((1, 3))
    open(path, 'wb').write(b''.join(parts))

def tile_texture(tx, ty, S=2048):
    """colour = hypsometric tint * hillshade. Image orientation (verified on the real Slovenia3 textures): normal north-up map,
    column 0 = WEST edge of the tile (x max), row 0 = NORTH edge (y max)."""
    px = (1-(np.arange(S)+0.5)/S)*TILE
    X = tx*TILE + px[None, :].repeat(S, 0)       # image column c -> x = x0 + T*(1-(c+.5)/S)
    Y = ty*TILE + px[:, None].repeat(S, 1)       # image row    r -> y = y0 + T*(1-(r+.5)/S)
    h = H(X, Y)
    gr, gc = np.gradient(h, TILE/S)              # d/drow (= -d/dy), d/dcol (= -d/dx)
    gx, gy = -gc, -gr
    shade = np.clip(0.75+0.9*(-gx*0.5+gy*0.5)/np.sqrt(1+gx**2+gy**2), 0.35, 1.2)
    t = np.clip((h-BASE)/900, 0, 1)[..., None]
    col = (1-t)*np.array([90, 140, 60.])+t*np.array([170, 150, 120.])
    col *= shade[..., None]
    gridx = (X % 1000) < 6; gridy = (Y % 1000) < 6
    col[gridx | gridy] = [30, 30, 200]
    for mx, my, c in ((CX+1500, CY, (255, 0, 255)), (CX-1500, CY, (0, 255, 255)), (CX, CY+1500, (255, 128, 0)), (CX, CY-1500, (255, 255, 255))):
        col[(abs(X-mx) < 150) & (abs(Y-my) < 150)] = c      # orientation markers: +x magenta, -x cyan, +y orange, -y white
    col[(np.arange(S)[:, None] < 120) & (np.arange(S)[None, :] < 120)] = [255, 0, 0]       # px=0,py=0 corner
    col[(np.arange(S)[:, None] < 120) & (np.arange(S)[None, :] >= S-60)] = [255, 255, 0]   # px=max, py=0 corner
    if os.environ.get('QUAD'):      # debug: colour by WORLD quadrant around the airport: x>CX&y>CY red (NW), x<CX&y>CY green (NE), x>CX&y<CY yellow (SW), x<CX&y<CY blue (SE)
        q = np.where(Y > CY, np.where(X > CX, 0, 1), np.where(X > CX, 2, 3))
        tint = np.array([[255, 0, 0], [0, 255, 0], [255, 255, 0], [0, 0, 255]], float)[q]
        col = 0.35*col + 0.65*tint
    return np.clip(col, 0, 255).astype(np.uint8)

def main(out, name, src):
    nx, ny = NX, NY
    for sub in ('HeightMaps', 'ForestMaps', 'Textures', 'Autogen/Textures', 'Airports', 'Images'):
        os.makedirs(f'{out}/{sub}', exist_ok=True)
    open(f'{out}/{name}.ini', 'wb').write(b'[General]\r\nVersion=1.02\r\nRealtimeShading=1')
    # trn
    ix = np.arange(nx*64)*90.0; iy = np.arange(ny*64)*90.0
    trn = np.rint(H(ix[:, None], iy[None, :])).astype('<u2')           # [x][y]
    hdr = struct.pack('<II5fII', nx*64, ny*64, 90.0, -90.0, 90.0, E0, N0, ZONE, ord('N'))
    open(f'{out}/{name}.trn', 'wb').write(hdr+trn.tobytes())
    tpl = open(sorted(f for f in (f'{src}/Textures/'+n for n in os.listdir(f'{src}/Textures')) if os.path.getsize(f) == 2796344)[0], 'rb').read(128)
    tha, fha, oha = [], [], []
    rng = np.random.default_rng(1)
    for tx in range(nx):
        for ty in range(ny):
            key = f'{tx*1000+ty:06d}'; idx = ny*tx+ty
            gx = tx*TILE+np.arange(193)*30.0; gy = ty*TILE+np.arange(193)*30.0
            t3 = np.rint(H(gx[:, None], gy[None, :])).astype('<u2')   # [x][y], y fastest
            d = t3.tobytes(); open(f'{out}/HeightMaps/h{tx:02d}{ty:02d}.tr3', 'wb').write(d)
            tha.append(f'{key} {condor_hash(d, idx)}')
            fx = tx*TILE+(np.arange(512)+.5)*TILE/512; fy = ty*TILE+(np.arange(512)+.5)*TILE/512
            hh = H(fx[:, None], fy[None, :])
            forest = np.zeros((512, 512), np.uint8)
            r = rng.random((512, 512))
            forest[(hh > BASE+120) & (r < 0.25)] = 1
            forest[(hh > BASE+120) & (r >= 0.25) & (r < 0.40)] = 2
            d = forest.tobytes(); open(f'{out}/ForestMaps/{tx:02d}{ty:02d}.for', 'wb').write(d)   # [row=?][col=?], see docs
            fha.append(f'{key} {condor_hash(d, idx)}')
            oha.append(key)
            tp = f'{out}/Textures/t{tx:02d}{ty:02d}.dds'
            force = (tx, ty) in {tuple(map(int, t.split(','))) for t in os.environ.get('FORCE_TILES', '').split(';') if t}
            if force or not os.path.exists(tp):   # resumable: write to temp then rename
                write_dds(tp+'.tmp', tile_texture(tx, ty), tpl); os.replace(tp+'.tmp', tp)
            print('tile', tx, ty, flush=True)
    shutil.copyfile(f'{src}/Textures/empty.dds', f'{out}/Textures/empty.dds')
    for ext, lines in (('tha', tha), ('fha', fha), ('oha', oha)):
        open(f'{out}/{name}.{ext}', 'wb').write(('\r\n'.join(lines)+'\r\n').encode())
    open(f'{out}/{name}.aha', 'wb').write(b'0\r\n')
    for e in ('obj', 'cup', 'air'): open(f'{out}/{name}.{e}', 'wb').write(b'')
    open(f'{out}/{name}.cup', 'wb').write(b'name,code,country,lat,lon,elev,style,rwdir,rwlen,freq,desc\r\n')
    # map images: tm3 (rows y, cols x) and bmp (north-up, west-left, bottom-up BGRA)
    X = (np.arange(nx*64)+.5)*90.0; Y = (np.arange(ny*64)+.5)*90.0
    hm = H(X[None, :], Y[:, None])                                     # [y][x]
    t = np.clip((hm-BASE)/900, 0, 1)[..., None]
    rgb = ((1-t)*np.array([90, 140, 60.])+t*np.array([170, 150, 120.])).astype(np.uint8)
    rgb.tofile(f'{out}/{name}.tm3')
    sb = open(f'{src}/{os.path.basename(src)}.bmp', 'rb').read(54)
    bh = bytearray(sb); struct.pack_into('<ii', bh, 18, nx*64, ny*64); struct.pack_into('<I', bh, 34, nx*64*ny*64*4); struct.pack_into('<I', bh, 2, 54+nx*64*ny*64*4)
    bgra = np.dstack([rgb[..., 2], rgb[..., 1], rgb[..., 0], np.full(rgb.shape[:2], 255, np.uint8)])   # row 0 = south
    bgra = bgra[:, ::-1]                                               # columns west-left: x big on the left
    open(f'{out}/{name}.bmp', 'wb').write(bytes(bh)+np.ascontiguousarray(bgra).tobytes())
    # airport: reuse Aigen's assets, moved to (CX,CY)
    sapt = open(f'{src}/{os.path.basename(src)}.apt', 'rb').read()
    rec = [sapt[i:i+72] for i in range(0, len(sapt), 72) if sapt[i+1:i+1+sapt[i]] == b'Aigen'][0]
    lat, lon = utm.inverse(E0-CX, N0+CY, ZONE)
    rec = bytearray(rec); struct.pack_into('<fff', rec, 0x24, lat, lon, float(H(CX, CY)))
    open(f'{out}/{name}.apt', 'wb').write(bytes(rec))
    for suf in ('.grs', 'G.c3d', 'O.c3d'): shutil.copyfile(f'{src}/Airports/Aigen{suf}', f'{out}/Airports/Aigen{suf}')
    shutil.copytree(f'{src}/Airports/Textures', f'{out}/Airports/Textures', dirs_exist_ok=True)
    shutil.copytree(f'{src}/Autogen/Textures', f'{out}/Autogen/Textures', dirs_exist_ok=True)
    shutil.copytree(f'{src}/World', f'{out}/World', dirs_exist_ok=True)
    shutil.copytree(f'{src}/Images', f'{out}/Images', dirs_exist_ok=True)
    print('airport at lat/lon', lat, lon, 'elev', float(H(CX, CY)))

if __name__ == '__main__':
    out, name = sys.argv[1], sys.argv[2]
    main(out, name, sys.argv[3] if len(sys.argv) > 3 else os.path.expanduser('~/mnt/Condor3/Landscapes/Slovenia3'))
