#!/usr/bin/env python3
"""tiles2condor.py - build a Condor 3 landscape from 3D Tiles exports (one GLB per tile, e.g. from the "earth-rea" tile library).

usage:  tiles2condor.py <export_dir> [<export_dir> ...] <config.json> <Landscapes_dir>/<Name> [--work DIR] [--budget SECONDS] [--only STAGE]

  export_dir   one or more folders with tileset.json, tiles/*.glb and (recommended) location.json. Several exports are merged; a tile that
               occurs twice is used once. The exports may mix any number of detail levels: at every place the FINEST tile wins.
  config.json  landscape options and the airports (see docs/TILES2CONDOR.md)
  output       the landscape folder to create (its name is the landscape name)

The work is done in resumable stages (each stage skips what already exists in the work folder / output):
  plan      projection, landscape grid, which tile is visible where, texture size per Condor tile; writes plan_levels.png (look at it!)
  raster    top-down render of the meshes into colour + height blocks (orthophoto and surface model), per Condor tile at its own resolution
  terrain   ground model from the surface model, airports smoothed -> .trn, .tr3, .tr3f, .tha
  textures  tile textures (DXT1 with mips), flight-planner map
  base      ini, forest maps, thermal map, hashes, empty object/airspace lists
  airports  .apt, .cup, airport grass meshes cut exactly from the terrain, aerial sub-textures, windsocks
  objects   everything standing above the ground (buildings, trees) as textured scenery meshes
--only STAGE runs just that stage (plan, raster, terrain, textures, base, airports, objects).
--budget makes a run stop cleanly after that many seconds (exit code 2; run the same command again to continue).
The work folder defaults to <first export_dir>/_condor_work. Needs python3 with numpy and Pillow. Formats: docs/TERRAIN_FORMAT.md.
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
    """every tile with content: [(glb path, accumulated transform)]. Which tile is visible where is decided later (finest wins)."""
    t = json.load(open(os.path.join(export, 'tileset.json'))); found = []
    def mat(n): return np.array(n['transform'], float).reshape(4, 4).T if 'transform' in n else np.eye(4)
    def walk(n, M):
        M = M @ mat(n)
        if 'content' in n: found.append((os.path.join(export, n['content']['uri']), M))
        for c in n.get('children', []): walk(c, M)
    walk(t['root'], np.eye(4))
    return found

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
    """landscape grid + the source tiles. World frame of the game: x = E0 - easting (west), y = northing - N0 (north)."""
    def __init__(s, d):
        s.__dict__.update(d); t = s.tiles
        s._lv = np.array([q['level'] for q in t]); s._S = np.array([q['south'] for q in t]); s._N = np.array([q['north'] for q in t])
        s._W = np.array([q['west'] for q in t]); s._E = np.array([q['east'] for q in t]); s._masks = {}
    def world(s, lat, lon):
        E, N = utm_forward(lat, lon, s.zone, s.south); return s.E0-E, N-s.N0
    def latlon(s, x, y): return utm_inverse(s.E0-x, s.N0+y, s.zone, s.south)
    def texsize(s, tx, ty): return int(s.tex.get(f'{tx},{ty}', 0))
    def hole_mask(s, i):
        """Where tile i is hidden by FINER tiles: boolean K x K grid over its lat/lon rectangle ([row from south][column from west]), or None.
        The footprints of an octree/quadtree export subdivide each other exactly, so a 2^d grid (d = level difference, at most 6) is exact."""
        if i in s._masks: return s._masks[i]
        t = s.tiles[i]; eps = 1e-9
        fin = np.nonzero((s._lv > t['level']) & (s._W < t['east']-eps) & (s._E > t['west']+eps) & (s._S < t['north']-eps) & (s._N > t['south']+eps))[0]
        m = None
        if len(fin):
            K = 2**int(min(6, s._lv[fin].max()-t['level']))
            cu = t['west'] + (np.arange(K)+0.5)/K*(t['east']-t['west']); cv = t['south'] + (np.arange(K)+0.5)/K*(t['north']-t['south'])
            m = np.zeros((K, K), bool)
            for j in fin: m |= ((cv[:, None] > s._S[j]) & (cv[:, None] < s._N[j])) & ((cu[None, :] > s._W[j]) & (cu[None, :] < s._E[j]))
            if not m.any(): m = None
        s._masks[i] = m
        return m
    def hole_coords(s, i, lat, lon):
        """normalised position of points inside tile i's rectangle (for looking up hole_mask)"""
        t = s.tiles[i]; return (lon-t['west'])/(t['east']-t['west']), (lat-t['south'])/(t['north']-t['south'])

def in_hole(mask, hu, hv):
    K = mask.shape[0]
    return mask[np.clip((hv*K).astype(np.int64), 0, K-1), np.clip((hu*K).astype(np.int64), 0, K-1)]

TEX_SIZES = (1024, 2048, 4096, 8192)

def stage_plan(exports, cfg, work):
    sig = hashlib.md5(json.dumps([[os.path.abspath(e) for e in exports], {k: cfg.get(k) for k in ('margin_tiles', 'texture_size', 'min_texture_size', 'utm_zone', 'height_offset', 'detail_min_share')}], sort_keys=True).encode()).hexdigest()
    pp = f'{work}/plan.json'
    if os.path.exists(pp):
        d = json.load(open(pp))
        if d.get('sig') != sig:
            raise SystemExit(f'The exports or the layout settings changed since the work folder {work} was started.\n'
                             f'Use a new work folder (--work DIR) or delete this one, then run again.')
        return Plan(d)
    # ---- collect the tiles of all exports
    tiles = {}; nodup = 0
    for ex in exports:
        lp = os.path.join(ex, 'location.json'); loc = {t['path']: t for t in json.load(open(lp))['tiles']} if os.path.exists(lp) else {}
        for p, M in read_tileset(ex):
            nm = os.path.basename(p)[:-4]
            if nm in tiles: nodup += 1; continue
            if not os.path.exists(p) or os.path.getsize(p) < 100: continue
            b = loc.get(nm)
            if b: rec = dict(south=b['south'], north=b['north'], west=b['west'], east=b['east'], level=int(b.get('level', len(nm))))
            else:                                                   # no location.json: footprint from the mesh, level from the footprint width
                ll = [ecef_to_lla(e)[:2] for e, _, _, _ in load_glb(p, M)]
                if not ll: continue
                la = np.concatenate([a for a, _ in ll]); lo = np.concatenate([o for _, o in ll])
                rec = dict(south=float(la.min()), north=float(la.max()), west=float(lo.min()), east=float(lo.max()))
                rec['level'] = int(round(math.log2(90.0/max(rec['east']-rec['west'], 1e-9))))+3
            rec.update(name=nm, glb=p, M=M.tolist()); tiles[nm] = rec
    tiles = list(tiles.values())
    if not tiles: raise SystemExit('no tiles found in the export folder(s)')
    lat0 = float(np.median([t['south'] for t in tiles])); lon0 = float(np.median([t['west'] for t in tiles]))
    zone = int(cfg.get('utm_zone') or (math.floor((lon0+180)/6)+1)); south = lat0 < 0
    for t in tiles:
        E, N = utm_forward(np.array([t['south'], t['south'], t['north'], t['north']]), np.array([t['west'], t['east'], t['west'], t['east']]), zone, south)
        t.update(e0=float(E.min()), e1=float(E.max()), n0=float(N.min()), n1=float(N.max()))
    Emin = min(t['e0'] for t in tiles); Emax = max(t['e1'] for t in tiles); Nmin = min(t['n0'] for t in tiles); Nmax = max(t['n1'] for t in tiles)
    m = int(cfg.get('margin_tiles', 1))
    nx = max(1, math.ceil((Emax-Emin)/TILE))+2*m; ny = max(1, math.ceil((Nmax-Nmin)/TILE))+2*m
    E0 = float(round((Emin+Emax)/2 + nx*TILE/2)); N0 = float(round((Nmin+Nmax)/2 - ny*TILE/2))      # east edge easting, south edge northing
    d = dict(sig=sig, zone=zone, south=south, nx=nx, ny=ny, E0=E0, N0=N0, data=dict(e0=Emin, e1=Emax, n0=Nmin, n1=Nmax),
             height_offset=float(cfg.get('height_offset', 0.0)), tiles=tiles, tex={})
    P = Plan(d)
    # ---- finest wins: how much of every tile is hidden by finer ones
    for i, t in enumerate(tiles):
        mk = P.hole_mask(i); t['hidden'] = float(mk.mean()) if mk is not None else 0.0
    # ---- texture size per Condor tile from the detail of the tiles visible in it
    smax = int(cfg.get('texture_size', 8192)); smin = int(cfg.get('min_texture_size', 1024)); share = float(cfg.get('detail_min_share', 0.03))
    need = {}
    for t in tiles:
        if t['hidden'] >= 1: continue
        texel = (t['e1']-t['e0'])/256.0                                # source textures are about 256 px across a tile
        S = next((s for s in TEX_SIZES if TILE/s <= texel*1.2), TEX_SIZES[-1]); S = int(min(max(S, smin), smax))
        for tx in range(max(0, int((E0-t['e1'])//TILE)), min(nx-1, int((E0-t['e0'])//TILE))+1):
            for ty in range(max(0, int((t['n0']-N0)//TILE)), min(ny-1, int((t['n1']-N0)//TILE))+1):
                ov = max(0, min(t['e1'], E0-tx*TILE)-max(t['e0'], E0-(tx+1)*TILE))*max(0, min(t['n1'], N0+(ty+1)*TILE)-max(t['n0'], N0+ty*TILE))
                if ov > 0: need.setdefault((tx, ty), {}).setdefault(S, 0.0); need[(tx, ty)][S] += ov*(1-t['hidden'])
    for (tx, ty), by in need.items():
        cum = 0.0; pick = min(by)
        for S in sorted(by, reverse=True):                               # the largest size that a real share of the tile needs
            cum += by[S]
            if cum >= share*TILE*TILE: pick = S; break
        d['tex'][f'{tx},{ty}'] = pick
    json.dump(d, open(pp, 'w'))
    P = Plan(d); report(P, cfg, work, nodup)
    return P

def report(plan, cfg, work, nodup=0):
    """summary on the console and plan_levels.png: which detail level is used where"""
    lv = sorted(set(t['level'] for t in plan.tiles))
    log(f'plan: UTM zone {plan.zone}{"S" if plan.south else "N"}, landscape {plan.nx} x {plan.ny} tiles = {plan.nx*TILE/1000:.1f} x {plan.ny*TILE/1000:.1f} km, '
        f'data {(plan.data["e1"]-plan.data["e0"])/1000:.1f} x {(plan.data["n1"]-plan.data["n0"])/1000:.1f} km' + (f', {nodup} duplicate tiles ignored' if nodup else ''))
    log('  level   tiles   used whole   partly hidden   fully hidden (finer tiles cover them)   approx. source detail')
    for L in lv:
        ts = [t for t in plan.tiles if t['level'] == L]
        log(f'  {L:5d} {len(ts):7d} {sum(t["hidden"] == 0 for t in ts):12d} {sum(0 < t["hidden"] < 1 for t in ts):15d} {sum(t["hidden"] >= 1 for t in ts):14d}'
            f'{"":27s}{np.median([(t["e1"]-t["e0"])/256 for t in ts]):.1f} m/px')
    cnt = {}
    for v in plan.tex.values(): cnt[v] = cnt.get(v, 0)+1
    log('  Condor tiles by texture size: ' + ', '.join(f'{cnt[k]} x {k} px' for k in sorted(cnt, reverse=True)) + f', {plan.nx*plan.ny-len(plan.tex)} filler')
    try:
        from PIL import ImageDraw
        sc = 900.0/max(plan.nx, plan.ny)/TILE; W = int(plan.nx*TILE*sc); H = int(plan.ny*TILE*sc); im = Image.new('RGB', (W+190, max(H, 40+18*len(lv))), (40, 40, 40)); dr = ImageDraw.Draw(im)
        Ww = plan.E0-plan.nx*TILE; Nn = plan.N0+plan.ny*TILE
        cols = [(70, 90, 160), (60, 150, 170), (70, 170, 90), (190, 190, 60), (220, 140, 50), (220, 70, 60), (200, 60, 170), (240, 240, 240)]
        col = {L: cols[min(k, len(cols)-1)] for k, L in enumerate(lv)}
        for i in np.argsort(plan._lv, kind='stable'):                    # coarse first, finer painted over them = what the build uses
            t = plan.tiles[i]
            if t['hidden'] >= 1: continue
            dr.rectangle(((t['e0']-Ww)*sc, (Nn-t['n1'])*sc, (t['e1']-Ww)*sc, (Nn-t['n0'])*sc), fill=col[t['level']])
        for k in range(plan.nx+1): dr.line((k*TILE*sc, 0, k*TILE*sc, H), fill=(0, 0, 0))
        for k in range(plan.ny+1): dr.line((0, k*TILE*sc, W, k*TILE*sc), fill=(0, 0, 0))
        for tx in range(plan.nx):
            for ty in range(plan.ny):
                dr.text(((plan.nx-1-tx)*TILE*sc+4, (plan.ny-1-ty)*TILE*sc+3), f'{tx:02d}{ty:02d} {plan.texsize(tx, ty) or "fill"}', fill=(255, 255, 255))
        for af in airfields(cfg, plan):
            a = af.to_world(0, -af.len/2); b = af.to_world(0, af.len/2)
            dr.line(((plan.E0-a[0]-Ww)*sc, (Nn-plan.N0-a[1])*sc, (plan.E0-b[0]-Ww)*sc, (Nn-plan.N0-b[1])*sc), fill=(255, 255, 255), width=3)
            dr.text(((plan.E0-a[0]-Ww)*sc+5, (Nn-plan.N0-a[1])*sc+5), af.name, fill=(255, 255, 255))
        for k, L in enumerate(lv):
            dr.rectangle((W+12, 12+18*k, W+26, 24+18*k), fill=col[L]); dr.text((W+32, 12+18*k), f'level {L}', fill=(255, 255, 255))
        dr.text((W+12, 20+18*len(lv)), 'north up; labels: Condor\ntile and texture size', fill=(200, 200, 200))
        im.save(f'{work}/plan_levels.png'); log(f'  map of the detail levels: {work}/plan_levels.png')
    except Exception as e: log('  (no preview picture:', e, ')')

# ------------------------------------------------------------------ rasteriser ----------------------------------------------------------
BLOCK = 2048          # largest raster block edge in pixels

def raster_tris(px, py, z, uv, tri, tex, W, H, zbuf, rgb, hole=None):
    """top-down z-buffer render of textured triangles into zbuf (H,W) float32 (highest wins) and rgb (H,W,3).
    hole = (hu, hv, mask): per-vertex position in the tile's rectangle and the mask of the parts hidden by finer tiles (not drawn)"""
    X = px[tri]; Y = py[tri]; Z = z[tri]                                    # (n,3)
    x0 = np.floor(X.min(1)).astype(np.int64); x1 = np.ceil(X.max(1)).astype(np.int64)
    y0 = np.floor(Y.min(1)).astype(np.int64); y1 = np.ceil(Y.max(1)).astype(np.int64)
    den = (Y[:, 1]-Y[:, 2])*(X[:, 0]-X[:, 2]) + (X[:, 2]-X[:, 1])*(Y[:, 0]-Y[:, 2])
    keep = (np.abs(den) > 1e-9) & (x1 >= 0) & (y1 >= 0) & (x0 < W) & (y0 < H)
    size = np.maximum(x1-x0, y1-y0)+1
    th, tw = tex.shape[:2]; U = uv[tri]
    if hole is not None: HU = hole[0][tri]; HV = hole[1][tri]
    for K in (4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048):
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
            if hole is not None and ins.any():
                ins &= ~in_hole(hole[2], l0*HU[q, 0, None, None] + l1*HU[q, 1, None, None] + l2*HU[q, 2, None, None],
                                l0*HV[q, 0, None, None] + l1*HV[q, 1, None, None] + l2*HV[q, 2, None, None])
            if not ins.any(): continue
            zz = (l0*Z[q, 0, None, None] + l1*Z[q, 1, None, None] + l2*Z[q, 2, None, None])[ins]
            uu = (l0*U[q, 0, 0, None, None] + l1*U[q, 1, 0, None, None] + l2*U[q, 2, 0, None, None])[ins]
            vv = (l0*U[q, 0, 1, None, None] + l1*U[q, 1, 1, None, None] + l2*U[q, 2, 1, None, None])[ins]
            pix = np.broadcast_to(gy, ins.shape)[ins].astype(np.int64)*W + np.broadcast_to(gx, ins.shape)[ins].astype(np.int64)
            col = tex[np.clip((vv*th).astype(np.int64), 0, th-1), np.clip((uu*tw).astype(np.int64), 0, tw-1)]
            order = np.argsort(zz, kind='stable'); pix = pix[order]; zz = zz[order].astype(np.float32); col = col[order]
            win = zz > zbuf.ravel()[pix]
            zbuf.ravel()[pix[win]] = zz[win]; rgb.reshape(-1, 3)[pix[win]] = col[win]

def block_path(work, tx, ty, i, j): return f'{work}/ras_{tx:02d}{ty:02d}_{i}_{j}.npz'

def tile_frame(plan, tx, ty):
    """(easting of the west edge, northing of the north edge, texture size, metres per pixel, block size, blocks per side) of a Condor tile"""
    S = plan.texsize(tx, ty); bs = min(S, BLOCK) if S else 0
    return plan.E0-(tx+1)*TILE, plan.N0+(ty+1)*TILE, S, (TILE/S if S else 0), bs, (S//bs if S else 0)

def stage_raster(plan, work):
    """every Condor tile that has data is rendered at its own resolution, in blocks (i from the west edge, j from the north edge)"""
    for tx in range(plan.nx):
        for ty in range(plan.ny):
            ew, nn, S, res, bs, nb = tile_frame(plan, tx, ty)
            if not S: continue
            for i in range(nb):
                for j in range(nb):
                    bp = block_path(work, tx, ty, i, j)
                    if os.path.exists(bp): continue
                    e0 = ew + i*bs*res; n1 = nn - j*bs*res; ext = bs*res
                    src = [k for k, t in enumerate(plan.tiles) if t['hidden'] < 1 and t['e1'] > e0-2 and t['e0'] < e0+ext+2 and t['n1'] > n1-ext-2 and t['n0'] < n1+2]
                    if out_of_time(): return False
                    zbuf = np.full((bs, bs), -1e9, np.float32); rgb = np.zeros((bs, bs, 3), np.uint8)
                    for k in sorted(src, key=lambda k: plan.tiles[k]['level']):
                        t = plan.tiles[k]; mk = plan.hole_mask(k)
                        for ecef, uv, tri, im in load_glb(t['glb'], np.array(t['M'])):
                            lat, lon, h = ecef_to_lla(ecef); E, N = utm_forward(lat, lon, plan.zone, plan.south)
                            tex = np.asarray(im) if im is not None else np.full((2, 2, 3), 128, np.uint8)
                            hole = (*plan.hole_coords(k, lat, lon), mk) if mk is not None else None
                            raster_tris((E-e0)/res, (n1-N)/res, h+plan.height_offset, uv, tri, tex, bs, bs, zbuf, rgb, hole)
                    valid = zbuf > -1e8
                    np.savez(bp+'.tmp.npz', rgb=rgb, z=np.where(valid, np.clip(np.rint((zbuf+500)*10), 1, 65535), 0).astype(np.uint16)); os.replace(bp+'.tmp.npz', bp)
                    log(f'raster tile {tx:02d}{ty:02d} ({S} px) block {i},{j}: {len(src)} source tiles, coverage {valid.mean()*100:.0f}%  ({time.time()-T0:.0f}s)')
    return True

def load_tile_raster(plan, work, tx, ty):
    """assembled raster of a Condor tile: rgb (S,S,3) uint8, z (S,S) uint16 (0 = no data, else (height+500)*10); None when the tile has no data"""
    ew, nn, S, res, bs, nb = tile_frame(plan, tx, ty)
    if not S: return None, None
    rgb = np.zeros((S, S, 3), np.uint8); z = np.zeros((S, S), np.uint16)
    for i in range(nb):
        for j in range(nb):
            bp = block_path(work, tx, ty, i, j)
            if not os.path.exists(bp): raise SystemExit(f'raster block {bp} is missing: run the raster stage first')
            d = np.load(bp); rgb[j*bs:(j+1)*bs, i*bs:(i+1)*bs] = d['rgb']; z[j*bs:(j+1)*bs, i*bs:(i+1)*bs] = d['z']
    return rgb, z

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
    """per G-cell statistics of the rasters, image orientation (row 0 = north edge, column 0 = west edge):
    zmin (lowest surface height in the cell, nan = no data), rgb mean"""
    p = f'{work}/grid.npz'
    if os.path.exists(p):
        d = np.load(p); return d['zmin'], d['rgb']
    W = plan.nx*1024; H = plan.ny*1024
    zmin = np.full((H, W), np.nan, np.float32); rgb = np.zeros((H, W, 3), np.float32)
    for tx in range(plan.nx):
        for ty in range(plan.ny):
            c, z = load_tile_raster(plan, work, tx, ty)
            if c is None: continue
            f = c.shape[0]//1024; r0 = (plan.ny-1-ty)*1024; c0 = (plan.nx-1-tx)*1024
            v = (z > 0).reshape(1024, f, 1024, f); zz = np.where(z > 0, z.astype(np.float32)/10-500, np.inf).reshape(1024, f, 1024, f)
            m = zz.min((1, 3)); n = v.sum((1, 3))
            zmin[r0:r0+1024, c0:c0+1024] = np.where(n > 0, m, np.nan)
            rgb[r0:r0+1024, c0:c0+1024] = (c.reshape(1024, f, 1024, f, 3)*v[..., None]).sum((1, 3), dtype=np.float32)/np.maximum(n, 1)[..., None]
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
    fill = np.load(fp); rng = np.random.default_rng(7)
    for tx in range(plan.nx):
        for ty in range(plan.ny):
            tp = f'{out}/Textures/t{tx:02d}{ty:02d}.dds'
            if os.path.exists(tp): continue
            if out_of_time(): return False
            cw = plan.nx-1-tx; rn = plan.ny-1-ty                                 # tile position counted from the west / north edge
            fg = fill[rn*1024:(rn+1)*1024, cw*1024:(cw+1)*1024]
            c, z = load_tile_raster(plan, work, tx, ty)
            if c is None:                                                         # filler tile: small texture from the extended colours
                img = np.clip(fg.reshape(512, 2, 512, 2, 3).mean((1, 3)) + rng.normal(0, 4, (512, 512, 1)), 0, 255).astype(np.uint8)
            else:
                S = c.shape[0]; f = S//1024; img = c
                for r in range(0, 1024, 128):                                     # where the tile has no data: extended colours with a little noise
                    zz = z[r*f:(r+128)*f]
                    if (zz > 0).all(): continue
                    fb = np.repeat(np.repeat(fg[r:r+128], f, 0), f, 1)
                    fb = np.clip(fb + rng.normal(0, 4, (128*f, S, 1)), 0, 255).astype(np.uint8)
                    img[r*f:(r+128)*f] = np.where((zz > 0)[..., None], img[r*f:(r+128)*f], fb)
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
def crop_ortho(plan, work, e0, e1, n0, n1, max_px=20e6):
    """north-up orthophoto of a UTM rectangle at the finest resolution of the Condor tiles it touches (coarsened to stay below max_px);
    returns rgb (h,w,3), valid (h,w) and the exact bounds used"""
    txs = range(max(0, int((plan.E0-e1)//TILE)), min(plan.nx-1, int((plan.E0-e0)//TILE))+1)
    tys = range(max(0, int((n0-plan.N0)//TILE)), min(plan.ny-1, int((n1-plan.N0)//TILE))+1)
    sizes = [plan.texsize(tx, ty) for tx in txs for ty in tys if plan.texsize(tx, ty)]
    res = TILE/max(sizes) if sizes else TILE/1024
    while (e1-e0)*(n1-n0)/res**2 > max_px: res *= 2
    w = max(4, int((e1-e0)/res)//4*4); h = max(4, int((n1-n0)/res)//4*4); e1 = e0+w*res; n0 = n1-h*res
    rgb = np.zeros((h, w, 3), np.uint8); val = np.zeros((h, w), bool)
    E = e0+(np.arange(w)+0.5)*res; N = n1-(np.arange(h)+0.5)*res
    for tx in txs:
        for ty in tys:
            ew, nn, S, rt, bs, nb = tile_frame(plan, tx, ty)
            mc = (E >= ew) & (E < ew+TILE); mr = (N <= nn) & (N > nn-TILE)
            if not S or not mc.any() or not mr.any(): continue
            c, z = load_tile_raster(plan, work, tx, ty)
            cc = np.clip(((E[mc]-ew)/rt).astype(np.int64), 0, S-1); rr = np.clip(((nn-N[mr])/rt).astype(np.int64), 0, S-1)
            rgb[np.ix_(np.nonzero(mr)[0], np.nonzero(mc)[0])] = c[np.ix_(rr, cc)]; val[np.ix_(np.nonzero(mr)[0], np.nonzero(mc)[0])] = z[np.ix_(rr, cc)] > 0
    return rgb, val, (e0, e1, n0, n1)

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
        rgb, val, (e0, e1, n0, n1) = crop_ortho(plan, work, e0, e1, n0, n1)          # at most ~80 MB
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
    min_h = float(oc.get('min_height', 2.5)); sink = float(oc.get('sink', 1.5)); min_level = int(oc.get('min_level', 0))
    ter = Terrain(plan, out); ad = f'{out}/Autogen'; os.makedirs(ad+'/Textures', exist_ok=True)
    groups = {}
    for k, t in enumerate(plan.tiles):                                            # every source tile belongs to the Condor tile of its centre
        if t['hidden'] >= 1 or t['level'] < min_level: continue                   # fully covered by finer tiles / too coarse for 3D objects
        x = plan.E0-(t['e0']+t['e1'])/2; y = (t['n0']+t['n1'])/2-plan.N0
        groups.setdefault((int(x//TILE), int(y//TILE)), []).append(k)
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
        for k in src:
            t = plan.tiles[k]; mk = plan.hole_mask(k)
            for ecef, uv, tri, im in load_glb(t['glb'], np.array(t['M'])):
                if im is None or not len(tri): continue
                lat, lon, h = ecef_to_lla(ecef); E, N = utm_forward(lat, lon, plan.zone, plan.south)
                if mk is not None:                                                 # drop what finer tiles replace (by triangle centre)
                    hu, hv = plan.hole_coords(k, lat, lon); tri = tri[~in_hole(mk, hu[tri].mean(1), hv[tri].mean(1))]
                    if not len(tri): continue
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
    exports, cfgp, out = a[:-2], a[-2], a[-1].rstrip('/')
    for e in exports:
        if not os.path.exists(os.path.join(e, 'tileset.json')): raise SystemExit(f'{e} is not an export folder (no tileset.json)')
    name = os.path.basename(out); cfg = json.load(open(cfgp)); work = opt('--work') or os.path.join(exports[0], '_condor_work')
    os.makedirs(work, exist_ok=True)
    BUDGET[0] = float(opt('--budget', 1e18)); only = opt('--only')
    stages = ('plan', 'raster', 'terrain', 'textures', 'base', 'airports', 'objects')
    if only not in (None,)+stages: raise SystemExit(f'--only must be one of: {", ".join(stages)}')
    def stop(): log('time budget used up: run the same command again to continue'); sys.exit(2)
    fresh = not os.path.exists(f'{work}/plan.json')
    plan = stage_plan(exports, cfg, work)
    if only == 'plan':
        if not fresh: report(plan, cfg, work)
        sys.exit(0)
    os.makedirs(out, exist_ok=True)
    if only in (None, 'raster') and not stage_raster(plan, work): stop()
    if only in (None, 'terrain', 'base'): trn = stage_terrain(plan, cfg, work, out, name)
    if out_of_time(): stop()
    if only in (None, 'textures') and not stage_textures(plan, work, out, name): stop()
    if only in (None, 'base'): stage_base(plan, cfg, work, out, name, trn)
    if only in (None, 'airports'): stage_airports(plan, cfg, work, out, name)
    if only in (None, 'objects') and not stage_objects(plan, cfg, work, out, name): stop()
    log(f'done: {out}')
