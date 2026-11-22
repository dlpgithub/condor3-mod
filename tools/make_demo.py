#!/usr/bin/env python3
"""Build a complete small Condor 3 demonstration landscape from nothing but this script (no file is copied from a stock landscape).

usage: make_demo.py <Landscapes_dir>/<Name>          e.g.  make_demo.py ~/mnt/Condor3/Landscapes/ProofValley
env:   KEEP=1 keep existing tile textures (resume), ONLY=airport skip tile textures, UTMZONE=n write another UTM zone (test)

Everything a landscape can contain is produced (see docs/TERRAIN_FORMAT.md):
  <N>.ini .trn .tm3 .bmp .tha .fha .oha .aha .air .obj .cup .apt
  HeightMaps/hXXYY.tr3 (+ 22.5m/hXXYY.tr3f for the airport tiles), ForestMaps/XXYY.for, Textures/tXXYY.dds (+ empty.dds)
  Airports/<A>G.c3d, <A>O.c3d, Airports/Textures/<A>/*.dds, World/Objects/*.c3d (+Textures), Autogen/oXXYY.c3d (+Textures)

The landscape: 6 x 5 tiles (34.56 x 28.8 km), a meandering east-west valley (floor ~500 m) between procedural mountains (to ~2700 m),
a lake (texture alpha = water), field patchwork, two forest classes, and two airports:
  Talfeld  - asphalt runway on terrain flattened to a horizontal plane; all ground surface types, material test objects
  Hangfeld - grass strip on a TILTED plane (2 % along, 1 % across the runway): the airport ground follows the terrain
World coordinates as in the game: x = metres WEST of the east edge, y = metres NORTH of the south edge.
"""
import sys, os, math, struct, hashlib
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from terrain_hash import condor_hash
from make_tm3 import make_tm3
from make_airport import box
import c3d, apt, utm, obj2c3d

NX, NY = 6, 5
TILE = 5760.0
SOUTH = os.environ.get('HEMI') == 'S'                 # test: southern hemisphere (UTM northing has a 10 000 km false northing there)
E0, N0, ZONE = 460800.0, (4787200.0 if SOUTH else 5184000.0), int(os.environ.get('UTMZONE', 33))   # UTM easting of the EAST edge, northing of the SOUTH edge
def LL(E, N): return utm.inverse(E, N - (1e7 if SOUTH else 0.0), ZONE)
FLOOR = 500.0
LAKE_X, LAKE_RX, LAKE_RY, LAKE_Z = 7500.0, 1600.0, 550.0, 496.0
MW, MH = NX*192+1, NY*192+1                     # master grid of 30 m posts

def smooth(t): t = np.clip(t, 0, 1); return t*t*(3-2*t)
def yv(x): return NY*TILE/2 + 2500.0*np.sin(np.asarray(x)/6000.0)      # valley axis
def rect_dist(xl, yl, hx, hy): return np.hypot(np.maximum(np.abs(xl)-hx, 0), np.maximum(np.abs(yl)-hy, 0))

class Airport:
    """local frame of the G/O files: +Y = runway heading, +X = left of the heading, origin = .apt position, Z = 0 = terrain height at the origin"""
    def __init__(s, name, ax, length, width, asphalt, gx, gy, h0, sy=0.0, sx=0.0):
        s.name, s.AX, s.AY, s.len, s.wid, s.asphalt, s.gx, s.gy, s.h0, s.sy, s.sx = name, ax, float(yv(ax)), length, width, asphalt, gx, gy, h0, sy, sx
        t = 2500.0/6000.0*math.cos(ax/6000.0)                                 # dy/dx of the valley axis: runway points "down the valley" (east)
        s.heading = round(math.degrees(math.atan2(1.0, -t)) % 360, 2)
        s.c, s.s = math.cos(math.radians(s.heading)), math.sin(math.radians(s.heading))
        s.hw, s.hl = width/2, length/2
        s.tile = (int(s.AX//TILE), int(s.AY//TILE))
    def to_local(s, X, Y):                                                    # inverse of world = origin + x*(c,s) + y*(-s,c)
        dW = X-s.AX; dN = Y-s.AY
        return dW*s.c + dN*s.s, -dW*s.s + dN*s.c
    def to_world(s, x, y): return s.AX + x*s.c - y*s.s, s.AY + x*s.s + y*s.c
    def z(s, x, y): return s.sy*np.asarray(y, np.float32) + s.sx*np.asarray(x, np.float32)      # local height of the ground plane
    def latlon(s): return LL(E0-s.AX, N0+s.AY)

A1 = Airport('Talfeld', 20000.0, 900, 30, True, 300.0, 700.0, FLOOR)
A2 = Airport('Hangfeld', 24000.0, 600, 30, False, 150.0, 450.0, 504.0, sy=0.02, sx=0.01)
AIRPORTS = [A1, A2]
APRON2 = (30.0, 60.0, -20.0, 20.0)              # small asphalt apron of Hangfeld (local x0, x1, y0, y1)

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
    for ap in AIRPORTS:                                                        # airports: replace the terrain by an exact plane, blended outwards
        xl, yl = ap.to_local(X, Y)
        w = 1-smooth(rect_dist(xl, yl, ap.gx+120, ap.gy+80)/350); H = H*(1-w) + (ap.h0 + ap.z(xl, yl))*w
    H = H.astype(np.float32)
    gx, gy = np.gradient(H, 30.0)
    return H, gx.astype(np.float32), gy.astype(np.float32), spectral((MW, MH), 3.0, 14)
Hm, Gx, Gy, N1 = build_master()
NT = spectral((512, 512), 2.0, 15)                                            # periodic detail noise

def ground(X, Y):
    """exact terrain height: inside an airport's ground rectangle the analytic plane (what the tr3f holds), else the 30 m master"""
    h = samp(Hm, X, Y)
    for ap in AIRPORTS:
        xl, yl = ap.to_local(np.asarray(X, np.float32), np.asarray(Y, np.float32))
        h = np.where(rect_dist(xl, yl, ap.gx+100, ap.gy+60) == 0, ap.h0 + ap.z(xl, yl), h)
    return h

def lake_r(X, Y): return np.sqrt(((X-LAKE_X)/LAKE_RX)**2 + ((Y-yv(X))/LAKE_RY)**2)

def forest_class(X, Y, h=None, slope=None):
    """0 none, 1 conifer, 2 deciduous, 3 both (the values of the .for file)"""
    if h is None:
        h = samp(Hm, X, Y); slope = np.degrees(np.arctan(np.hypot(samp(Gx, X, Y), samp(Gy, X, Y))))
    n = samp(N1, X, Y)
    con = (h > 780) & (h < 1800) & (slope < 40) & (n > 0.40)
    dec = (h > 512) & (h <= 900) & (slope > 4) & (n > 0.46)
    ok = lake_r(X, Y) > 1.25
    for ap in AIRPORTS:
        xl, yl = ap.to_local(X, Y); ok &= rect_dist(xl, yl, ap.gx+120, ap.gy+80) > 250
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
    a = X*0.94+Y*0.34; b = -X*0.34+Y*0.94                                    # field patchwork on the valley floor
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
    for ap in AIRPORTS:                                                      # airports: mown grass, runway, apron, taxiway
        xl, yl = ap.to_local(X, Y)
        w = (1-smooth(rect_dist(xl, yl, ap.gx-20, ap.gy-20)/60))[..., None]
        col = col*(1-w) + np.array([105, 145, 62], np.float32)*(1+nz*0.3)*w
        if ap.asphalt:
            asph = (rect_dist(xl, yl, ap.hw, ap.hl) == 0) | (rect_dist(xl-75, yl, 35, 40) == 0) | (rect_dist(xl-27, yl, 13, 6) == 0)
        else:
            strip = rect_dist(xl, yl, ap.hw, ap.hl) == 0
            col[strip] = (np.array([122, 152, 70], np.float32)*(1+nz*0.3))[strip]
            asph = (xl > APRON2[0]) & (xl < APRON2[1]) & (yl > APRON2[2]) & (yl < APRON2[3])
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

# ---------------- mesh helpers ----------------
def sphere(cx, cy, cz, r, n=14):
    V = []; I = []
    for i in range(n+1):
        th = math.pi*i/n
        for j in range(2*n+1):
            ph = math.pi*j/n; nx, ny, nz = math.sin(th)*math.cos(ph), math.sin(th)*math.sin(ph), math.cos(th)
            V.append([cx+r*nx, cy+r*ny, cz+r*nz, nx, ny, nz, j/(2*n), i/n])
    for i in range(n):
        for j in range(2*n):
            a = i*(2*n+1)+j; b = a+2*n+1
            I += [a, b, b+1, a, b+1, a+1]           # outward CCW
    return np.array(V, np.float32), np.array(I, np.uint32)

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

def merge(parts):
    V = []; I = []; n = 0
    for v, i in parts: V.append(v); I.append(i+n); n += len(v)
    return np.concatenate(V), np.concatenate(I)

def moved(vi, dx=0, dy=0, dz=0, rot=0.0):
    v, i = vi; v = v.copy(); c, s = math.cos(rot), math.sin(rot)
    x, y = v[:, 0].copy(), v[:, 1].copy(); v[:, 0] = x*c-y*s+dx; v[:, 1] = x*s+y*c+dy; v[:, 2] += dz
    nx, ny = v[:, 3].copy(), v[:, 4].copy(); v[:, 3] = nx*c-ny*s; v[:, 4] = nx*s+ny*c
    return v, i

def write_bgra(path, rgb, alpha):
    h, w = rgb.shape[:2]
    bgra = np.dstack([rgb[..., 2], rgb[..., 1], rgb[..., 0], alpha])
    hdr = b'DDS ' + struct.pack('<31I', 124, 0xA1007, h, w, w*h*4, 0, 1, *([0]*11), 32, 65, 0, 32, 0xFF0000, 0xFF00, 0xFF, 0xFF000000, 0x401008, 0, 0, 0, 0)
    open(path, 'wb').write(hdr + np.ascontiguousarray(bgra).tobytes())

# ---------------- airports ----------------
def plane_mesh(rects, uv, ap):
    """rectangles (x0, x1, y0, y1) lying ON the airport ground plane -> one mesh (CCW, normal = plane normal)"""
    n = np.array([-ap.sx, -ap.sy, 1.0]); n /= np.linalg.norm(n); V = []; I = []
    for x0, x1, y0, y1 in rects:
        b = len(V)
        for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            u, v = uv(x, y); V.append([x, y, float(ap.z(x, y)), *n, u, v])
        I += [b, b+1, b+2, b, b+2, b+3]
    return np.array(V, np.float32), np.array(I, np.uint32)

def cells(xs, ys, pred):
    return [(xs[i], xs[i+1], ys[j], ys[j+1]) for i in range(len(xs)-1) for j in range(len(ys)-1) if pred((xs[i]+xs[i+1])/2, (ys[j]+ys[j+1])/2)]

def on_ground(ap, mesh, x, y):
    """lift an object mesh built at z=0 onto the ground plane at local (x, y)"""
    return moved(mesh, dz=float(ap.z(x, y)))

GRAVEL = (150.0, 170.0, -20.0, 0.0)

def build_airport(out, ap):
    td = f'{out}/Airports/Textures/{ap.name}'; os.makedirs(td, exist_ok=True)
    tex = f'Textures/{ap.name}/'
    # --- sub-texture: NORTH-UP aerial photo like the stock airports (u -> east, v -> south), alpha = grass length ---
    Eh = abs(ap.gx*ap.c)+abs(ap.gy*ap.s); Nh = abs(ap.gx*ap.s)+abs(ap.gy*ap.c); res = 0.5
    nu, nv = int(2*Eh/res)//4*4, int(2*Nh/res)//4*4
    e = (-Eh + (np.arange(nu, dtype=np.float32)+0.5)/nu*2*Eh)[None, :]
    img = np.empty((nv, nu, 3), np.uint8); alpha = np.empty((nv, nu), np.uint8)
    for r0 in range(0, nv, 400):
        n = (Nh - (np.arange(r0, min(r0+400, nv), dtype=np.float32)+0.5)/nv*2*Nh)[:, None]
        W = np.broadcast_to(ap.AX - e, (n.shape[0], nu)); N = np.broadcast_to(ap.AY + n, (n.shape[0], nu))
        c, _, th = color(W, N); xl, yl = ap.to_local(W, N)
        a = np.full(W.shape, 255, np.uint8)                       # 255 = long grass
        a[np.abs(xl) < 60] = 96                                   # mown strip along the runway: short grass
        a[th == 200] = 0                                          # asphalt painted in the photo: no grass
        img[r0:r0+n.shape[0]] = c.astype(np.uint8); alpha[r0:r0+n.shape[0]] = a
    write_bgra(f'{td}/{ap.name}_Grass.dds', img, alpha)
    sub = f'Textures\\{ap.name}\\{ap.name}_Grass.dds'
    uv_sub = lambda x, y: ((-(x*ap.c - y*ap.s) + Eh)/(2*Eh), (Nh - (x*ap.s + y*ap.c))/(2*Nh))
    # runway-aligned mapping like the stock muck textures: u along the runway (local y), v across. The game derives the scale of the
    # tiled detail texture from du/dy and dv/dx of Asphalt/AsphaltPaint objects (regression over the vertices): both must be non-zero.
    uv_rwy = lambda x, y: ((y+ap.hl)/ap.len, 0.5 - x/ap.len)
    hw, hl = ap.hw, ap.hl
    G = []
    def add(name, rects, uv, texture='', color=(1, 1, 1, 1)):
        v, i = plane_mesh(rects, uv, ap); G.append(dict(name=name, texture=texture, color=color, params=(0, 0, 0), vertices=v, indices=i))
    if ap.asphalt:
        rng = np.random.default_rng(3); mk = np.full((1024, 1024), 236.0, np.float32) + rng.normal(0, 3, (1024, 1024)).astype(np.float32)
        for k in range(60):                                       # muck: almost white, tyre marks in the touch-down zones
            end = rng.integers(0, 2); u0 = rng.uniform(0.04, 0.2); ln = rng.uniform(0.02, 0.08); v0 = 0.5 + rng.uniform(-9, 9)/ap.len
            c0, c1 = int((u0 if end == 0 else 1-u0-ln)*1024), int(((u0+ln) if end == 0 else 1-u0)*1024); r = int(v0*1024)
            mk[r:r+1, c0:c1] -= rng.uniform(40, 110)
        write_dds(f'{td}/{ap.name}_Muck.dds', np.repeat(np.clip(mk, 0, 255).astype(np.uint8)[..., None], 3, 2))
        xs = sorted(set(list(np.arange(-ap.gx, ap.gx+1, 50.0)) + [-hw, hw, GRAVEL[0], GRAVEL[1]]))
        ys = sorted(set(list(np.arange(-ap.gy, ap.gy+1, 50.0)) + [-hl, hl, GRAVEL[2], GRAVEL[3]]))
        on_rwy = lambda x, y: abs(x) < hw and abs(y) < hl
        on_gravel = lambda x, y: GRAVEL[0] < x < GRAVEL[1] and GRAVEL[2] < y < GRAVEL[3]
        paint = [(-hw, -hw+1.2, -hl, hl), (hw-1.2, hw, -hl, hl)]                                         # edge lines
        paint += [(-0.45, 0.45, y0, y0+22) for y0 in np.arange(-hl+40, hl-60, 36.0)]                      # centre line
        for sgn in (-1, 1):                                                                               # threshold bars
            for k in (-4, -3, -2, -1, 1, 2, 3, 4):
                yb = sorted((sgn*(hl-30), sgn*(hl-8))); paint.append((k*hw/5-0.9, k*hw/5+0.9, yb[0], yb[1]))
        gp = [(min(sx*(hw+6), sx*(hw+8)), max(sx*(hw+6), sx*(hw+8)), y0-1.5, y0+1.5) for sx in (-1, 1) for y0 in np.arange(-hl, hl+1, 100.0)]
        # surfaces of a G file must not overlap: Grass3D has holes where Asphalt and Gravel are; paint lies on top (coplanar)
        add('Grass3D', cells(xs, ys, lambda x, y: not on_rwy(x, y) and not on_gravel(x, y)), uv_sub, sub)
        add('Asphalt', cells(xs, ys, on_rwy), uv_rwy, f'Textures\\{ap.name}\\{ap.name}_Muck.dds', (0.8, 0.8, 0.8, 1))
        add('AsphaltPaint', paint, uv_rwy, '', (0.9, 0.9, 0.9, 1))
        add('AsphaltPaintYellow', [(-hw+1.2, hw-1.2, -0.5, 0.5)], uv_rwy, '', (0.6, 0.6, 0, 1))
        add('GrassPaint', gp, uv_sub, '', (0.8, 0.8, 0.8, 1))
        add('Gravel', cells(xs, ys, on_gravel), uv_sub, '', (0.8, 0.8, 0.78, 1))
    else:
        xs = sorted(set(list(np.arange(-ap.gx, ap.gx+1, 50.0)) + [APRON2[0], APRON2[1]]))
        ys = sorted(set(list(np.arange(-ap.gy, ap.gy+1, 50.0)) + [APRON2[2], APRON2[3]]))
        on_apron = lambda x, y: APRON2[0] < x < APRON2[1] and APRON2[2] < y < APRON2[3]
        gp = [(min(sx*(hw), sx*(hw+2)), max(sx*(hw), sx*(hw+2)), y0-2, y0+2) for sx in (-1, 1) for y0 in np.arange(-hl, hl+1, 100.0)]
        gp += [(-hw, hw, sgn*hl-1, sgn*hl+1) for sgn in (-1, 1)]
        bad = os.environ.get('RED_TEST')      # u only along x, v only along y: du/dy = dv/dx = 0 -> detail scale divides by zero
        add('Grass3D', cells(xs, ys, lambda x, y: not on_apron(x, y)), uv_sub, sub)
        add('Asphalt', cells(xs, ys, on_apron), (lambda x, y: (x/30.0, y/30.0)) if bad else (lambda x, y: (y/60.0, 0.5 - x/60.0)), '', (0.7, 0.7, 0.7, 1))
        add('GrassPaint', gp, uv_sub, '', (0.8, 0.8, 0.8, 1))
    open(f'{out}/Airports/{ap.name}G.c3d', 'wb').write(c3d.write(c3d.build(G)))
    # --- O file (everything in it is solid for collisions) ---
    P = (0.5, 2.5, 0)
    def tri(x, y, z, r):      # windsock proxy: equilateral triangle, centre = attachment point, sock length = 100 x circumradius
        z = z + float(ap.z(x, y))
        return np.array([[x, y, z, -1, 0, 0, 0.75, 0.51], [x-r*0.866, y+r*0.5, z, -1, 0, 0, 0.958, 0.87], [x-r*0.866, y-r*0.5, z, -1, 0, 0, 0.542, 0.87]], np.float32), np.array([0, 2, 1], np.uint32)
    wall = np.zeros((256, 256, 3), np.uint8); wall[:] = (150, 155, 160); wall[:, ::16] = (120, 125, 130)
    wall[96:, 48:208] = (70, 80, 110); wall[96:, 126:130] = (40, 45, 60); wall[:24] = (190, 60, 50)
    write_dds(f'{td}/Hangar.dds', wall)
    if ap.asphalt:
        holes = np.zeros((256, 256, 3), np.uint8); holes[:] = (230, 200, 60); ha = np.where(((np.arange(256)[:, None]//32)+(np.arange(256)[None, :]//32)) % 2, 255, 0).astype(np.uint8)
        write_dds(f'{td}/Holes.dds', holes, ha)
        O = [dict(name='Hangar', texture=tex+'Hangar.dds', color=(1, 1, 1, 1), params=P, mesh=box(75, 60, 0.0, 30, 24, 7)),
             dict(name='HangarRoof', texture='', color=(0.55, 0.2, 0.15, 1), params=P, mesh=roof(75, 60, 7.0, 30, 24, 3.5)),
             dict(name='WindsockPole', texture='', color=(0.9, 0.9, 0.9, 1), params=P, mesh=merge([box(40, -150, 0.0, 0.2, 0.2, 7.9), box(40, 150, 0.0, 0.3, 0.3, 12)])),
             dict(name='Windsack1', texture='', color=(0.8, 0.8, 0.8, 1), params=(0.8, 5.0, 0), mesh=tri(40, -150, 7.9, 0.045)),
             dict(name='Windsack2', texture='', color=(0.8, 0.8, 0.8, 1), params=(0.8, 5.0, 0), mesh=tri(40, 150, 12.0, 0.10))]
        red = (0.8, 0.1, 0.1)                 # material test row (Spec/Shiny/Env do nothing on ground objects; alpha blends; texture alpha is tested)
        tests = [('T1_Diffuse', '', (*red, 1), (0, 0, 0)), ('T2_Spec', '', (*red, 1), (1, 50, 0)), ('T3_Env', '', (*red, 1), (0, 0, 1)),
                 ('T4_Alpha', '', (*red, 0.35), (0, 0, 0)), ('T6_TexTint', tex+'Hangar.dds', (0.4, 1, 0.4, 1), (0, 0, 0)),
                 ('T7_TexHoles', tex+'Holes.dds', (1, 1, 1, 1), (0, 0, 0)), ('T8_TexSpec', tex+'Hangar.dds', (1, 1, 1, 1), (1, 50, 0)), ('zT5_Alpha', '', (*red, 0.35), (0, 0, 0))]
        late = []
        for k, (nm, tx, col, par) in enumerate(tests):
            x = -80 - 12*k
            ob = dict(name=nm, texture=tx, color=col, params=par, mesh=sphere(x, -150, 3.5, 3.0))
            back = dict(name=nm+'Back', texture='', color=(0.1, 0.2, 0.9, 1), params=(0, 0, 0), mesh=box(x, -142, 0, 2, 2, 7))
            if nm.startswith('z'): late += [back, ob]             # stock convention: transparent objects are named z... and sorted last
            else: O += [ob, back]
        O += late
    else:
        O = [dict(name='Shed', texture=tex+'Hangar.dds', color=(1, 1, 1, 1), params=P, mesh=on_ground(ap, box(45, 35, 0.0, 16, 12, 5), 45, 35)),
             dict(name='ShedRoof', texture='', color=(0.3, 0.45, 0.3, 1), params=P, mesh=on_ground(ap, roof(45, 35, 5.0, 16, 12, 2.5), 45, 35)),
             dict(name='WindsockPole', texture='', color=(0.9, 0.9, 0.9, 1), params=P, mesh=on_ground(ap, box(25, -120, 0.0, 0.2, 0.2, 7.9), 25, -120)),
             dict(name='Windsack1', texture='', color=(0.8, 0.8, 0.8, 1), params=(0.8, 5.0, 0), mesh=tri(25, -120, 7.9, 0.045))]
    for ob in O: ob['vertices'], ob['indices'] = ob.pop('mesh')
    open(f'{out}/Airports/{ap.name}O.c3d', 'wb').write(c3d.write(c3d.build(O)))
    lat, lon = ap.latlon()
    elev = ap.h0 + float(os.environ.get('APT_ELEV_OFFSET', 0))    # informational only: the game puts Z=0 on the terrain
    return apt.pack_one(ap.name, lat, lon, elev, ap.heading, ap.len, ap.wid, asphalt=ap.asphalt, freq=123.5)

# ---------------- scenery: world objects, autogen, airspace ----------------
def arrow_house():
    """asymmetric test object: house 10 x 16 x 5 with gable roof, a red 'nose' block on its +Y end and a green block on its +X side"""
    return [dict(name='Body', texture='Textures/TestWall.dds', color=(1, 1, 1, 1), params=(0, 0, 0), mesh=box(0, 0, 0, 10, 16, 5)),
            dict(name='Roof', texture='', color=(0.35, 0.35, 0.4, 1), params=(0, 0, 0), mesh=moved(roof(0, 0, 5.0, 16, 10, 3), rot=math.pi/2)),
            dict(name='Nose', texture='', color=(0.9, 0.1, 0.1, 1), params=(0, 0, 0), mesh=box(0, 11, 0, 4, 6, 3)),
            dict(name='LeftGreen', texture='', color=(0.1, 0.8, 0.1, 1), params=(0, 0, 0), mesh=box(7, 0, 0, 4, 4, 2))]

TOWER_OBJ = """# small control tower written the way Blender exports it (Forward = Y, Up = Z): +X of this file becomes C3D +Y
mtllib Tower.mtl
o Shaft
usemtl concrete
v -3 -3 0\nv 3 -3 0\nv 3 3 0\nv -3 3 0\nv -3 -3 12\nv 3 -3 12\nv 3 3 12\nv -3 3 12
vt 0 0\nvt 1 0\nvt 1 1\nvt 0 1
f 1/1 2/2 6/3 5/4\nf 2/1 3/2 7/3 6/4\nf 3/1 4/2 8/3 7/4\nf 4/1 1/2 5/3 8/4\nf 5/1 6/2 7/3 8/4
o Cabin
usemtl glass
v -4 -4 12\nv 4 -4 12\nv 4 4 12\nv -4 4 12\nv -4 -4 15\nv 4 -4 15\nv 4 4 15\nv -4 4 15
f 9 10 14 13\nf 10 11 15 14\nf 11 12 16 15\nf 12 9 13 16\nf 13 14 15 16\nf 12 11 10 9
o Balcony
usemtl orange
v 3 -1.5 9\nv 6 -1.5 9\nv 6 1.5 9\nv 3 1.5 9\nv 3 -1.5 9.6\nv 6 -1.5 9.6\nv 6 1.5 9.6\nv 3 1.5 9.6
f 17 18 22 21\nf 18 19 23 22\nf 19 20 24 23\nf 20 17 21 24\nf 21 22 23 24\nf 20 19 18 17
"""
TOWER_MTL = "newmtl concrete\nKd 1 1 1\nd 1\nmap_Kd TestWall.dds\nnewmtl glass\nKd 0.3 0.5 0.7\nd 0.45\nnewmtl orange\nKd 1 0.5 0\nd 1\n"

def build_scenery(out, name):
    wd = f'{out}/World/Objects'; os.makedirs(wd+'/Textures', exist_ok=True)
    wall = np.zeros((128, 128, 3), np.uint8); wall[:] = (225, 215, 190); wall[40:90, 20:50] = (60, 70, 90); wall[40:90, 78:108] = (60, 70, 90)
    write_dds(f'{wd}/Textures/TestWall.dds', wall)
    objs = arrow_house()
    for ob in objs: ob['vertices'], ob['indices'] = ob.pop('mesh')
    open(f'{wd}/TestArrow.c3d', 'wb').write(c3d.write(c3d.build(objs)))
    open(f'{wd}/Tower.obj', 'w').write(TOWER_OBJ); open(f'{wd}/Tower.mtl', 'w').write(TOWER_MTL)        # OBJ -> C3D with our own converter
    open(f'{wd}/Tower.c3d', 'wb').write(c3d.write(obj2c3d.convert(f'{wd}/Tower.obj', texdir='Textures/')))
    # <name>.obj = placement list, 152-byte records: f32 x (west), y (north), z (ignored: the game uses the terrain height),
    # f32 scale, f32 rotation [rad, clockwise seen on the map = heading of the model's +Y axis], ShortString[131] file name.
    # The model is searched in Landscapes/<L>/World/Objects/ first, then in the game's own World/Objects/.
    recs = b''
    for (xl, yl, scale, rot, nm) in ((-200, -60, 1, 0, 'TestArrow.c3d'), (-235, -60, 1, math.pi/2, 'TestArrow.c3d'), (-270, -60, 1, math.pi, 'TestArrow.c3d'),
                                     (-315, -60, 2, 0, 'TestArrow.c3d'), (-365, -60, 1, 0, 'Tower.c3d'), (-400, -60, 1, 0, 'Winch.c3d'),
                                     (-900, 0, 1, 0, 'TestArrow.c3d'), (-1100, 300, 1, math.pi, 'TestArrow.c3d')):
        W, N = A1.to_world(xl, yl); nb = nm.encode()
        recs += struct.pack('<5f', W, N, 0.0, scale, rot) + bytes([len(nb)]) + nb.ljust(131, b'\0')
    open(f'{out}/{name}.obj', 'wb').write(recs)
    # autogen tile of the Talfeld patch: coordinates relative to the tile CENTRE (x west, y north), z = absolute altitude
    ad = f'{out}/Autogen'; os.makedirs(ad+'/Textures', exist_ok=True)
    brick = np.zeros((128, 128, 3), np.uint8); brick[:] = (170, 90, 70); brick[::16] = (200, 190, 180); brick[:, ::32] = (200, 190, 180)
    write_dds(f'{ad}/Textures/Brick.dds', brick)
    atx, aty = A1.tile; cx, cy = (atx+0.5)*TILE, (aty+0.5)*TILE
    parts = []; roofs = []
    for k in range(6):
        W, N = A1.to_world(230, -400+80*k); z = float(ground(W, N))
        parts.append(box(W-cx, N-cy, z, 12, 10, 6)); roofs.append(roof(W-cx, N-cy, z+6, 12, 10, 3))
    A = [dict(name='Houses', texture='Textures\\Brick.dds', color=(1, 1, 1, 1), params=(0, 0, 0)), dict(name='Roofs', texture='', color=(0.5, 0.25, 0.2, 1), params=(0, 0, 0))]
    for ob, m in zip(A, (merge(parts), merge(roofs))): ob['vertices'], ob['indices'] = m
    d = c3d.write(c3d.build(A)); open(f'{ad}/o{atx:02d}{aty:02d}.c3d', 'wb').write(d)
    oha = [f'{tx*1000+ty:06d}' + (' '+hashlib.md5(d).hexdigest() if (tx, ty) == (atx, aty) else '') for tx in range(NX) for ty in range(NY)]
    open(f'{out}/{name}.oha', 'wb').write(('\r\n'.join(oha)+'\r\n').encode())
    # airspace: OpenAir text; the game refuses the landscape unless <name>.aha holds the hash of this file
    lat, lon = A1.latlon()
    def dms(v, w): d_ = int(v); m = int((v-d_)*60); s_ = int(round(((v-d_)*60-m)*60)); return f'{d_:0{w}d}:{m:02d}:{min(s_, 59):02d}'
    ns = 'S' if lat < 0 else 'N'
    air = f'* {name}\r\n\r\nAC D\r\nAN TALFELD CTR\r\nAL GND\r\nAH FL 65\r\nV X={dms(abs(lat), 2)} {ns} {dms(lon, 3)} E\r\nDC 2\r\n'.encode()
    open(f'{out}/{name}.air', 'wb').write(air); open(f'{out}/{name}.aha', 'wb').write(f'{condor_hash(air, 0)}\r\n'.encode())

def cup_coord(lat, lon):
    h = 'S' if lat < 0 else 'N'; lat = abs(lat)
    return f'{int(lat):02d}{(lat-int(lat))*60:06.3f}{h},{int(lon):03d}{(lon-int(lon))*60:06.3f}E'

def main(out):
    name = os.path.basename(out.rstrip('/'))
    for sub in ('HeightMaps/22.5m', 'ForestMaps', 'Textures', 'Airports/Textures'): os.makedirs(f'{out}/{sub}', exist_ok=True)
    open(f'{out}/{name}.ini', 'wb').write(b'[General]\r\nVersion=1.02\r\nRealtimeShading=1')
    Hi = np.rint(Hm).astype('<u2')
    trn = np.ascontiguousarray(Hi[:-1:3, :-1:3])                              # 90 m cells [x][y]
    # header: width, height, f32 cell size, f32 dx (-90: x counts west), f32 dy (+90), f32 E0, f32 N0, UTM zone, hemisphere char
    # (the game's "calibration" check only wants 80 <= |dx|, |dy| <= 100)
    open(f'{out}/{name}.trn', 'wb').write(struct.pack('<II5fII', NX*64, NY*64, 90.0, -90.0, 90.0, E0, N0, ZONE, ord('S' if SOUTH else 'N')) + trn.tobytes())
    apt_tiles = {ap.tile for ap in AIRPORTS}
    tha, fha = [], []
    only = os.environ.get('ONLY')
    for tx in range(NX):
        for ty in range(NY):
            key = f'{tx*1000+ty:06d}'; idx = NY*tx+ty
            d = np.ascontiguousarray(Hi[tx*192:tx*192+193, ty*192:ty*192+193]).tobytes()      # [x][y], y fastest, shared edge posts
            open(f'{out}/HeightMaps/h{tx:02d}{ty:02d}.tr3', 'wb').write(d)
            if (tx, ty) in apt_tiles:                                         # 22.5 m float patch: wins over the .tr3 in the game and in the .tha hash
                g = tx*TILE + np.arange(257, dtype=np.float32)*22.5; g2 = ty*TILE + np.arange(257, dtype=np.float32)*22.5
                d = ground(np.broadcast_to(g[:, None], (257, 257)), np.broadcast_to(g2[None, :], (257, 257))).astype('<f4').tobytes()
                open(f'{out}/HeightMaps/22.5m/h{tx:02d}{ty:02d}.tr3f', 'wb').write(d)
            tha.append(f'{key} {condor_hash(d, idx)}')
            f = (np.arange(512, dtype=np.float32)+0.5)*TILE/512
            d = forest_class(tx*TILE+f[:, None], ty*TILE+f[None, :]).tobytes()                # [i][j]: i from the east edge, j from the south edge
            open(f'{out}/ForestMaps/{tx:02d}{ty:02d}.for', 'wb').write(d)
            fha.append(f'{key} {condor_hash(d, idx)}')
            tp = f'{out}/Textures/t{tx:02d}{ty:02d}.dds'
            redo = os.environ.get('TILES') and f'{tx},{ty}' in os.environ['TILES'].split(';')
            if redo or (not only and not (os.environ.get('KEEP') and os.path.exists(tp))):
                rgb, water = tile_texture(tx, ty, 4096 if (tx, ty) in apt_tiles else 2048)
                write_dds(tp, rgb, np.where(water, 51, 255).astype(np.uint8) if water.any() else None)   # water = alpha < 0.8 (DXT3)
            print('tile', tx, ty, flush=True)
    write_dds(f'{out}/Textures/empty.dds', np.full((256, 256, 3), (95, 135, 60), np.uint8), np.full((256, 256), 255, np.uint8))
    for ext, lines in (('tha', tha), ('fha', fha)):
        open(f'{out}/{name}.{ext}', 'wb').write(('\r\n'.join(lines)+'\r\n').encode())
    build_scenery(out, name)
    # 90 m rasters: thermal map (tm3) and flight-planner map (bmp)
    cx = (np.arange(NX*64, dtype=np.float32)*90)[:, None]; cy = (np.arange(NY*64, dtype=np.float32)*90)[None, :]
    rgb, _, therm = color(np.broadcast_to(cx, trn.shape), np.broadcast_to(cy, trn.shape))
    make_tm3(trn.astype(np.uint16), therm.T).tofile(f'{out}/{name}.tm3')
    W, Hh = trn.shape; rgb = rgb.astype(np.uint8)
    img = np.transpose(rgb, (1, 0, 2))[:, ::-1]                               # rows = y from the south (bottom-up BMP), columns west-left
    bgra = np.dstack([img[..., 2], img[..., 1], img[..., 0], np.full(img.shape[:2], 255, np.uint8)])
    open(f'{out}/{name}.bmp', 'wb').write(struct.pack('<2sIHHIIiiHHIIiiII', b'BM', 54+W*Hh*4, 0, 0, 54, 40, W, Hh, 1, 32, 0, W*Hh*4, 2834, 2834, 0, 0) + np.ascontiguousarray(bgra).tobytes())
    open(f'{out}/{name}.apt', 'wb').write(b''.join(build_airport(out, ap) for ap in AIRPORTS))
    pk = np.unravel_index(int(Hm.argmax()), Hm.shape); plat, plon = LL(E0-pk[0]*30.0, N0+pk[1]*30.0)
    llat, llon = LL(E0-LAKE_X, N0+float(yv(LAKE_X)))
    cup = ['name,code,country,lat,lon,elev,style,rwdir,rwlen,freq,desc']
    for ap in AIRPORTS:
        cup.append(f'"{ap.name}",{ap.name[:8]},,{cup_coord(*ap.latlon())},{ap.h0:.1f}m,{5 if ap.asphalt else 2},{int(round(ap.heading)):03d},{ap.len}.0m,123.500,')
    cup += [f'"Lake",Lake,,{cup_coord(llat, llon)},{LAKE_Z:.1f}m,1,,,,', f'"Summit",Summit,,{cup_coord(plat, plon)},{float(Hm.max()):.1f}m,1,,,,']
    open(f'{out}/{name}.cup', 'wb').write(('\r\n'.join(cup)+'\r\n').encode())
    print(f'{name}: {NX}x{NY} tiles, zone {ZONE}, heights {Hm.min():.0f}..{Hm.max():.0f} m')
    for ap in AIRPORTS:
        lat, lon = ap.latlon(); print(f'  {ap.name}: world ({ap.AX:.0f}, {ap.AY:.1f}) tile {ap.tile} lat {lat:.6f} lon {lon:.6f} heading {ap.heading} elev {ap.h0}')

if __name__ == '__main__':
    main(sys.argv[1])
