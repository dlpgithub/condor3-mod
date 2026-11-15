#!/usr/bin/env python3
"""Build a complete small Condor 3 demonstration landscape from nothing but this script (no file is copied from a stock landscape).

usage: make_demo.py <Landscapes_dir>/<Name>          e.g.  make_demo.py ~/mnt/Condor3/Landscapes/ProofValley

What is produced (every file type the game needs, see docs/TERRAIN_FORMAT.md):
  <N>.ini .trn .tm3 .bmp .tha .fha .oha .aha .air .obj .cup .apt
  HeightMaps/hXXYY.tr3 (+ 22.5m/hXXYY.tr3f for the airport tile), ForestMaps/XXYY.for, Textures/tXXYY.dds (+ empty.dds)
  Airports/<Airport>G.c3d, <Airport>O.c3d, Airports/Textures/<Airport>/*.dds

The landscape: 6 x 5 tiles (34.56 x 28.8 km), a meandering east-west valley (floor ~500 m) between procedural mountains (to ~2700 m),
a lake (texture alpha = water), field patchwork, two forest classes, and one airport 'Talfeld' on terrain flattened for it.
World coordinates as in the game: x = metres WEST of the east edge, y = metres NORTH of the south edge.
"""
import sys, os, math, struct
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from terrain_hash import condor_hash
from make_tm3 import make_tm3
from make_airport import grid_mesh, box, write_bgra_dds, paint_runway
import c3d, apt, utm

NX, NY = 6, 5
TILE = 5760.0
E0, N0, ZONE = 460800.0, 5184000.0, 33          # UTM easting of the EAST edge, northing of the SOUTH edge
AIRPORT = 'Talfeld'
AX = 20000.0                                    # airport world x (west); y follows the valley axis
RWY_LEN, RWY_WID = 900, 30
FLOOR = 500.0
LAKE_X, LAKE_RX, LAKE_RY, LAKE_Z = 7500.0, 1600.0, 550.0, 496.0
MW, MH = NX*192+1, NY*192+1                     # master grid of 30 m posts

def smooth(t): t = np.clip(t, 0, 1); return t*t*(3-2*t)
def yv(x): return NY*TILE/2 + 2500.0*np.sin(np.asarray(x)/6000.0)      # valley axis
AY = float(yv(AX))
_t = 2500.0/6000.0*math.cos(AX/6000.0)                                    # dy/dx of the axis at the airport
HEADING = round(math.degrees(math.atan2(1.0, -_t)) % 360, 2)             # fly "down the valley" towards the east: (east, north) = (1, -dy/dx)
_c, _s = math.cos(math.radians(HEADING)), math.sin(math.radians(HEADING))
def to_local(X, Y):   # inverse of world = origin + x*(c,s) + y*(-s,c)
    dW = X-AX; dN = Y-AY
    return dW*_c + dN*_s, -dW*_s + dN*_c
def to_world(x, y): return AX + x*_c - y*_s, AY + x*_s + y*_c
def rect_dist(xl, yl, hx, hy): return np.hypot(np.maximum(np.abs(xl)-hx, 0), np.maximum(np.abs(yl)-hy, 0))

def spectral(shape, beta, seed):
    rng = np.random.default_rng(seed)
    f = np.fft.rfft2(rng.standard_normal(shape))
    k = np.hypot(np.fft.fftfreq(shape[0])[:, None], np.fft.rfftfreq(shape[1])[None, :]); k[0, 0] = 1
    f *= k**(-beta/2); f[0, 0] = 0
    a = np.fft.irfft2(f, s=shape); a -= a.min(); a /= a.max()
    return a.astype(np.float32)

def samp(F, X, Y):
    fx = np.clip(np.asarray(X, np.float32)/30, 0, F.shape[0]-1.001); fy = np.clip(np.asarray(Y, np.float32)/30, 0, F.shape[1]-1.001)
    i = fx.astype(np.int32); j = fy.astype(np.int32); u = fx-i; v = fy-j
    return F[i, j]*(1-u)*(1-v) + F[i+1, j]*u*(1-v) + F[i, j+1]*(1-u)*v + F[i+1, j+1]*u*v

# ---------------- master fields (30 m posts, [ix][iy]) ----------------
def build_master():
    X = (np.arange(MW)*30.0)[:, None].astype(np.float32); Y = (np.arange(MH)*30.0)[None, :].astype(np.float32)
    a = spectral((MW, MH), 3.6, 11); b = spectral((MW, MH), 2.6, 12); c = spectral((MW, MH), 4.0, 13)
    d = np.abs(Y-yv(X))
    m = smooth((d-900)/6000)
    floor = FLOOR + 8*(c-0.5)
    H = floor + m*(450 + 2000*a**1.25) + m*60*(b-0.5)
    r = np.sqrt(((X-LAKE_X)/LAKE_RX)**2 + ((Y-yv(X))/LAKE_RY)**2)             # lake basin
    w = smooth((1.15-r)/0.15); H = H*(1-w) + LAKE_Z*w
    xl, yl = to_local(X, Y)                                                    # airport: flatten to an exact plane
    w = 1-smooth(rect_dist(xl, yl, 420, 780)/350); H = H*(1-w) + FLOOR*w
    H = H.astype(np.float32)
    gx, gy = np.gradient(H, 30.0)
    return H, gx.astype(np.float32), gy.astype(np.float32), spectral((MW, MH), 3.0, 14)
Hm, Gx, Gy, N1 = build_master()
NT = spectral((512, 512), 2.0, 15)                                            # periodic detail noise

def lake_r(X, Y): return np.sqrt(((X-LAKE_X)/LAKE_RX)**2 + ((Y-yv(X))/LAKE_RY)**2)

def forest_class(X, Y, h=None, slope=None):
    """0 none, 1 conifer, 2 deciduous, 3 both (the values of the .for file)"""
    if h is None:
        h = samp(Hm, X, Y); slope = np.degrees(np.arctan(np.hypot(samp(Gx, X, Y), samp(Gy, X, Y))))
    n = samp(N1, X, Y)
    con = (h > 780) & (h < 1800) & (slope < 40) & (n > 0.40)
    dec = (h > 512) & (h <= 900) & (slope > 4) & (n > 0.46)
    xl, yl = to_local(X, Y)
    ok = (lake_r(X, Y) > 1.25) & (rect_dist(xl, yl, 420, 780) > 250)
    return ((con & ok)*1 + (dec & ok)*2).astype(np.uint8)

FIELDS = np.array([(150, 160, 70), (110, 150, 60), (185, 170, 95), (130, 120, 70), (95, 140, 55), (165, 180, 90)], np.float32)
def color(X, Y):
    """ground colour (float32 RGB), water mask and thermal class for arbitrary world points"""
    X = np.asarray(X, np.float32); Y = np.asarray(Y, np.float32)
    h = samp(Hm, X, Y); gx = samp(Gx, X, Y); gy = samp(Gy, X, Y)
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))
    nm = NT[(X/5.625).astype(np.int64) % 512, (Y/5.625).astype(np.int64) % 512]
    nf = NT[(X/0.7).astype(np.int64) % 512, (Y/0.7).astype(np.int64) % 512]
    nz = ((nm-0.5)*0.5 + (nf-0.5)*0.3)[..., None]
    col = np.array([95, 135, 60], np.float32)*(1+nz*0.5)
    therm = np.full(X.shape, 102, np.uint8)                                  # grass
    # field patchwork on the valley floor
    a = X*0.94+Y*0.34; b = -X*0.34+Y*0.94
    i = np.floor(a/220).astype(np.int64); bb = b + (i % 2)*60; j = np.floor(bb/130).astype(np.int64)
    fcol = FIELDS[((i*73856093) ^ (j*19349663)) % 6]*(1+nz*0.35)
    edge = (np.minimum(a % 220, 220-(a % 220)) < 2.5) | (np.minimum(bb % 130, 130-(bb % 130)) < 2.5)
    fcol[edge] *= 0.75
    fm = (np.abs(Y-yv(X)) < 1050) & (slope < 5)
    col[fm] = fcol[fm]; therm[fm] = 178
    t = smooth((h-1500)/300)[..., None]; col = col*(1-t) + np.array([128, 130, 84], np.float32)*(1+nz*0.4)*t
    tr = np.maximum(smooth((slope-30)/10), smooth((h-2050)/250))
    col = col*(1-tr[..., None]) + np.array([125, 120, 112], np.float32)*(1+nz*0.6)*tr[..., None]; therm[tr > 0.5] = 200
    fc = forest_class(X, Y, h, slope)
    for v, c, th in ((1, (40, 72, 42), 102), (2, (62, 104, 48), 64), (3, (50, 88, 45), 80)):
        mk = fc == v; col[mk] = (np.array(c, np.float32)*(1+nz*1.2))[mk]; therm[mk] = th
    ts = smooth((h-2300+(nm-0.5)*300)/120)*(1-0.6*smooth((slope-40)/15))
    col = col*(1-ts[..., None]) + np.array([238, 240, 245], np.float32)*ts[..., None]; therm[ts > 0.5] = 30
    ndl = (0.6*gy+0.8)/np.sqrt(1+gx*gx+gy*gy)                                # light from the south
    col *= np.clip(0.55+0.6*ndl, 0.5, 1.25)[..., None]
    # airport: mown grass, asphalt runway, apron, taxiway
    xl, yl = to_local(X, Y)
    w = (1-smooth(rect_dist(xl, yl, 280, 680)/60))[..., None]
    col = col*(1-w) + np.array([105, 145, 62], np.float32)*(1+nz*0.3)*w
    asph = (rect_dist(xl, yl, RWY_WID/2, RWY_LEN/2) == 0) | (rect_dist(xl-75, yl, 35, 40) == 0) | (rect_dist(xl-27, yl, 13, 6) == 0)
    col[asph] = (np.array([96, 96, 100], np.float32)*(1+nz*0.15))[asph]; therm[asph] = 200
    r = lake_r(X, Y)
    sand = (r >= 1) & (r < 1.04); col[sand] = (np.array([170, 160, 120], np.float32)*(1+nz*0.3))[sand]
    water = r < 1
    col[water] = (np.array([38, 78, 105], np.float32)*(1+nz*0.1))[water]; therm[water] = 0
    return np.clip(col, 0, 255), water, therm

# ---------------- DDS ----------------
def dds_header(S, fourcc):
    bs = 8 if fourcc == b'DXT1' else 16
    return (b'DDS ' + struct.pack('<7I', 124, 0xA1007, S, S, (S//4)**2*bs, 0, int(math.log2(S))+1) + b'\0'*44 +
            struct.pack('<2I4s5I', 32, 4, fourcc, 0, 0, 0, 0, 0) + struct.pack('<5I', 0x401008, 0, 0, 0, 0))

def bc1_blocks(img):
    h, w, _ = img.shape
    ph, pw = max(4, (h+3)//4*4), max(4, (w+3)//4*4)
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
        pal = np.stack([p0, p1, (2*p0+p1)//3, (p0+2*p1)//3], 1)
        idx = ((blk[:, :, None, :]-pal[:, None, :, :])**2).sum(-1).argmin(-1).astype(np.uint32)
        idx[(c0 == c1)[:, None].repeat(16, 1)] = 0
        bits = (idx << (2*np.arange(16, dtype=np.uint32))).sum(1).astype(np.uint32)
        o = out[s:s+16384]
        o[:, 0] = c0 & 255; o[:, 1] = c0 >> 8; o[:, 2] = c1 & 255; o[:, 3] = c1 >> 8
        for k in range(4): o[:, 4+k] = (bits >> (8*k)) & 255
    return out

def alpha_blocks(a):   # DXT3 explicit alpha: 4 bits per pixel, row-major, low nibble first
    h, w = a.shape
    ph, pw = max(4, (h+3)//4*4), max(4, (w+3)//4*4)
    if (ph, pw) != (h, w): a = np.pad(a, ((0, ph-h), (0, pw-w)), mode='edge')
    n = (a.reshape(ph//4, 4, pw//4, 4).transpose(0, 2, 1, 3).reshape(-1, 16).astype(np.uint16)+8)//17
    return (n[:, 0::2] | (n[:, 1::2] << 4)).astype(np.uint8)

def write_dds(path, rgb, alpha=None):
    """rgb (S,S,3) uint8; alpha (S,S) uint8 or None.  DXT1 without alpha, DXT3 with; full mip chain; exact size (the game checks it)"""
    S = rgb.shape[0]; parts = [dds_header(S, b'DXT3' if alpha is not None else b'DXT1')]
    cur = rgb.astype(np.float32); ca = None if alpha is None else alpha.astype(np.float32)
    while True:
        blk = bc1_blocks(np.clip(cur+0.5, 0, 255).astype(np.uint8))
        if ca is not None: blk = np.concatenate([alpha_blocks(np.clip(ca+0.5, 0, 255).astype(np.uint8)), blk], 1)
        parts.append(blk.tobytes())
        if cur.shape[0] == 1: break
        s = cur.shape[0]//2
        cur = cur.reshape(s, 2, s, 2, 3).mean((1, 3))
        if ca is not None: ca = ca.reshape(s, 2, s, 2).mean((1, 3))
    open(path, 'wb').write(b''.join(parts))

def tile_texture(tx, ty, S):
    """north-up image: column 0 = WEST edge (x max), row 0 = NORTH edge (y max)"""
    px = (1-(np.arange(S, dtype=np.float32)+0.5)/S)*TILE
    rgb = np.empty((S, S, 3), np.uint8); water = np.empty((S, S), bool)
    for r0 in range(0, S, 512):
        X = np.broadcast_to(tx*TILE + px[None, :], (512, S)); Y = np.broadcast_to(ty*TILE + px[r0:r0+512, None], (512, S))
        c, w, _ = color(X, Y); rgb[r0:r0+512] = c.astype(np.uint8); water[r0:r0+512] = w
    return rgb, water

# ---------------- airport ----------------
def roof(cx, cy, z0, w, d, rise):
    """gable roof over the rectangle (ridge along x), outward CCW"""
    x0, x1, y0, y1 = cx-w/2, cx+w/2, cy-d/2, cy+d/2; zt = z0+rise
    quads = [[(x0, y0, z0), (x1, y0, z0), (x1, cy, zt), (x0, cy, zt)], [(x1, y1, z0), (x0, y1, z0), (x0, cy, zt), (x1, cy, zt)]]
    tris = [[(x1, y0, z0), (x1, y1, z0), (x1, cy, zt)], [(x0, y1, z0), (x0, y0, z0), (x0, cy, zt)]]
    V = []; I = []
    for p in quads+tris:
        n = np.cross(np.subtract(p[1], p[0]), np.subtract(p[2], p[0])); n = n/np.linalg.norm(n); b = len(V)
        for k, q in enumerate(p): V.append([*q, *n, k in (1, 2), k >= 2])
        I += [b, b+1, b+2] + ([b, b+2, b+3] if len(p) == 4 else [])
    return np.array(V, np.float32), np.array(I, np.uint32)

def build_airport(out):
    ax0, ax1, ay0, ay1 = -300.0, 300.0, -700.0, 700.0
    res = 0.5; nu, nv = int((ax1-ax0)/res), int((ay1-ay0)/res)
    x = (ax1 - (np.arange(nu, dtype=np.float32)+0.5)/nu*(ax1-ax0))[None, :]; img = np.empty((nv, nu, 3), np.uint8)
    for r0 in range(0, nv, 400):                                              # heading-up photo: image right = -x, image down = -y
        y = (ay1 - (np.arange(r0, min(r0+400, nv), dtype=np.float32)+0.5)/nv*(ay1-ay0))[:, None]
        xx = np.broadcast_to(x, (y.shape[0], nu)); yy = np.broadcast_to(y, (y.shape[0], nu))
        c, _, _ = color(*to_world(xx, yy)); part = c.astype(np.uint8)
        paint_runway(part, xx, yy, RWY_LEN, RWY_WID, asphalt=False)          # markings only; the asphalt is already in color()
        img[r0:r0+y.shape[0]] = part
    td = f'{out}/Airports/Textures/{AIRPORT}'; os.makedirs(td, exist_ok=True)
    write_bgra_dds(f'{td}/{AIRPORT}_Grass.dds', img)
    uv = lambda x, y: ((ax1-x)/(ax1-ax0), (ay1-y)/(ay1-ay0))
    # the terrain under the airport is an exact plane at FLOOR, so the mesh is the terrain surface itself: z = 0 (as in the stock files)
    vg, ig = grid_mesh(ax0, ax1, ay0, ay1, 50.0, lambda X, Y: np.zeros_like(X), uv, zoff=float(os.environ.get('ZOFF', 0)))
    g = c3d.build([dict(name='Grass3D', texture=f'Textures\\{AIRPORT}\\{AIRPORT}_Grass.dds', color=(1, 1, 1, 1), params=(0, 0, 0), vertices=vg, indices=ig)])
    open(f'{out}/Airports/{AIRPORT}G.c3d', 'wb').write(c3d.write(g))
    # objects: a hangar with a textured front (own DXT1 texture), a windsock pole and the windsock proxy triangle
    wall = np.zeros((256, 256, 3), np.uint8); wall[:] = (150, 155, 160); wall[:, ::16] = (120, 125, 130)
    wall[96:, 48:208] = (70, 80, 110); wall[96:, 126:130] = (40, 45, 60); wall[:24] = (190, 60, 50)
    write_dds(f'{td}/Hangar.dds', wall)
    hv, hi = box(75, 60, 0.0, 30, 24, 7); rv, ri = roof(75, 60, 7.0, 30, 24, 3.5)
    pv, pi = box(40, -150, 0.0, 0.2, 0.2, 7.9)
    ws = np.array([[40, -150, 7.9, -1, 0, 0, 0.75, 0.51], [39.961, -149.9775, 7.9, -1, 0, 0, 0.957846, 0.87], [39.961, -150.0225, 7.9, -1, 0, 0, 0.542154, 0.87]], np.float32)
    objs = [dict(name='Hangar', texture=f'Textures/{AIRPORT}/Hangar.dds', color=(1, 1, 1, 1), params=(0.5, 2.5, 0), vertices=hv, indices=hi),
            dict(name='HangarRoof', texture='', color=(0.55, 0.2, 0.15, 1), params=(0.5, 2.5, 0), vertices=rv, indices=ri),
            dict(name='WindsockPole', texture='', color=(0.9, 0.9, 0.9, 1), params=(0.5, 2.5, 0), vertices=pv, indices=pi),
            dict(name='Windsack1', texture='', color=(0.8, 0.8, 0.8, 1), params=(0.8, 5.0, 0), vertices=ws, indices=np.array([0, 2, 1], np.uint32))]
    open(f'{out}/Airports/{AIRPORT}O.c3d', 'wb').write(c3d.write(c3d.build(objs)))
    lat, lon = utm.inverse(E0-AX, N0+AY, ZONE)
    elev = FLOOR + float(os.environ.get('APT_ELEV_OFFSET', 0))
    return apt.pack_one(AIRPORT, lat, lon, elev, HEADING, RWY_LEN, RWY_WID, asphalt=True, freq=123.5), lat, lon

def cup_coord(lat, lon):
    return f'{int(lat):02d}{(lat-int(lat))*60:06.3f}N,{int(lon):03d}{(lon-int(lon))*60:06.3f}E'

def main(out):
    name = os.path.basename(out.rstrip('/'))
    for sub in ('HeightMaps/22.5m', 'ForestMaps', 'Textures', 'Airports/Textures'): os.makedirs(f'{out}/{sub}', exist_ok=True)
    open(f'{out}/{name}.ini', 'wb').write(b'[General]\r\nVersion=1.02\r\nRealtimeShading=1')
    Hi = np.rint(Hm).astype('<u2')
    trn = np.ascontiguousarray(Hi[:-1:3, :-1:3])                              # 90 m cells [x][y]
    open(f'{out}/{name}.trn', 'wb').write(struct.pack('<II5fII', NX*64, NY*64, 90.0, -90.0, 90.0, E0, N0, ZONE, ord('N')) + trn.tobytes())
    atx, aty = int(AX//TILE), int(AY//TILE)
    tha, fha, oha = [], [], []
    only = os.environ.get('ONLY')                                             # e.g. ONLY=airport to rebuild just the airport files
    for tx in range(NX):
        for ty in range(NY):
            key = f'{tx*1000+ty:06d}'; idx = NY*tx+ty
            d = np.ascontiguousarray(Hi[tx*192:tx*192+193, ty*192:ty*192+193]).tobytes()      # [x][y], y fastest, shared edge posts
            open(f'{out}/HeightMaps/h{tx:02d}{ty:02d}.tr3', 'wb').write(d)
            if (tx, ty) == (atx, aty):                                        # 22.5 m float patch (takes precedence in the game and in the hash)
                g = tx*TILE + np.arange(257, dtype=np.float32)*22.5; g2 = ty*TILE + np.arange(257, dtype=np.float32)*22.5
                d = samp(Hm, g[:, None], g2[None, :]).astype('<f4').tobytes()
                open(f'{out}/HeightMaps/22.5m/h{tx:02d}{ty:02d}.tr3f', 'wb').write(d)
            tha.append(f'{key} {condor_hash(d, idx)}')
            f = (np.arange(512, dtype=np.float32)+0.5)*TILE/512
            d = forest_class(tx*TILE+f[:, None], ty*TILE+f[None, :]).tobytes()                # [i][j]: i from the east edge, j from the south edge
            open(f'{out}/ForestMaps/{tx:02d}{ty:02d}.for', 'wb').write(d)
            fha.append(f'{key} {condor_hash(d, idx)}')
            oha.append(key)
            tp = f'{out}/Textures/t{tx:02d}{ty:02d}.dds'
            if not only and not (os.environ.get('KEEP') and os.path.exists(tp)):   # KEEP=1: resume, do not redo existing textures
                rgb, water = tile_texture(tx, ty, 4096 if (tx, ty) == (atx, aty) else 2048)
                write_dds(tp, rgb, np.where(water, 51, 255).astype(np.uint8) if water.any() else None)
            print('tile', tx, ty, flush=True)
    write_dds(f'{out}/Textures/empty.dds', np.full((256, 256, 3), (95, 135, 60), np.uint8), np.full((256, 256), 255, np.uint8))
    for ext, lines in (('tha', tha), ('fha', fha), ('oha', oha)):
        open(f'{out}/{name}.{ext}', 'wb').write(('\r\n'.join(lines)+'\r\n').encode())
    open(f'{out}/{name}.air', 'wb').write(b''); open(f'{out}/{name}.aha', 'wb').write(b'0\r\n'); open(f'{out}/{name}.obj', 'wb').write(b'')
    # 90 m rasters: thermal map (tm3) and flight-planner map (bmp)
    cx = (np.arange(NX*64, dtype=np.float32)*90)[:, None]; cy = (np.arange(NY*64, dtype=np.float32)*90)[None, :]
    rgb, _, therm = color(np.broadcast_to(cx, trn.shape), np.broadcast_to(cy, trn.shape))
    make_tm3(trn.astype(np.uint16), therm.T).tofile(f'{out}/{name}.tm3')
    W, Hh = trn.shape; rgb = rgb.astype(np.uint8)
    img = np.transpose(rgb, (1, 0, 2))[:, ::-1]                               # rows = y from the south (bottom-up BMP), columns west-left
    bgra = np.dstack([img[..., 2], img[..., 1], img[..., 0], np.full(img.shape[:2], 255, np.uint8)])
    open(f'{out}/{name}.bmp', 'wb').write(struct.pack('<2sIHHIIiiHHIIiiII', b'BM', 54+W*Hh*4, 0, 0, 54, 40, W, Hh, 1, 32, 0, W*Hh*4, 2834, 2834, 0, 0) + np.ascontiguousarray(bgra).tobytes())
    rec, lat, lon = build_airport(out)
    open(f'{out}/{name}.apt', 'wb').write(rec)
    pk = np.unravel_index(int(Hm.argmax()), Hm.shape); plat, plon = utm.inverse(E0-pk[0]*30.0, N0+pk[1]*30.0, ZONE)
    llat, llon = utm.inverse(E0-LAKE_X, N0+float(yv(LAKE_X)), ZONE)
    cup = ['name,code,country,lat,lon,elev,style,rwdir,rwlen,freq,desc',
           f'"{AIRPORT}",{AIRPORT[:8]},,{cup_coord(lat, lon)},{FLOOR:.1f}m,5,{int(round(HEADING)):03d},{RWY_LEN}.0m,123.500,',
           f'"Lake",Lake,,{cup_coord(llat, llon)},{LAKE_Z:.1f}m,1,,,,', f'"Summit",Summit,,{cup_coord(plat, plon)},{float(Hm.max()):.1f}m,1,,,,']
    open(f'{out}/{name}.cup', 'wb').write(('\r\n'.join(cup)+'\r\n').encode())
    print(f'{name}: {NX}x{NY} tiles, heights {Hm.min():.0f}..{Hm.max():.0f} m, airport {AIRPORT} at world ({AX:.0f}, {AY:.1f}) tile ({atx},{aty}) '
          f'lat {lat:.6f} lon {lon:.6f} heading {HEADING} elev {FLOOR}')

if __name__ == '__main__':
    main(sys.argv[1])
