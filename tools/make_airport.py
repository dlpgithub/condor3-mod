#!/usr/bin/env python3
"""Build a complete custom Condor 3 airport inside a landscape using only our own tools.

usage: make_airport.py <landscape_dir> <Name> <lat> <lon> <heading_deg> [length=900] [width=50] [--append]

Local frame of G/O .c3d files (derived from the stock files by an exact affine fit of the Grass3D UVs, see docs/TERRAIN_FORMAT.md):
    +Y = along the runway heading (clockwise from north), +X = LEFT of the heading, +Z up, origin = .apt lat/lon, Z=0 = .apt elevation.
In Condor world coordinates (x west, y north):  world = origin + x*(cosH, sinH) + y*(-sinH, cosH).

Writes Airports/<Name>G.c3d (Grass3D ground with the runway painted into the photo), <Name>O.c3d (four corner markers), Airports/Textures/<Name>/
(<Name>_Grass.dds uncompressed aerial photo cropped from the landscape tiles, <Name>_Muck.dds copied from a stock airport if available)
and writes <landscape>/<landscape>.apt with this single airport (--append keeps existing records)."""
import sys, os, math, struct, glob, shutil
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c3d, apt, utm
from make_tm3 import load_trn

TILE = 5760.0

def read_trn_header(path):
    d = open(path, 'rb').read(36)
    w, h = struct.unpack_from('<II', d, 0)
    E0, N0 = struct.unpack_from('<ff', d, 20)
    return w, h, E0, N0

class Terrain:
    def __init__(self, ld):
        p = glob.glob(os.path.join(ld, '*.trn'))[0]
        self.w, self.h, self.E0, self.N0 = read_trn_header(p)
        self.H = load_trn(p).astype(np.float64)          # [x][y], 90 m cells, x west, y north
    def height(self, W, N):
        fx = np.clip(np.asarray(W) / 90.0, 0, self.H.shape[0]-1.001); fy = np.clip(np.asarray(N) / 90.0, 0, self.H.shape[1]-1.001)
        x0 = fx.astype(int); y0 = fy.astype(int); tx = fx-x0; ty = fy-y0
        H = self.H
        return (H[x0, y0]*(1-tx)*(1-ty) + H[x0+1, y0]*tx*(1-ty) + H[x0, y0+1]*(1-tx)*ty + H[x0+1, y0+1]*tx*ty)
    def world(self, lat, lon):
        E, N = utm.forward(lat, lon, 33)
        return self.E0 - E, N - self.N0

def local_to_world(W0, N0, hd, x, y):
    h = math.radians(hd); c, s = math.cos(h), math.sin(h)
    return W0 + x*c - y*s, N0 + x*s + y*c

def decode_dxt(path):
    """mip 0 of a DXT1/DXT3 DDS -> (H,W,3) uint8 (alpha ignored)"""
    d = open(path, 'rb').read()
    h, w = struct.unpack_from('<II', d, 12); fourcc = d[84:88]
    bs = 8 if fourcc == b'DXT1' else 16
    data = np.frombuffer(d, np.uint8, offset=128, count=(h//4)*(w//4)*bs).reshape(h//4, w//4, bs)
    c = data[..., bs-8:].astype(np.uint32)
    c0 = c[..., 0] | (c[..., 1] << 8); c1 = c[..., 2] | (c[..., 3] << 8)
    def rgb(v): return np.stack([((v >> 11) & 31)*255//31, ((v >> 5) & 63)*255//63, (v & 31)*255//31], -1).astype(np.float32)
    p0, p1 = rgb(c0), rgb(c1)
    pal = np.stack([p0, p1, (2*p0+p1)/3, (p0+2*p1)/3], -2)
    bits = c[..., 4] | (c[..., 5] << 8) | (c[..., 6] << 16) | (c[..., 7] << 24)
    out = np.zeros((h//4, w//4, 4, 4, 3), np.float32)
    for k in range(16):
        sel = ((bits >> (2*k)) & 3).astype(int)
        out[:, :, k//4, k % 4] = np.take_along_axis(pal, sel[..., None, None].repeat(3, -1), -2)[..., 0, :]
    return out.transpose(0, 2, 1, 3, 4).reshape(h, w, 3).astype(np.uint8)

def write_bgra_dds(path, rgb):
    h, w = rgb.shape[:2]
    bgra = np.dstack([rgb[..., 2], rgb[..., 1], rgb[..., 0], np.full((h, w), 255, np.uint8)])
    hdr = b'DDS ' + struct.pack('<31I', 124, 0xA1007, h, w, w*h*4, 0, 1, *([0]*11), 32, 65, 0, 32, 0xFF0000, 0xFF00, 0xFF, 0xFF000000, 0x401008, 0, 0, 0, 0)
    open(path, 'wb').write(hdr + bgra.tobytes())

def photo(ld, W0, N0, hd, xmin, xmax, ymin, ymax, res=1.0, runway=None):
    """aerial photo of the local rectangle, heading-up (image right = -x, image down = -y), from the landscape's tile textures"""
    nu, nv = int((xmax-xmin)/res), int((ymax-ymin)/res)
    u = (np.arange(nu)+0.5)/nu; v = (np.arange(nv)+0.5)/nv
    x = xmax - u[None, :]*(xmax-xmin) + 0*v[:, None]; y = ymax - v[:, None]*(ymax-ymin) + 0*u[None, :]
    W, N = local_to_world(W0, N0, hd, x, y)
    img = np.zeros((nv, nu, 3), np.uint8); cache = {}
    tx = np.floor(W/TILE).astype(int); ty = np.floor(N/TILE).astype(int)
    for key in set(zip(tx.ravel().tolist(), ty.ravel().tolist())):
        m = (tx == key[0]) & (ty == key[1])
        if key not in cache:
            f = [p for p in (f'{ld}/Textures/t{key[0]:03d}{key[1]:03d}.dds', f'{ld}/Textures/t{key[0]:02d}{key[1]:02d}.dds') if os.path.exists(p)]
            cache[key] = decode_dxt(f[0]) if f else None
        T = cache[key]
        if T is None: img[m] = (90, 140, 60); continue
        S = T.shape[0]
        col = np.clip(((1-(W[m]-key[0]*TILE)/TILE)*S).astype(int), 0, S-1)      # col 0 = west edge (x max)
        row = np.clip(((1-(N[m]-key[1]*TILE)/TILE)*S).astype(int), 0, S-1)      # row 0 = north edge (y max)
        img[m] = T[row, col]
    if runway: paint_runway(img, x, y, *runway)
    return img

def paint_runway(img, x, y, length, width, asphalt=True):
    """draw an asphalt (or grass) runway with markings straight into the photo; x,y = local coordinates of every pixel"""
    L, Wd = length/2, width/2
    on = (np.abs(x) <= Wd) & (np.abs(y) <= L)
    if asphalt:
        rng = np.random.default_rng(1); noise = rng.normal(0, 3, img.shape[:2])[..., None]
        img[on] = np.clip(np.array([92, 92, 96]) + noise[on], 0, 255).astype(np.uint8)
    white = np.array([235, 235, 230], np.uint8)
    edge = on & (np.abs(x) >= Wd-1.2)                                                       # edge lines
    centre = on & (np.abs(x) <= 0.45) & (np.abs(y) <= L-25) & (((y+L) % 36) < 22)           # dashed centre line
    bars = np.zeros_like(on)
    for k in range(-4, 5):                                                                   # threshold stripes at both ends
        if k == 0: continue
        bars |= on & (np.abs(x - k*Wd/5.0) <= 1.0) & (np.abs(y) >= L-30) & (np.abs(y) <= L-10)
    img[edge | centre | bars] = white



def grid_mesh(x0, x1, y0, y1, step, zfun, uvfun, zoff=0.0):
    nx = max(2, int(math.ceil((x1-x0)/step))+1); ny = max(2, int(math.ceil((y1-y0)/step))+1)
    xs = np.linspace(x0, x1, nx); ys = np.linspace(y0, y1, ny)
    X, Y = np.meshgrid(xs, ys, indexing='ij'); Z = zfun(X, Y) + zoff
    U, Vv = uvfun(X, Y)
    dzx = np.gradient(Z, xs, axis=0); dzy = np.gradient(Z, ys, axis=1)
    n = np.stack([-dzx, -dzy, np.ones_like(Z)], -1); n /= np.linalg.norm(n, axis=-1, keepdims=True)
    V = np.stack([X, Y, Z, n[..., 0], n[..., 1], n[..., 2], U, Vv], -1).reshape(-1, 8).astype(np.float32)
    idx = []
    for i in range(nx-1):
        for j in range(ny-1):
            a = i*ny+j; b = (i+1)*ny+j; c = (i+1)*ny+j+1; d = i*ny+j+1
            idx += [a, b, c, a, c, d]          # CCW in (x,y) like all stock triangles (clockwise ones are culled -> invisible ground)
    return V, np.array(idx, np.uint32)

def box(cx, cy, z0, w, d, h):
    x0, x1, y0, y1 = cx-w/2, cx+w/2, cy-d/2, cy+d/2; z1 = z0+h
    faces = [((0, 0, 1), [(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]), ((0, 0, -1), [(x0, y1, z0), (x1, y1, z0), (x1, y0, z0), (x0, y0, z0)]),
             ((0, -1, 0), [(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)]), ((0, 1, 0), [(x1, y1, z0), (x0, y1, z0), (x0, y1, z1), (x1, y1, z1)]),
             ((1, 0, 0), [(x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)]), ((-1, 0, 0), [(x0, y1, z0), (x0, y0, z0), (x0, y0, z1), (x0, y1, z1)])]
    V = []; I = []
    for nrm, pts in faces:
        b = len(V)
        for k, p in enumerate(pts): V.append([*p, *nrm, k in (1, 2), k in (2, 3)])
        I += [b, b+1, b+2, b, b+2, b+3]
    return np.array(V, np.float32), np.array(I, np.uint32)

def main(ld, name, lat, lon, hd, length=900, width=50, append=False):
    ld = ld.rstrip('/')
    ter = Terrain(ld)
    W0, N0 = ter.world(lat, lon)
    zg = lambda x, y: ter.height(*local_to_world(W0, N0, hd, x, y))
    h0 = float(zg(np.array(0.0), np.array(0.0))); elev = float(round(h0))
    print(f'airport world pos W={W0:.1f} N={N0:.1f}  terrain height {h0:.2f} -> elev {elev}')
    zl = lambda x, y: zg(x, y) - elev
    ax0, ax1, ay0, ay1 = -width/2-300, width/2+300, -length/2-150, length/2+150
    os.makedirs(f'{ld}/Airports/Textures/{name}', exist_ok=True)
    write_bgra_dds(f'{ld}/Airports/Textures/{name}/{name}_Grass.dds', photo(ld, W0, N0, hd, ax0, ax1, ay0, ay1, runway=(length, width)))
    stock = os.path.expanduser('~/mnt/Condor3/Landscapes/Slovenia3/Airports/Textures/Aigen/AigenMuck.dds')
    if os.path.exists(stock): shutil.copy(stock, f'{ld}/Airports/Textures/{name}/{name}_Muck.dds')
    uv_g = lambda x, y: ((ax1-x)/(ax1-ax0), (ay1-y)/(ay1-ay0))
    vg, ig = grid_mesh(ax0, ax1, ay0, ay1, 30.0, zl, uv_g, zoff=0.25)   # mesh must lie ABOVE the terrain surface (stock airports flatten the terrain slightly below the mesh)
    g = c3d.build([dict(name='Grass3D', texture=f'Textures\\{name}\\{name}_Grass.dds', color=(1, 1, 1, 1), params=(0, 0, 0), vertices=vg, indices=ig)])
    open(f'{ld}/Airports/{name}G.c3d', 'wb').write(c3d.write(g))
    objs = []
    for nm, cx, cy, col in (('FrontLeftRed', width/2+12, length/2, (1, 0, 0, 1)), ('FrontRightGreen', -width/2-12, length/2, (0, 0.8, 0, 1)),
                            ('BackLeftBlue', width/2+12, -length/2, (0, 0, 1, 1)), ('BackRightYellow', -width/2-12, -length/2, (1, 1, 0, 1))):
        v, i = box(cx, cy, float(zl(np.array(cx), np.array(cy))), 8, 8, 12)
        objs.append(dict(name=nm, texture='', color=col, params=(0.5, 2.5, 0), vertices=v, indices=i))
    open(f'{ld}/Airports/{name}O.c3d', 'wb').write(c3d.write(c3d.build(objs)))
    rec = apt.pack_one(name, lat, lon, elev, hd, int(length), int(width), asphalt=True, freq=123.5)
    p = f'{ld}/{os.path.basename(ld)}.apt'
    old = open(p, 'rb').read() if append and os.path.exists(p) else b''
    open(p, 'wb').write(old + rec)
    print('wrote', name, 'G/O, textures and', os.path.basename(p), f'({len(vg)} ground verts)')

if __name__ == '__main__':
    a = [x for x in sys.argv[1:] if not x.startswith('--')]
    main(a[0], a[1], float(a[2]), float(a[3]), float(a[4]), *(float(x) for x in a[5:7]), append='--append' in sys.argv)
