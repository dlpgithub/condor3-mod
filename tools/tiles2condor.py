#!/usr/bin/env python3
"""tiles2condor.py - build a Condor 3 landscape from a 3D Tiles export (one GLB per tile, e.g. from the "earth-rea" tile library).

usage:  tiles2condor.py <export_dir> <config.json> <Landscapes_dir>/<Name> [--work DIR] [--budget SECONDS] [--only STAGE]

  export_dir   folder with tileset.json and tiles/*.glb (3D Tiles 1.1, positions = WGS84 ECEF via tileset transforms, glTF Y-up)
  config.json  landscape options and the airports (see docs/TILES2CONDOR.md)
  output       the landscape folder to create (its name is the landscape name)

The work is done in resumable stages (each stage skips what already exists in the work folder):
  plan      projection (UTM zone of the data), landscape grid, list of leaf tiles
  raster    top-down render of the meshes into colour + height blocks (orthophoto and surface model)
  terrain   ground model from the surface model, airports smoothed -> .trn, .tr3, .tr3f, .tha
  textures  tile textures (DXT1 with mips), flight-planner map
  base      ini, forest maps, thermal map, hashes, empty object/airspace lists, waypoints
  airports  .apt, airport ground meshes cut exactly from the terrain, aerial sub-textures, windsocks
  objects   everything standing above the ground (buildings, trees) as textured scenery meshes
--budget makes a run stop cleanly after that many seconds (rerun to continue); default: no limit.
Needs python3 with numpy and Pillow. Formats: docs/TERRAIN_FORMAT.md.
"""
import sys, os, json, struct, math, time, io, hashlib
import numpy as np
from PIL import Image
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c3d, apt
from terrain_hash import condor_hash
from make_tm3 import make_tm3

TILE = 5760.0
T0 = time.time(); BUDGET = [1e18]
def out_of_time(): return time.time() - T0 > BUDGET[0]
def log(*a): print(*a, flush=True)

# ------------------------------------------------------------------ geodesy (vectorised) ------------------------------------------------
WGS_A = 6378137.0; WGS_F = 1/298.257223563; WGS_E2 = WGS_F*(2-WGS_F); K0 = 0.9996
_n = WGS_F/(2-WGS_F); _A = WGS_A/(1+_n)*(1+_n**2/4+_n**4/64)
_al = [_n/2-2*_n**2/3+5*_n**3/16, 13*_n**2/48-3*_n**3/5, 61*_n**3/240]
_be = [_n/2-2*_n**2/3+37*_n**3/96, _n**2/48+_n**3/15, 17*_n**3/480]
_e = math.sqrt(WGS_E2)

def ecef_to_lla(p):
    x, y, z = p[..., 0], p[..., 1], p[..., 2]
    lon = np.arctan2(y, x); r = np.hypot(x, y); lat = np.arctan2(z, r*(1-WGS_E2))
    for _ in range(5):
        N = WGS_A/np.sqrt(1-WGS_E2*np.sin(lat)**2); h = r/np.cos(lat)-N; lat = np.arctan2(z, r*(1-WGS_E2*N/(N+h)))
    return np.degrees(lat), np.degrees(lon), h

def utm_forward(lat, lon, zone, south=False):
    lam = np.radians(np.asarray(lon, float)-((zone-1)*6-180+3)); phi = np.radians(np.asarray(lat, float))
    t = np.sinh(np.arctanh(np.sin(phi))-_e*np.arctanh(_e*np.sin(phi)))
    xi = np.arctan2(t, np.cos(lam)); eta = np.arctanh(np.sin(lam)/np.sqrt(1+t*t))
    E = K0*_A*(eta+sum(a*np.cos(2*(j+1)*xi)*np.sinh(2*(j+1)*eta) for j, a in enumerate(_al)))
    N = K0*_A*(xi+sum(a*np.sin(2*(j+1)*xi)*np.cosh(2*(j+1)*eta) for j, a in enumerate(_al)))
    return 500000+E, N+(1e7 if south else 0.0)

def utm_inverse(E, N, zone, south=False):
    xi = (N-(1e7 if south else 0.0))/(K0*_A); eta = (E-500000)/(K0*_A)
    xp = xi-sum(b*math.sin(2*(j+1)*xi)*math.cosh(2*(j+1)*eta) for j, b in enumerate(_be))
    ep = eta-sum(b*math.cos(2*(j+1)*xi)*math.sinh(2*(j+1)*eta) for j, b in enumerate(_be))
    chi = math.asin(math.sin(xp)/math.cosh(ep)); phi = chi
    for _ in range(6): phi = math.asin(math.tanh(math.atanh(math.sin(chi))+_e*math.atanh(_e*math.sin(phi))))
    return math.degrees(phi), math.degrees(math.atan2(math.sinh(ep), math.cos(xp)))+((zone-1)*6-180+3)

# ------------------------------------------------------------------ 3D Tiles input -----------------------------------------------------
def read_tileset(export):
    """leaf contents of the tileset: [(glb path, centre ECEF)]. A node with children is replaced by them (refine REPLACE)."""
    t = json.load(open(os.path.join(export, 'tileset.json'))); leaves = []
    def mat(n): return np.array(n['transform'], float).reshape(4, 4).T if 'transform' in n else np.eye(4)
    def walk(n, M, top):
        M = M @ mat(n); kids = n.get('children', [])
        if 'content' in n and (top or not kids or n.get('refine', 'REPLACE') == 'ADD'):
            leaves.append((os.path.join(export, n['content']['uri']), M))
        for c in kids: walk(c, M, False)
    walk(t['root'], np.eye(4), True)
    return leaves

def load_glb(path, M):
    """-> list of primitives (ecef (n,3) float64, uv (n,2), triangles (m,3), PIL image). glTF is Y-up: ecef = M * (x, -z, y)."""
    d = open(path, 'rb').read()
    if len(d) < 20 or d[:4] != b'glTF': return []
    jl = struct.unpack_from('<I', d, 12)[0]; j = json.loads(d[20:20+jl]); bo = 20+jl+8
    def acc(i):
        a = j['accessors'][i]; bv = j['bufferViews'][a['bufferView']]
        n = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3}[a['type']]; dt = {5126: '<f4', 5123: '<u2', 5125: '<u4', 5121: 'u1'}[a['componentType']]
        return np.frombuffer(d, dt, a['count']*n, bo+bv.get('byteOffset', 0)+a.get('byteOffset', 0)).reshape(-1, n)
    out = []
    for mesh in j.get('meshes', []):
        for pr in mesh['primitives']:
            if pr.get('mode', 4) != 4 or 'indices' not in pr: continue
            pos = acc(pr['attributes']['POSITION']).astype(np.float64)
            uv = acc(pr['attributes']['TEXCOORD_0']).astype(np.float32) if 'TEXCOORD_0' in pr['attributes'] else np.zeros((len(pos), 2), np.float32)
            tri = acc(pr['indices']).astype(np.int64).reshape(-1, 3)
            im = None
            try:
                tex = j['materials'][pr['material']]['pbrMetallicRoughness']['baseColorTexture']['index']
                bv = j['bufferViews'][j['images'][j['textures'][tex]['source']]['bufferView']]
                im = Image.open(io.BytesIO(d[bo+bv.get('byteOffset', 0):bo+bv.get('byteOffset', 0)+bv['byteLength']])).convert('RGB')
            except Exception: pass
            loc = np.c_[pos[:, 0], -pos[:, 2], pos[:, 1], np.ones(len(pos))]
            out.append(((loc @ M.T)[:, :3], uv, tri, im))
    return out

class Plan:
    """landscape grid. World frame of the game: x = E0 - easting (west), y = northing - N0 (north)."""
    def __init__(s, d): s.__dict__.update(d)
    def world(s, lat, lon):
        E, N = utm_forward(lat, lon, s.zone, s.south); return s.E0-E, N-s.N0
    def latlon(s, x, y): return utm_inverse(s.E0-x, s.N0+y, s.zone, s.south)

def stage_plan(export, cfg, work):
    pp = f'{work}/plan.json'
    if os.path.exists(pp): return Plan(json.load(open(pp)))
    leaves = read_tileset(export)
    loc = json.load(open(os.path.join(export, 'location.json'))) if os.path.exists(os.path.join(export, 'location.json')) else None
    bounds = {t['path']: t for t in loc['tiles']} if loc else {}
    cen = np.array([M[:3, 3] for _, M in leaves]); lat, lon, _ = ecef_to_lla(cen)
    zone = int(cfg.get('utm_zone') or (math.floor((float(np.median(lon))+180)/6)+1)); south = bool(float(np.median(lat)) < 0)
    tiles = []
    for (p, M), la, lo in zip(leaves, lat, lon):
        b = bounds.get(os.path.basename(p)[:-4])
        if b: cl = [(b['south'], b['west']), (b['south'], b['east']), (b['north'], b['west']), (b['north'], b['east'])]
        else:                                                   # no location.json: read the mesh once for its extent
            ll = [ecef_to_lla(e)[:2] for e, _, _, _ in load_glb(p, M)]
            if not ll: continue
            la_ = np.concatenate([a for a, _ in ll]); lo_ = np.concatenate([o for _, o in ll]); cl = [(la_.min(), lo_.min()), (la_.min(), lo_.max()), (la_.max(), lo_.min()), (la_.max(), lo_.max())]
        E, N = utm_forward(np.array([c[0] for c in cl]), np.array([c[1] for c in cl]), zone, south)
        tiles.append(dict(glb=p, M=M.tolist(), E0=float(E.min()), E1=float(E.max()), N0=float(N.min()), N1=float(N.max())))
    Emin = min(t['E0'] for t in tiles); Emax = max(t['E1'] for t in tiles); Nmin = min(t['N0'] for t in tiles); Nmax = max(t['N1'] for t in tiles)
    m = int(cfg.get('margin_tiles', 1))
    nx = max(1, math.ceil((Emax-Emin)/TILE))+2*m; ny = max(1, math.ceil((Nmax-Nmin)/TILE))+2*m
    E0 = float(round((Emin+Emax)/2 + nx*TILE/2)); N0 = float(round((Nmin+Nmax)/2 - ny*TILE/2))      # east edge easting, south edge northing
    plan = dict(zone=zone, south=south, nx=nx, ny=ny, E0=E0, N0=N0, res=TILE/int(cfg.get('texture_size', 8192)), tex=int(cfg.get('texture_size', 8192)),
                data=dict(E0=Emin, E1=Emax, N0=Nmin, N1=Nmax), height_offset=float(cfg.get('height_offset', 0.0)), tiles=tiles)
    json.dump(plan, open(pp, 'w'))
    log(f'plan: UTM zone {zone}{"S" if south else "N"}, landscape {nx} x {ny} tiles ({nx*TILE/1000:.1f} x {ny*TILE/1000:.1f} km), data {(Emax-Emin)/1000:.1f} x {(Nmax-Nmin)/1000:.1f} km, {len(tiles)} source tiles')
    return Plan(plan)

# ------------------------------------------------------------------ rasteriser ----------------------------------------------------------
BLOCK = 2048          # raster block edge in pixels

def raster_tris(px, py, z, uv, tri, tex, W, H, zbuf, rgb):
    """top-down z-buffer render of textured triangles into zbuf (H,W) float32 (highest wins) and rgb (H,W,3)"""
    X = px[tri]; Y = py[tri]; Z = z[tri]                                    # (n,3)
    x0 = np.floor(X.min(1)).astype(np.int64); x1 = np.ceil(X.max(1)).astype(np.int64)
    y0 = np.floor(Y.min(1)).astype(np.int64); y1 = np.ceil(Y.max(1)).astype(np.int64)
    den = (Y[:, 1]-Y[:, 2])*(X[:, 0]-X[:, 2]) + (X[:, 2]-X[:, 1])*(Y[:, 0]-Y[:, 2])
    keep = (np.abs(den) > 1e-9) & (x1 >= 0) & (y1 >= 0) & (x0 < W) & (y0 < H)
    size = np.maximum(x1-x0, y1-y0)+1
    th, tw = tex.shape[:2]; U = uv[tri]
    for K in (4, 8, 16, 32, 64, 128, 256, 512, 1024):
        sel = np.nonzero(keep & (size <= K) & (size > (K//2 if K > 4 else 0)))[0]
        if not len(sel): continue
        o = np.arange(K, dtype=np.float32)+0.5
        step = max(1, int(3e6//(K*K)))
        for s in range(0, len(sel), step):
            q = sel[s:s+step]
            gx = x0[q, None, None] + o[None, None, :]; gy = y0[q, None, None] + o[None, :, None]              # pixel centres (n,K,K)
            xa, xb, xc = X[q, 0, None, None], X[q, 1, None, None], X[q, 2, None, None]
            ya, yb, yc = Y[q, 0, None, None], Y[q, 1, None, None], Y[q, 2, None, None]
            d = den[q, None, None]
            l0 = ((yb-yc)*(gx-xc) + (xc-xb)*(gy-yc))/d; l1 = ((yc-ya)*(gx-xc) + (xa-xc)*(gy-yc))/d; l2 = 1-l0-l1
            ins = (l0 >= -1e-4) & (l1 >= -1e-4) & (l2 >= -1e-4) & (gx >= 0) & (gx < W) & (gy >= 0) & (gy < H)
            if not ins.any(): continue
            zz = (l0*Z[q, 0, None, None] + l1*Z[q, 1, None, None] + l2*Z[q, 2, None, None])[ins]
            uu = (l0*U[q, 0, 0, None, None] + l1*U[q, 1, 0, None, None] + l2*U[q, 2, 0, None, None])[ins]
            vv = (l0*U[q, 0, 1, None, None] + l1*U[q, 1, 1, None, None] + l2*U[q, 2, 1, None, None])[ins]
            pix = (gy[np.broadcast_to(ins, ins.shape)] if False else np.broadcast_to(gy, ins.shape)[ins]).astype(np.int64)*W + np.broadcast_to(gx, ins.shape)[ins].astype(np.int64)
            col = tex[np.clip((vv*th).astype(np.int64), 0, th-1), np.clip((uu*tw).astype(np.int64), 0, tw-1)]
            order = np.argsort(zz, kind='stable'); pix = pix[order]; zz = zz[order].astype(np.float32); col = col[order]
            win = zz > zbuf.ravel()[pix]
            zbuf.ravel()[pix[win]] = zz[win]; rgb.reshape(-1, 3)[pix[win]] = col[win]

def block_path(work, bx, by): return f'{work}/ras_{bx:03d}_{by:03d}.npz'

def stage_raster(plan, work):
    """blocks are indexed from the WEST edge (bx) and the NORTH edge (by) of the landscape, BLOCK pixels each at plan.res m/px"""
    res = plan.res; Wwest = plan.E0 - plan.nx*TILE; Nnorth = plan.N0 + plan.ny*TILE; bs = BLOCK*res
    nbx = int(round(plan.nx*TILE/bs)); nby = int(round(plan.ny*TILE/bs)); done = True
    for bx in range(nbx):
        for by in range(nby):
            if os.path.exists(block_path(work, bx, by)): continue
            e0 = Wwest + bx*bs; n1 = Nnorth - by*bs
            src = [t for t in plan.tiles if t['E1'] > e0-2 and t['E0'] < e0+bs+2 and t['N1'] > n1-bs-2 and t['N0'] < n1+2]
            if not src: continue
            if out_of_time(): return False
            zbuf = np.full((BLOCK, BLOCK), -1e9, np.float32); rgb = np.zeros((BLOCK, BLOCK, 3), np.uint8)
            for t in src:
                for ecef, uv, tri, im in load_glb(t['glb'], np.array(t['M'])):
                    lat, lon, h = ecef_to_lla(ecef); E, N = utm_forward(lat, lon, plan.zone, plan.south)
                    tex = np.asarray(im) if im is not None else np.full((2, 2, 3), 128, np.uint8)
                    raster_tris((E-e0)/res, (n1-N)/res, h+plan.height_offset, uv, tri, tex, BLOCK, BLOCK, zbuf, rgb)
            valid = zbuf > -1e8
            np.savez(block_path(work, bx, by), rgb=rgb, z=np.where(valid, np.clip(np.rint((zbuf+500)*10), 1, 65535), 0).astype(np.uint16))
            log(f'raster block {bx},{by}: {len(src)} source tiles, coverage {valid.mean()*100:.0f}%  ({time.time()-T0:.0f}s)')
    return done

def load_block(work, bx, by):
    p = block_path(work, bx, by)
    if not os.path.exists(p): return None, None
    d = np.load(p); return d['rgb'], d['z']

# ------------------------------------------------------------------ small raster helpers ------------------------------------------------
G = 5.625                                    # working grid for terrain and colour statistics [m] (1024 cells per Condor tile)
def smooth01(t): t = np.clip(t, 0, 1); return t*t*(3-2*t)

def box_blur(a, r, w=None):
    """normalised box blur (radius r cells) of a 2-D array; w = weights/validity (0..1)"""
    w = np.ones(a.shape, np.float32) if w is None else w.astype(np.float32)
    def run(x):
        for ax in (0, 1):
            c = np.cumsum(np.pad(x, [(r+1, r) if k == ax else (0, 0) for k in range(2)], mode='constant'), axis=ax, dtype=np.float64)
            n = x.shape[ax]; sl0 = [slice(None)]*2; sl1 = [slice(None)]*2; sl0[ax] = slice(0, n); sl1[ax] = slice(2*r+1, 2*r+1+n)
            x = (c[tuple(sl1)]-c[tuple(sl0)]).astype(np.float32)
        return x
    num = run(np.where(w > 0, a, 0).astype(np.float32)*w); den = run(w)
    return np.where(den > 1e-6, num/np.maximum(den, 1e-6), 0).astype(np.float32), den

def slide(a, k, fn):
    """separable sliding min/max with window 2k+1 (edges padded with the edge value)"""
    from numpy.lib.stride_tricks import sliding_window_view
    for ax in (0, 1):
        p = np.pad(a, [(k, k) if i == ax else (0, 0) for i in range(2)], mode='edge')
        a = fn(sliding_window_view(p, 2*k+1, axis=ax), axis=-1)
    return a

def pushpull(a, valid):
    """fill invalid cells from coarser and coarser averages; a (H,W) or (H,W,C)"""
    a = a.astype(np.float32); v = valid.astype(np.float32)
    if v.all() or a.shape[0] < 2 or a.shape[1] < 2:
        if not v.any(): return a
        m = (a*(v[..., None] if a.ndim == 3 else v)).sum((0, 1))/max(v.sum(), 1)
        return np.where((v[..., None] if a.ndim == 3 else v) > 0, a, m)
    H, W = v.shape; ph, pw = H % 2, W % 2
    ap = np.pad(a, [(0, ph), (0, pw)]+[(0, 0)]*(a.ndim-2)); vp = np.pad(v, [(0, ph), (0, pw)])
    vv = vp[..., None] if a.ndim == 3 else vp
    sa = (ap*vv).reshape(ap.shape[0]//2, 2, ap.shape[1]//2, 2, *a.shape[2:]).sum((1, 3)); sv = vp.reshape(vp.shape[0]//2, 2, vp.shape[1]//2, 2).sum((1, 3))
    low = pushpull(sa/np.maximum(sv[..., None] if a.ndim == 3 else sv, 1e-6), sv > 0)
    up = np.repeat(np.repeat(low, 2, 0), 2, 1)[:H, :W]
    return np.where((v[..., None] if a.ndim == 3 else v) > 0, a, up)

def grid_stats(plan, work):
    """per G-cell statistics of the raster blocks, image orientation (row 0 = north edge, column 0 = west edge):
    zmin (lowest surface height in the cell, nan = no data), rgb mean"""
    p = f'{work}/grid.npz'
    if os.path.exists(p):
        d = np.load(p); return d['zmin'], d['rgb']
    f = int(round(G/plan.res)); cb = BLOCK//f; W = plan.nx*1024; H = plan.ny*1024
    zmin = np.full((H, W), np.nan, np.float32); rgb = np.zeros((H, W, 3), np.float32)
    for by in range(H//cb):
        for bx in range(W//cb):
            c, z = load_block(work, bx, by)
            if c is None: continue
            zz = np.where(z > 0, z.astype(np.float32)/10-500, np.inf).reshape(cb, f, cb, f); v = (z > 0).reshape(cb, f, cb, f)
            m = zz.min((1, 3)); n = v.sum((1, 3))
            zmin[by*cb:(by+1)*cb, bx*cb:(bx+1)*cb] = np.where(n > 0, m, np.nan)
            rgb[by*cb:(by+1)*cb, bx*cb:(bx+1)*cb] = (c.reshape(cb, f, cb, f, 3)*v[..., None]).sum((1, 3))/np.maximum(n, 1)[..., None]
    np.savez(p, zmin=zmin, rgb=rgb)
    return zmin, rgb

# ------------------------------------------------------------------ airports from the config ---------------------------------------------
class Airfield:
    """one Condor airport = one runway. Local frame: +Y = runway heading, +X = left, origin = runway centre (docs/TERRAIN_FORMAT.md 2.4)"""
    def __init__(s, d, plan):
        s.name = d['name']; r = d['runway']
        xa, ya = plan.world(*r['from']); xb, yb = plan.world(*r['to'])
        s.x0, s.y0 = float((xa+xb)/2), float((ya+yb)/2); s.len = float(math.hypot(xb-xa, yb-ya)); s.wid = float(r.get('width', 30))
        east, north = -(xb-xa), (yb-ya)                              # world x counts west
        s.heading = math.degrees(math.atan2(east, north)) % 360     # grid heading in the landscape's UTM zone (what the game uses)
        s.c, s.s = math.cos(math.radians(s.heading)), math.sin(math.radians(s.heading))
        s.asphalt = r.get('surface', 'asphalt').lower() not in ('grass', 'gras', 'turf')
        s.freq = float(d.get('frequency', 123.5)); g = d.get('ground', {})
        s.hx = s.wid/2 + float(g.get('side', 100)); s.hy = s.len/2 + float(g.get('ends', 150))
        s.grass = d.get('grass', 'auto'); s.lat, s.lon = plan.latlon(s.x0, s.y0)
        s.flags = (bool(d.get('primary_reversed', False)), bool(d.get('tow_primary_left', False)), bool(d.get('tow_secondary_left', False)))
    def to_local(s, X, Y):
        dW = X-s.x0; dN = Y-s.y0; return dW*s.c + dN*s.s, -dW*s.s + dN*s.c
    def to_world(s, x, y): return s.x0 + x*s.c - y*s.s, s.y0 + x*s.s + y*s.c
    def outside(s, X, Y, pad=0.0):
        xl, yl = s.to_local(X, Y); return np.hypot(np.maximum(np.abs(xl)-s.hx-pad, 0), np.maximum(np.abs(yl)-s.hy-pad, 0))

def airfields(cfg, plan): return [Airfield(d, plan) for d in cfg.get('airports', [])]

# ------------------------------------------------------------------ terrain ----------------------------------------------------------------
def stage_terrain(plan, cfg, work, out, name):
    """ground model -> terrain.npy (G grid, image orientation), then .trn / .tr3 / .tr3f / .tha"""
    tp = f'{work}/terrain.npy'
    if not os.path.exists(tp):
        zmin, _ = grid_stats(plan, work); valid = np.isfinite(zmin)
        if not valid.any(): raise SystemExit('no raster data: run the raster stage first')
        k = max(1, int(round(float(cfg.get('ground_filter_m', 35))/G)))
        z = np.where(valid, zmin, np.float32(1e9))
        z = slide(slide(z, k, np.min), k, np.max)                                # opening: removes buildings/trees narrower than 2k cells
        z = np.where(valid, np.minimum(z, np.where(valid, zmin, 1e9)), 0).astype(np.float32)
        z, _ = box_blur(z, 2, valid); z, _ = box_blur(z, 2, valid)
        H, W = z.shape
        cy = (plan.ny*TILE - (np.arange(H, dtype=np.float32)+0.5)*G)[:, None]; cx = (plan.nx*TILE - (np.arange(W, dtype=np.float32)+0.5)*G)[None, :]
        for af in airfields(cfg, plan):                                          # airports: extra smooth ground (no bumps under the wheels)
            d = af.outside(cx, cy); m = d < 150
            if not m.any(): continue
            r0, r1 = np.nonzero(m.any(1))[0][[0, -1]]; c0, c1 = np.nonzero(m.any(0))[0][[0, -1]]
            sub = z[r0:r1+1, c0:c1+1]; sv = valid[r0:r1+1, c0:c1+1]
            sm, _ = box_blur(sub, 5, sv); sm, _ = box_blur(sm, 5, sv)
            w = (1-smooth01(d[r0:r1+1, c0:c1+1]/90.0))*sv
            z[r0:r1+1, c0:c1+1] = sub*(1-w) + sm*w
        zf = pushpull(z, valid)
        for _ in range(4):                                                       # soften the filler, keep the data
            b, _ = box_blur(zf, 12); zf = np.where(valid, zf, b)
        zf = np.maximum(zf, 1.0)
        np.save(tp, zf.astype(np.float32)); np.save(f'{work}/valid.npy', valid)
        log(f'terrain: ground {np.nanmin(zf[valid]):.0f}..{np.nanmax(zf[valid]):.0f} m, data covers {valid.mean()*100:.0f}% of the landscape')
    Z = np.load(tp); H, W = Z.shape
    def sample(x, y):                                                            # bilinear in the G grid at world (x west, y north)
        fc = np.clip((plan.nx*TILE - x)/G - 0.5, 0, W-1.001); fr = np.clip((plan.ny*TILE - y)/G - 0.5, 0, H-1.001)
        c = fc.astype(np.int64); r = fr.astype(np.int64); u = fc-c; v = fr-r
        return Z[r, c]*(1-u)*(1-v) + Z[r, c+1]*u*(1-v) + Z[r+1, c]*(1-u)*v + Z[r+1, c+1]*u*v
    os.makedirs(f'{out}/HeightMaps/22.5m', exist_ok=True)
    tha = []; trn = np.zeros((plan.nx*64, plan.ny*64), '<u2')
    for tx in range(plan.nx):
        for ty in range(plan.ny):
            p3 = (np.arange(193)*30.0); x = tx*TILE + p3[:, None]; y = ty*TILE + p3[None, :]
            t3 = np.rint(sample(np.broadcast_to(x, (193, 193)), np.broadcast_to(y, (193, 193)))).astype('<u2')        # [x][y], y fastest
            open(f'{out}/HeightMaps/h{tx:02d}{ty:02d}.tr3', 'wb').write(t3.tobytes())
            trn[tx*64:(tx+1)*64, ty*64:(ty+1)*64] = t3[:192:3, :192:3]
            pf = (np.arange(257)*22.5); x = tx*TILE + pf[:, None]; y = ty*TILE + pf[None, :]
            d = sample(np.broadcast_to(x, (257, 257)), np.broadcast_to(y, (257, 257))).astype('<f4').tobytes()         # float posts: rendered terrain
            open(f'{out}/HeightMaps/22.5m/h{tx:02d}{ty:02d}.tr3f', 'wb').write(d)
            tha.append(f'{tx*1000+ty:06d} {condor_hash(d, plan.ny*tx+ty)}')
    open(f'{out}/{name}.tha', 'wb').write(('\r\n'.join(tha)+'\r\n').encode())
    open(f'{out}/{name}.trn', 'wb').write(struct.pack('<II5fII', plan.nx*64, plan.ny*64, 90.0, -90.0, 90.0, plan.E0, plan.N0, plan.zone, ord('S' if plan.south else 'N')) + trn.tobytes())
    return trn

class Terrain:
    """the surface the game renders: 22.5 m posts of the .tr3f files, each cell split by the diagonal rule of the stock patches:
    cell (i, j) with i+j even -> diagonal (i,j)-(i+1,j+1), odd -> (i+1,j)-(i,j+1)   (i counts west, j north, global post indices)"""
    def __init__(s, plan, out):
        s.nx, s.ny = plan.nx*256+1, plan.ny*256+1; s.P = np.zeros((s.nx, s.ny), np.float32)
        for tx in range(plan.nx):
            for ty in range(plan.ny):
                s.P[tx*256:tx*256+257, ty*256:ty*256+257] = np.fromfile(f'{out}/HeightMaps/22.5m/h{tx:02d}{ty:02d}.tr3f', '<f4').reshape(257, 257)
    def height(s, x, y):
        fx = np.clip(np.asarray(x, float)/22.5, 0, s.nx-1.000001); fy = np.clip(np.asarray(y, float)/22.5, 0, s.ny-1.000001)
        i = fx.astype(np.int64); j = fy.astype(np.int64); u = fx-i; v = fy-j; P = s.P
        a, b, c, d = P[i, j], P[i+1, j], P[i+1, j+1], P[i, j+1]
        even = ((i+j) % 2) == 0
        he = np.where(u >= v, a + u*(b-a) + v*(c-b), a + v*(d-a) + u*(c-d))             # diagonal a-c
        ho = np.where(u+v <= 1, a + u*(b-a) + v*(d-a), c + (1-u)*(d-c) + (1-v)*(b-c))   # diagonal b-d
        return np.where(even, he, ho)

# ------------------------------------------------------------------ textures and map ------------------------------------------------------
def stage_textures(plan, work, out, name):
    import dds
    os.makedirs(f'{out}/Textures', exist_ok=True)
    zmin, rgbg = grid_stats(plan, work); valid = np.isfinite(zmin)
    fp = f'{work}/fillrgb.npy'
    if not os.path.exists(fp):
        f = pushpull(rgbg, valid)
        for _ in range(3):
            for ch in range(3):
                b, _ = box_blur(f[..., ch], 16); f[..., ch] = np.where(valid, f[..., ch], b)
        np.save(fp, f.astype(np.float32))
    fill = np.load(fp); bpt = plan.tex//BLOCK; rng = np.random.default_rng(7)
    for tx in range(plan.nx):
        for ty in range(plan.ny):
            tp = f'{out}/Textures/t{tx:02d}{ty:02d}.dds'
            if os.path.exists(tp): continue
            if out_of_time(): return False
            cw = plan.nx-1-tx; rn = plan.ny-1-ty                                 # tile position counted from the west / north edge
            blocks = {(i, j): load_block(work, cw*bpt+i, rn*bpt+j) for i in range(bpt) for j in range(bpt)}
            fg = fill[rn*1024:(rn+1)*1024, cw*1024:(cw+1)*1024]
            if all(b[0] is None for b in blocks.values()):                        # filler tile: small texture from the extended colours
                img = np.clip(fg.reshape(512, 2, 512, 2, 3).mean((1, 3)) + rng.normal(0, 4, (512, 512, 1)), 0, 255).astype(np.uint8)
            else:
                S = plan.tex; img = np.empty((S, S, 3), np.uint8); f = S//1024
                for (i, j), (c, z) in blocks.items():
                    fb = np.repeat(np.repeat(fg[j*BLOCK//f:(j+1)*BLOCK//f, i*BLOCK//f:(i+1)*BLOCK//f], f, 0), f, 1)
                    fb = np.clip(fb + rng.normal(0, 4, (BLOCK, BLOCK, 1)), 0, 255).astype(np.uint8)
                    img[j*BLOCK:(j+1)*BLOCK, i*BLOCK:(i+1)*BLOCK] = fb if c is None else np.where((z > 0)[..., None], c, fb)
            dds.write_dxt(tp+'.tmp', img); os.replace(tp+'.tmp', tp)
            log(f'texture t{tx:02d}{ty:02d} {img.shape[0]} px  ({time.time()-T0:.0f}s)')
    dds.write_dxt(f'{out}/Textures/empty.dds', np.full((256, 256, 3), np.clip(fill.mean((0, 1)), 0, 255).astype(np.uint8), np.uint8), np.full((256, 256), 255, np.uint8))
    # flight-planner map: 90 m per pixel, north up, bottom-up BMP with exactly the .trn dimensions
    m = np.clip(fill.reshape(plan.ny*64, 16, plan.nx*64, 16, 3).mean((1, 3)), 0, 255).astype(np.uint8)[::-1]
    bgra = np.dstack([m[..., 2], m[..., 1], m[..., 0], np.full(m.shape[:2], 255, np.uint8)]); W, H = plan.nx*64, plan.ny*64
    open(f'{out}/{name}.bmp', 'wb').write(struct.pack('<2sIHHIIiiHHIIiiII', b'BM', 54+W*H*4, 0, 0, 54, 40, W, H, 1, 32, 0, W*H*4, 2834, 2834, 0, 0) + np.ascontiguousarray(bgra).tobytes())
    return True

# ------------------------------------------------------------------ base files -------------------------------------------------------------
def stage_base(plan, cfg, work, out, name, trn):
    open(f'{out}/{name}.ini', 'wb').write(b'[General]\r\nVersion=1.02\r\nRealtimeShading=1')
    os.makedirs(f'{out}/ForestMaps', exist_ok=True); zero = bytes(512*512); fha = []
    for tx in range(plan.nx):
        for ty in range(plan.ny):                                                 # no generated trees: the 3D data brings its own
            open(f'{out}/ForestMaps/{tx:02d}{ty:02d}.for', 'wb').write(zero); fha.append(f'{tx*1000+ty:06d} {condor_hash(zero, plan.ny*tx+ty)}')
    open(f'{out}/{name}.fha', 'wb').write(('\r\n'.join(fha)+'\r\n').encode())
    fill = np.load(f'{work}/fillrgb.npy'); c = fill.reshape(plan.ny*64, 16, plan.nx*64, 16, 3).mean((1, 3))
    green = (c[..., 1] > c[..., 0]*1.03) & (c[..., 1] > c[..., 2]*1.05)           # thermal map guess: vegetation 102, built-up / bare 178
    therm_img = np.where(green, 102, 178).astype(np.uint8)                        # image orientation -> [iy][ix] (row 0 south, column 0 east)
    make_tm3(trn.astype(np.uint16), therm_img[::-1, ::-1]).tofile(f'{out}/{name}.tm3')
    for ext, data in (('obj', b''), ('air', b''), ('aha', b'0\r\n')):
        if not os.path.exists(f'{out}/{name}.{ext}') or ext != 'obj': open(f'{out}/{name}.{ext}', 'wb').write(data)
    if not os.path.exists(f'{out}/{name}.oha'):
        open(f'{out}/{name}.oha', 'wb').write(('\r\n'.join(f'{tx*1000+ty:06d}' for tx in range(plan.nx) for ty in range(plan.ny))+'\r\n').encode())

# ------------------------------------------------------------------ airports ----------------------------------------------------------------
def crop_ortho(plan, work, e0, e1, n0, n1, step):
    """orthophoto of the UTM rectangle, every step-th raster pixel; returns rgb (h,w,3), valid (h,w), and the exact bounds used"""
    res = plan.res; Ww = plan.E0 - plan.nx*TILE; Nn = plan.N0 + plan.ny*TILE
    c0 = int(math.floor((e0-Ww)/res)); c1 = int(math.ceil((e1-Ww)/res)); r0 = int(math.floor((Nn-n1)/res)); r1 = int(math.ceil((Nn-n0)/res))
    w = (c1-c0)//step//4*4; h = (r1-r0)//step//4*4; c1 = c0+w*step; r1 = r0+h*step
    rgb = np.zeros((h, w, 3), np.uint8); val = np.zeros((h, w), bool)
    for bx in range(c0//BLOCK, (c1-1)//BLOCK+1):
        for by in range(r0//BLOCK, (r1-1)//BLOCK+1):
            c, z = load_block(work, bx, by)
            if c is None: continue
            cols = np.arange(c0, c1, step); rows = np.arange(r0, r1, step)
            mc = (cols >= bx*BLOCK) & (cols < (bx+1)*BLOCK); mr = (rows >= by*BLOCK) & (rows < (by+1)*BLOCK)
            if not mc.any() or not mr.any(): continue
            sub = c[np.ix_(rows[mr]-by*BLOCK, cols[mc]-bx*BLOCK)]; sz = z[np.ix_(rows[mr]-by*BLOCK, cols[mc]-bx*BLOCK)]
            rgb[np.ix_(np.nonzero(mr)[0], np.nonzero(mc)[0])] = sub; val[np.ix_(np.nonzero(mr)[0], np.nonzero(mc)[0])] = sz > 0
    return rgb, val, (Ww+c0*res, Ww+c1*res, Nn-r1*res, Nn-r0*res)

def box(cx, cy, z0, w, d, h):
    x0, x1, y0, y1 = cx-w/2, cx+w/2, cy-d/2, cy+d/2; z1 = z0+h
    faces = [((0, 0, 1), [(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]), ((0, -1, 0), [(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)]),
             ((0, 1, 0), [(x1, y1, z0), (x0, y1, z0), (x0, y1, z1), (x1, y1, z1)]), ((1, 0, 0), [(x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)]),
             ((-1, 0, 0), [(x0, y1, z0), (x0, y0, z0), (x0, y0, z1), (x0, y1, z1)])]
    V = []; I = []
    for nrm, pts in faces:
        b = len(V)
        for k, p in enumerate(pts): V.append([*p, *nrm, k in (1, 2), k in (2, 3)])
        I += [b, b+1, b+2, b, b+2, b+3]
    return np.array(V, np.float32), np.array(I, np.uint32)

def stage_airports(plan, cfg, work, out, name):
    import dds
    afs = airfields(cfg, plan)
    os.makedirs(f'{out}/Airports/Textures', exist_ok=True)
    ter = Terrain(plan, out); valid = np.load(f'{work}/valid.npy')
    _, rg = grid_stats(plan, work)                                                # greenness per G cell, then "mostly green" over each 22.5 m cell
    g1 = ((rg[..., 1] > rg[..., 0]+6) & (rg[..., 1] > rg[..., 2]+10)).astype(np.float32)
    greens = box_blur(g1, 2)[0] > 0.85
    claimed = np.zeros((ter.nx-1, ter.ny-1), bool); recs = b''; cup = ['name,code,country,lat,lon,elev,style,rwdir,rwlen,freq,desc']; info = []
    for af in afs:
        # cells of the 22.5 m terrain grid whose centre lies in the airport's ground rectangle (and has data, and is not taken by an earlier airport)
        cs = [af.to_world(sx*af.hx, sy*af.hy) for sx in (-1, 1) for sy in (-1, 1)]
        i0 = max(0, int(min(c[0] for c in cs)//22.5)); i1 = min(ter.nx-2, int(max(c[0] for c in cs)//22.5)+1)
        j0 = max(0, int(min(c[1] for c in cs)//22.5)); j1 = min(ter.ny-2, int(max(c[1] for c in cs)//22.5)+1)
        ii, jj = np.meshgrid(np.arange(i0, i1+1), np.arange(j0, j1+1), indexing='ij'); cx = (ii+0.5)*22.5; cy = (jj+0.5)*22.5
        vr = np.clip(((plan.ny*TILE - cy)/G).astype(np.int64), 0, valid.shape[0]-1); vc = np.clip(((plan.nx*TILE - cx)/G).astype(np.int64), 0, valid.shape[1]-1)
        m = (af.outside(cx, cy) == 0) & valid[vr, vc] & ~claimed[ii, jj]
        if not m.any(): log(f'airport {af.name}: outside the data or fully overlapped, skipped'); continue
        # The G file only carries GRASS (Grass3D = photo x grass detail + 3D blades). Paved areas are left to the plain terrain texture:
        # drawn through Grass3D they come out bleached and grainy. A cell counts as grass when most of its orthophoto pixels are green.
        gr = greens[vr, vc] if af.grass in ('auto', True) else np.zeros(m.shape, bool)
        if (m & gr).sum() >= 4: m &= gr
        else:                                                                     # no grass at all: one cell beside the runway keeps the mandatory file valid
            k = np.argmin(np.where(m, np.abs(af.to_local(cx, cy)[0]) + np.abs(af.to_local(cx, cy)[1])*0.01 - af.hx, 1e9)); m = np.zeros(m.shape, bool); m.flat[k] = True
        claimed[ii[m], jj[m]] = True
        h0 = float(ter.height(af.x0, af.y0))
        # sub-texture: north-up orthophoto of the bounding box of those cells (u east, v south), alpha = 3D grass length
        ci, cj = ii[m], jj[m]; xw0, xw1 = ci.min()*22.5, (ci.max()+1)*22.5; yn0, yn1 = cj.min()*22.5, (cj.max()+1)*22.5
        e0, e1, n0, n1 = plan.E0-xw1, plan.E0-xw0, plan.N0+yn0, plan.N0+yn1
        step = 1
        while (e1-e0)*(n1-n0)/(plan.res*step)**2 > 20e6: step *= 2              # keep the photo below ~80 MB
        rgb, val, (e0, e1, n0, n1) = crop_ortho(plan, work, e0, e1, n0, n1, step)
        r, g, b = (rgb[..., k].astype(np.int16) for k in range(3))
        alpha = np.zeros(rgb.shape[:2], np.uint8)
        if af.grass in ('auto', True): alpha[(g > r+6) & (g > b+10)] = 110        # blades only where the photo is green
        td = f'{out}/Airports/Textures/{af.name}'; os.makedirs(td, exist_ok=True)
        dds.write_bgra(f'{td}/{af.name}_Ground.dds', rgb, alpha)
        # ground mesh = the terrain triangles themselves
        pid = {}; V = []; I = []
        def post(i, j):
            k = (i, j)
            if k not in pid:
                x, y = i*22.5, j*22.5; xl, yl = af.to_local(x, y); E = plan.E0-x; N = plan.N0+y
                pid[k] = len(V); V.append([xl, yl, float(ter.P[i, j])-h0, 0, 0, 1, (E-e0)/(e1-e0), (n1-N)/(n1-n0)])
            return pid[k]
        for i, j in zip(ci.tolist(), cj.tolist()):
            a, b_, c, d = post(i, j), post(i+1, j), post(i+1, j+1), post(i, j+1)
            I += [a, b_, c, a, c, d] if (i+j) % 2 == 0 else [a, b_, d, b_, c, d]
        V = np.array(V, np.float32); I = np.array(I, np.uint32).reshape(-1, 3)
        p = V[:, :2][I]; area = (p[:, 1, 0]-p[:, 0, 0])*(p[:, 2, 1]-p[:, 0, 1]) - (p[:, 2, 0]-p[:, 0, 0])*(p[:, 1, 1]-p[:, 0, 1])
        I[area < 0] = I[area < 0][:, ::-1]                                        # counter-clockwise in local (x, y)
        G_ = c3d.build([dict(name='Grass3D', texture=f'Textures\\{af.name}\\{af.name}_Ground.dds', color=(1, 1, 1, 1), params=(0, 0, 0), vertices=V, indices=I.ravel())])
        open(f'{out}/Airports/{af.name}G.c3d', 'wb').write(c3d.write(G_))
        # objects: a windsock beside the runway start
        wx, wy = af.wid/2+35, -af.len/2+320; wz = float(ter.height(*af.to_world(wx, wy)))-h0
        pv, pi = box(wx, wy, wz, 0.25, 0.25, 8.0); r_ = 0.05
        tv = np.array([[wx, wy, wz+8, -1, 0, 0, 0.75, 0.51], [wx-r_*0.866, wy+r_*0.5, wz+8, -1, 0, 0, 0.958, 0.87], [wx-r_*0.866, wy-r_*0.5, wz+8, -1, 0, 0, 0.542, 0.87]], np.float32)
        O = c3d.build([dict(name='WindsockPole', texture='', color=(0.9, 0.9, 0.9, 1), params=(0.5, 2.5, 0), vertices=pv, indices=pi),
                       dict(name='Windsack1', texture='', color=(0.8, 0.8, 0.8, 1), params=(0.8, 5.0, 0), vertices=tv, indices=np.array([0, 2, 1], np.uint32))])
        open(f'{out}/Airports/{af.name}O.c3d', 'wb').write(c3d.write(O))
        for stale in (f'{out}/Airports/{af.name}.grs',):
            if os.path.exists(stale):
                try: os.remove(stale)
                except OSError: log(f'  note: remove the stale grass cache {stale} by hand')
        recs += apt.pack_one(af.name, af.lat, af.lon, round(h0), af.heading, int(round(af.len)), int(round(af.wid)), asphalt=af.asphalt, freq=af.freq,
                             reversed=af.flags[0], tow_primary_left=af.flags[1], tow_secondary_left=af.flags[2])
        la, lo = abs(af.lat), abs(af.lon)
        cup.append(f'"{af.name}",{af.name[:8]},,{int(la):02d}{(la-int(la))*60:06.3f}{"S" if af.lat < 0 else "N"},{int(lo):03d}{(lo-int(lo))*60:06.3f}{"W" if af.lon < 0 else "E"},'
                   f'{h0:.1f}m,{5 if af.asphalt else 2},{int(round(af.heading)):03d},{af.len:.1f}m,{af.freq:.3f},')
        info.append(dict(name=af.name, x=af.x0, y=af.y0, z=h0, heading=af.heading, length=af.len))
        log(f'airport {af.name}: heading {af.heading:.2f}, {af.len:.0f} x {af.wid:.0f} m, elevation {h0:.1f} m, ground {int(m.sum())} cells, photo {rgb.shape[1]} x {rgb.shape[0]} px; '
            f'flight plan TPPosX={af.x0:.1f} TPPosY={af.y0:.1f} TPPosZ={h0:.0f}')
    open(f'{out}/{name}.apt', 'wb').write(recs)
    open(f'{out}/{name}.cup', 'wb').write(('\r\n'.join(cup)+'\r\n').encode())
    json.dump(info, open(f'{work}/airports.json', 'w'))

# ------------------------------------------------------------------ 3D objects ---------------------------------------------------------------
CELL = 256            # atlas cell [px]
PAGE = 2048           # atlas page [px] (8 x 8 cells)

def stage_objects(plan, cfg, work, out, name):
    """Everything that rises above the ground model (buildings, trees, bridges) is kept as textured mesh, one Autogen/oXXYY.c3d per Condor tile
    (coordinates relative to the tile centre: x west, y north, z = altitude). Textures are packed into atlas pages; <name>.oha lists the MD5s."""
    import dds
    oc = cfg.get('objects', {})
    if oc is False or oc.get('enabled', True) is False: return True
    min_h = float(oc.get('min_height', 2.5)); sink = float(oc.get('sink', 1.5))
    ter = Terrain(plan, out); ad = f'{out}/Autogen'; os.makedirs(ad+'/Textures', exist_ok=True)
    groups = {}
    for t in plan.tiles:                                                          # every source tile belongs to the Condor tile of its centre
        x = plan.E0-(t['E0']+t['E1'])/2; y = (t['N0']+t['N1'])/2-plan.N0
        groups.setdefault((int(x//TILE), int(y//TILE)), []).append(t)
    for (tx, ty), src in sorted(groups.items()):
        fn = f'{ad}/o{tx:02d}{ty:02d}.c3d'
        if os.path.exists(fn) or not (0 <= tx < plan.nx and 0 <= ty < plan.ny): continue
        if out_of_time(): return False
        cx, cy = (tx+0.5)*TILE, (ty+0.5)*TILE
        pages = []; grid = None; meshes = {}                                      # page index -> list of (verts, tris)
        def place(im):
            """put a texture into the atlas; returns (page, x0, y0, w, h) in pixels"""
            nonlocal grid
            w, h = im.size; cw, ch = -(-w//CELL), -(-h//CELL)
            if cw > PAGE//CELL or ch > PAGE//CELL: im = im.resize((min(w, PAGE), min(h, PAGE))); w, h = im.size; cw, ch = -(-w//CELL), -(-h//CELL)
            n = PAGE//CELL
            for attempt in (0, 1):
                if grid is not None:
                    for gy in range(n-ch+1):
                        for gx in range(n-cw+1):
                            if not grid[gy:gy+ch, gx:gx+cw].any():
                                grid[gy:gy+ch, gx:gx+cw] = True
                                tile_ = np.asarray(im); pg = pages[-1]
                                pg[gy*CELL:gy*CELL+h, gx*CELL:gx*CELL+w] = tile_
                                pg[gy*CELL+h:(gy+ch)*CELL, gx*CELL:gx*CELL+w] = tile_[-1:, :]            # pad to the cell with edge pixels
                                pg[gy*CELL:(gy+ch)*CELL, gx*CELL+w:(gx+cw)*CELL] = pg[gy*CELL:(gy+ch)*CELL, gx*CELL+w-1:gx*CELL+w]
                                return len(pages)-1, gx*CELL, gy*CELL, w, h
                pages.append(np.zeros((PAGE, PAGE, 3), np.uint8)); grid = np.zeros((n, n), bool)
        nt = 0; flip_votes = 0
        for t in src:
            for ecef, uv, tri, im in load_glb(t['glb'], np.array(t['M'])):
                if im is None or not len(tri): continue
                lat, lon, h = ecef_to_lla(ecef); E, N = utm_forward(lat, lon, plan.zone, plan.south)
                x = plan.E0-E; y = N-plan.N0; z = h+plan.height_offset
                hag = z - ter.height(x, y)
                keep = hag[tri].max(1) > min_h
                if not keep.any(): continue
                tri = tri[keep]; used = np.unique(tri); remap = np.full(len(x), -1, np.int64); remap[used] = np.arange(len(used)); tri = remap[tri]
                x, y, z, hag, uv = x[used], y[used], z[used], hag[used], uv[used]
                z = np.where(hag < 1.0, z-sink, z)                                 # push the feet of walls and trunks into the ground
                pg, px, py, w, hh = place(im)
                u = (px + np.clip(uv[:, 0], 0.5/w, 1-0.5/w)*w)/PAGE; v = (py + np.clip(uv[:, 1], 0.5/hh, 1-0.5/hh)*hh)/PAGE
                V = np.zeros((len(x), 8), np.float32); V[:, 0] = x-cx; V[:, 1] = y-cy; V[:, 2] = z; V[:, 5] = 1; V[:, 6] = u; V[:, 7] = v
                p = V[:, :3][tri]; nz = np.cross(p[:, 1]-p[:, 0], p[:, 2]-p[:, 0])[:, 2]
                flip_votes += int((nz < 0).sum()) - int((nz > 0).sum())          # roofs must face up after the mirrored axes
                meshes.setdefault(pg, []).append((V, tri)); nt += len(tri)
        objs = []
        for pg in sorted(meshes):
            Vs = []; Is = []; n = 0
            for V, tri in meshes[pg]:
                Vs.append(V); Is.append((tri[:, ::-1] if flip_votes > 0 else tri)+n); n += len(V)
            dds.write_dxt(f'{ad}/Textures/a{tx:02d}{ty:02d}_{pg:03d}.dds', pages[pg])
            objs.append(dict(name=f'tiles_{pg:03d}', texture=f'Textures\\a{tx:02d}{ty:02d}_{pg:03d}.dds', color=(1, 1, 1, 1), params=(0, 0, 0),
                             vertices=np.concatenate(Vs), indices=np.concatenate(Is).astype(np.uint32).ravel()))
        if objs:
            d = c3d.write(c3d.build(objs)); open(fn+'.tmp', 'wb').write(d); os.replace(fn+'.tmp', fn)
            log(f'objects o{tx:02d}{ty:02d}: {len(src)} source tiles, {nt} triangles, {len(objs)} atlas pages, {len(d)/1e6:.0f} MB  ({time.time()-T0:.0f}s)')
        else: open(fn, 'wb').write(b'')
    oha = []
    for tx in range(plan.nx):
        for ty in range(plan.ny):
            fn = f'{ad}/o{tx:02d}{ty:02d}.c3d'; key = f'{tx*1000+ty:06d}'
            if os.path.exists(fn) and os.path.getsize(fn) > 0: oha.append(key+' '+hashlib.md5(open(fn, 'rb').read()).hexdigest())
            else: oha.append(key)
    open(f'{out}/{name}.oha', 'wb').write(('\r\n'.join(oha)+'\r\n').encode())
    return True

if __name__ == '__main__':
    a = [x for x in sys.argv[1:] if not x.startswith('--')]
    def opt(name, default=None):
        return sys.argv[sys.argv.index(name)+1] if name in sys.argv else default
    for o in ('--work', '--budget', '--only'):
        v = opt(o)
        if v in a: a.remove(v)
    if len(a) < 3: print(__doc__); sys.exit(1)
    export, cfgp, out = a[0], a[1], a[2].rstrip('/')
    name = os.path.basename(out); cfg = json.load(open(cfgp)); work = opt('--work') or os.path.join(export, '_condor_work')
    os.makedirs(work, exist_ok=True); os.makedirs(out, exist_ok=True)
    BUDGET[0] = float(opt('--budget', 1e18)); only = opt('--only')
    def stop(): log('time budget used up: run the same command again to continue'); sys.exit(2)
    plan = stage_plan(export, cfg, work)
    if only in (None, 'raster') and not stage_raster(plan, work): stop()
    if only in (None, 'terrain', 'base'): trn = stage_terrain(plan, cfg, work, out, name)
    if out_of_time(): stop()
    if only in (None, 'textures') and not stage_textures(plan, work, out, name): stop()
    if only in (None, 'base'): stage_base(plan, cfg, work, out, name, trn)
    if only in (None, 'airports'): stage_airports(plan, cfg, work, out, name)
    if only in (None, 'objects') and 'stage_objects' in globals():
        if not stage_objects(plan, cfg, work, out, name): stop()
    log(f'done: {out}')
