#!/usr/bin/env python3
"""c3view: interactive 3D preview of a COMPILED Condor 3 landscape (reads only the files the game reads).

    c3view.py <Condor3>/Landscapes/<Name> [--ctl DIR] [--size WxH] [--hidden]

What it draws (formats: docs/TERRAIN_FORMAT.md):
  terrain    HeightMaps/22.5m/hXXYY.tr3f (else HeightMaps/hXXYY.tr3, resampled to the game's 22.5 m grid), triangulated with the game's
             alternating diagonal, textured with Textures/tXXYY.dds (else empty.dds); water texels (alpha < 0.8) tinted
  autogen    Autogen/oXXYY.c3d (tiles listed with a value in <L>.oha) + Autogen/Textures
  airports   <L>.apt + Airports/<A>G.c3d / <A>O.c3d (+ Airports/Textures) in the airport frame (origin = apt lat/lon via the .trn UTM
             zone, +Y = runway heading, +X = left, z = terrain height under the origin)
  objects    <L>.obj placements, models from Landscapes/<L>/World/Objects or <game>/World/Objects
World frame as in the game: x = metres WEST of the east edge, y = metres NORTH of the south edge, z = altitude.
Camera angles as the mod's `att`: yaw = compass heading (clockwise from north), pitch = up positive, roll = right wing down.

Interactive: left mouse drag = look, W/S forward/back, A/D left/right, E/Q up/down, Shift = x5, mouse wheel = speed,
             1 terrain, 2 autogen, 3 airports, 4 objects, G wireframe, F fog, P screenshot, R start view, H help, Esc quit.

Remote control (for chat agents): write lines into <ctl>/cmd.txt (default <game>/postfx/viewer/cmd.txt); the file is consumed
within ~0.1 s, results are appended to <ctl>/log.txt, screenshots go to <ctl>/shots/<name>.png. Commands:
  cam X Y Z [YAW [PITCH [ROLL]]]   absolute camera (world frame)      look YAW [PITCH [ROLL]]   absolute view direction
  move DX DY DZ                    world offset                       fly FWD [RIGHT [UP]]      relative to the view direction
  turn DYAW [DPITCH]               relative turn                      agl H                     put the camera H m above the terrain
  goto NAME                        a spot saved with the mod's `dump` (<game>/postfx/positions.txt): the game's camera if the
                                   line has it (exactly the game's view; use size 1920 942 for the same image), else the glider
  goto X Y Z [PITCH ROLL YAW]      same order as positions.txt        state                     log camera, terrain height, layers
  shot [NAME]                      screenshot of the next frame       fov DEG                   vertical field of view (game: 60.6)
  size W H                         window size                        layer terrain|autogen|airports|objects on|off
  wire on|off   fog on|off   sun AZIMUTH ELEVATION   speed M/S   wait MS   quit
"""
import ctypes, math, os, struct, sys, time
import numpy as np
import glfw
from OpenGL import GL as gl
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c3d as C3D, apt as APT, utm as UTM

# ------------------------------------------------------------------ files -------------------------------------------------------------------
def first(*paths):
    for p in paths:
        if p and os.path.exists(p): return p
    return None

def find_ci(folder, name):
    """case-insensitive file lookup (the game runs on Windows), name may contain sub folders"""
    p = os.path.join(folder, *name.replace('\\', '/').split('/'))
    if os.path.exists(p): return p
    cur = folder
    for part in name.replace('\\', '/').split('/'):
        if not os.path.isdir(cur): return None
        hit = [e for e in os.listdir(cur) if e.lower() == part.lower()]
        if not hit: return None
        cur = os.path.join(cur, hit[0])
    return cur

class Landscape:
    def __init__(s, path):
        s.dir = os.path.abspath(path.rstrip('/')); s.name = os.path.basename(s.dir); s.game = os.path.dirname(os.path.dirname(s.dir))
        d = open(os.path.join(s.dir, s.name + '.trn'), 'rb').read(36)
        s.w, s.h, s.cell, dx, dy, s.E0, s.N0, s.zone, hemi = struct.unpack('<II5fII', d)
        s.south = hemi == ord('S'); s.tx, s.ty = s.w // 64, s.h // 64; s.T = 64 * s.cell
        s.heights = {}
        for tx in range(s.tx):
            for ty in range(s.ty): s.heights[(tx, ty)] = s.load_heights(tx, ty)
        oha = first(os.path.join(s.dir, s.name + '.oha'))
        s.autogen = set()
        if oha:
            for ln in open(oha, encoding='latin1').read().splitlines():
                f = ln.split()
                if len(f) >= 2: s.autogen.add((int(f[0]) // 1000, int(f[0]) % 1000))

    def tile_file(s, folder, pre, tx, ty, ext):
        return first(os.path.join(s.dir, folder, f'{pre}{tx:03d}{ty:03d}{ext}'), os.path.join(s.dir, folder, f'{pre}{tx:02d}{ty:02d}{ext}'))

    def load_heights(s, tx, ty):
        p = s.tile_file('HeightMaps/22.5m', 'h', tx, ty, '.tr3f')
        if p: return np.fromfile(p, '<f4').reshape(257, 257).astype(np.float32)
        p = s.tile_file('HeightMaps', 'h', tx, ty, '.tr3')
        if not p: return np.zeros((257, 257), np.float32)
        a = np.fromfile(p, '<u2').reshape(193, 193).astype(np.float32)             # 30 m -> 22.5 m, bilinear like the game
        t = np.arange(257) * (192 / 256.0); i0 = np.minimum(t.astype(int), 191); f = (t - i0)[:, None]
        rows = a[i0] * (1 - f) + a[i0 + 1] * f
        f2 = (t - i0)[None, :]
        return (rows[:, i0] * (1 - f2) + rows[:, i0 + 1] * f2).astype(np.float32)

    def height(s, x, y):
        tx, ty = int(x // s.T), int(y // s.T)
        tx = min(max(tx, 0), s.tx - 1); ty = min(max(ty, 0), s.ty - 1)
        h = s.heights[(tx, ty)]; st = s.T / 256
        u = min(max((x - tx * s.T) / st, 0), 255.999); v = min(max((y - ty * s.T) / st, 0), 255.999)
        i, j = int(u), int(v); fu, fv = u - i, v - j
        return float(h[i, j] * (1 - fu) * (1 - fv) + h[i + 1, j] * fu * (1 - fv) + h[i, j + 1] * (1 - fu) * fv + h[i + 1, j + 1] * fu * fv)

    def world(s, lat, lon):
        E, N = UTM.forward(lat, lon, s.zone)
        if s.south: N += 10000000.0
        return s.E0 - E, N - s.N0

# ------------------------------------------------------------------ GL helpers ----------------------------------------------------------------
VS = """#version 410 core
layout(location=0) in vec3 pos; layout(location=1) in vec3 nrm; layout(location=2) in vec2 uv;
uniform mat4 mvp; uniform vec3 eye;
out vec3 vN; out vec2 vUV; out float vD;
void main(){ gl_Position = mvp * vec4(pos, 1.0); vN = nrm; vUV = uv; vD = length(pos - eye); }"""
FS = """#version 410 core
in vec3 vN; in vec2 vUV; in float vD; out vec4 o;
uniform sampler2D tex; uniform int hasTex; uniform vec4 color; uniform vec3 sun; uniform float ambient, diffuse;
uniform vec3 fogColor; uniform float fogDist; uniform int water; uniform int wire;
void main(){
  vec4 c = color; if (hasTex == 1) c *= texture(tex, vUV);
  if (wire == 1) { o = vec4(1.0, 0.85, 0.2, 1.0); return; }
  if (water == 1 && c.a < 0.8) c = vec4(mix(c.rgb, vec3(0.20, 0.32, 0.42), 0.6), 1.0);
  if (c.a < 0.5) discard;
  float l = ambient + diffuse * abs(dot(normalize(vN), sun));
  vec3 rgb = c.rgb * min(l, 1.15);
  if (fogDist > 0.0) rgb = mix(rgb, fogColor, 1.0 - exp(-vD / fogDist));
  o = vec4(rgb, 1.0);
}"""
SKY_VS = """#version 410 core
const vec2 p[3] = vec2[3](vec2(-1,-1), vec2(3,-1), vec2(-1,3)); out vec2 q;
void main(){ q = p[gl_VertexID]; gl_Position = vec4(q, 0.999, 1.0); }"""
SKY_FS = """#version 410 core
in vec2 q; out vec4 o; uniform mat4 invVP; uniform vec3 fogColor;
void main(){ vec4 a = invVP * vec4(q, 1.0, 1.0); vec4 b = invVP * vec4(q, -1.0, 1.0); vec3 d = normalize(a.xyz/a.w - b.xyz/b.w);
  float t = clamp(d.y, 0.0, 1.0); o = vec4(mix(fogColor, vec3(0.30, 0.50, 0.85), pow(t, 0.6)), 1.0); }"""

def shader(vs, fs):
    def comp(src, kind):
        sh = gl.glCreateShader(kind); gl.glShaderSource(sh, src); gl.glCompileShader(sh)
        if not gl.glGetShaderiv(sh, gl.GL_COMPILE_STATUS): raise RuntimeError(gl.glGetShaderInfoLog(sh).decode())
        return sh
    p = gl.glCreateProgram()
    gl.glAttachShader(p, comp(vs, gl.GL_VERTEX_SHADER)); gl.glAttachShader(p, comp(fs, gl.GL_FRAGMENT_SHADER)); gl.glLinkProgram(p)
    if not gl.glGetProgramiv(p, gl.GL_LINK_STATUS): raise RuntimeError(gl.glGetProgramInfoLog(p).decode())
    return p

class Mesh:
    """one VBO/IBO; parts = [(first_index, index_count, texture or 0, color)]"""
    def __init__(s, verts, idx, parts, kind):
        s.kind = kind; s.parts = parts; s.n = len(idx)
        s.vao = gl.glGenVertexArrays(1); gl.glBindVertexArray(s.vao)
        vb = gl.glGenBuffers(1); gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vb)
        v = np.ascontiguousarray(verts, np.float32); gl.glBufferData(gl.GL_ARRAY_BUFFER, v.nbytes, v, gl.GL_STATIC_DRAW)
        ib = gl.glGenBuffers(1); gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, ib)
        i = np.ascontiguousarray(idx, np.uint32); gl.glBufferData(gl.GL_ELEMENT_ARRAY_BUFFER, i.nbytes, i, gl.GL_STATIC_DRAW)
        for loc, n, off in ((0, 3, 0), (1, 3, 12), (2, 2, 24)):
            gl.glEnableVertexAttribArray(loc); gl.glVertexAttribPointer(loc, n, gl.GL_FLOAT, False, 32, ctypes.c_void_p(off))
        gl.glBindVertexArray(0)
        s.tris = len(idx) // 3

def to_gl(xyz):
    """world (x west, y north, z up) -> GL (x east, y up, z south). Mirror + axis swap: a proper right-handed view of the world"""
    xyz = np.asarray(xyz, np.float32)
    return np.stack([-xyz[..., 0], xyz[..., 2], -xyz[..., 1]], -1)

FMT = {0x31545844: (0x83F1, 8), 0x33545844: (0x83F2, 16), 0x35545844: (0x83F3, 16)}   # DXT1, DXT3, DXT5
class Textures:
    def __init__(s): s.cache = {}; s.aniso = 0; s.bytes = 0
    def get(s, path):
        if not path: return 0
        key = os.path.normcase(path)
        if key in s.cache: return s.cache[key]
        try: t = s.load(path)
        except Exception as e: print('texture', path, e); t = 0
        s.cache[key] = t
        return t
    def load(s, path):
        d = open(path, 'rb').read()
        if d[:4] != b'DDS ': raise ValueError('not a DDS')
        h, w = struct.unpack_from('<II', d, 12); mips = max(1, struct.unpack_from('<I', d, 28)[0])
        pf, fourcc, bits = struct.unpack_from('<III', d, 80)
        t = gl.glGenTextures(1); gl.glBindTexture(gl.GL_TEXTURE_2D, t); off = 128; lv = 0
        if pf & 4:
            fmt, bs = FMT[fourcc]
            for lv in range(mips):
                mw, mh = max(1, w >> lv), max(1, h >> lv); n = max(1, (mw + 3) // 4) * max(1, (mh + 3) // 4) * bs
                if off + n > len(d): lv -= 1; break
                gl.glCompressedTexImage2D(gl.GL_TEXTURE_2D, lv, fmt, mw, mh, 0, np.frombuffer(d, np.uint8, n, off)); off += n; s.bytes += n
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAX_LEVEL, max(lv, 0))
        elif bits == 32:
            for lv in range(mips):
                mw, mh = max(1, w >> lv), max(1, h >> lv); n = mw * mh * 4
                if off + n > len(d): lv -= 1; break
                gl.glTexImage2D(gl.GL_TEXTURE_2D, lv, gl.GL_RGBA8, mw, mh, 0, gl.GL_BGRA, gl.GL_UNSIGNED_BYTE, np.frombuffer(d, np.uint8, n, off)); off += n; s.bytes += n
            if mips == 1: gl.glGenerateMipmap(gl.GL_TEXTURE_2D)
            else: gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAX_LEVEL, max(lv, 0))
        else: raise ValueError(f'unsupported DDS ({bits} bit)')
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR_MIPMAP_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        if s.aniso: gl.glTexParameterf(gl.GL_TEXTURE_2D, 0x84FE, s.aniso)
        return t

# ------------------------------------------------------------------ scene ----------------------------------------------------------------------
def terrain_mesh(L, tx, ty, tex):
    h = L.heights[(tx, ty)]; n = 257; st = L.T / 256
    i, j = np.meshgrid(np.arange(n), np.arange(n), indexing='ij')
    x = tx * L.T + i * st; y = ty * L.T + j * st
    gx = np.gradient(h, st, axis=0); gy = np.gradient(h, st, axis=1)
    nw = np.stack([-gx, -gy, np.ones_like(h)], -1); nw /= np.linalg.norm(nw, axis=-1, keepdims=True)
    V = np.zeros((n, n, 8), np.float32)
    V[..., 0:3] = to_gl(np.stack([x, y, h], -1)); V[..., 3:6] = to_gl(nw)
    V[..., 6] = 1 - i / 256.0; V[..., 7] = 1 - j / 256.0                       # texture: column 0 = west (x max), row 0 = north (y max)
    a = (i[:-1, :-1] * n + j[:-1, :-1]).ravel(); gi = (tx * 256 + i[:-1, :-1]).ravel(); gj = (ty * 256 + j[:-1, :-1]).ravel()
    even = (gi + gj) % 2 == 0                                                   # the game's alternating diagonal (TERRAIN_FORMAT 6.2)
    b, c, d = a + n, a + 1, a + n + 1                                           # (i+1,j), (i,j+1), (i+1,j+1)
    t1 = np.where(even[:, None], np.stack([a, b, d], 1), np.stack([a, b, c], 1))
    t2 = np.where(even[:, None], np.stack([a, d, c], 1), np.stack([b, d, c], 1))
    idx = np.concatenate([t1, t2], 1).reshape(-1).astype(np.uint32)
    return Mesh(V.reshape(-1, 8), idx, [(0, len(idx), tex, (1, 1, 1, 1))], 'terrain')

def c3d_mesh(path, texdir, textures, xform, kind):
    """xform(v (n,3) model coords) -> world xyz; normals rotated with the same linear part"""
    c = C3D.parse(open(path, 'rb').read()); v = np.asarray(c['vertices'], np.float32)
    if not len(v): return None
    p, nrm = xform(v[:, 0:3], v[:, 3:6])
    V = np.zeros((len(v), 8), np.float32); V[:, 0:3] = to_gl(p); V[:, 3:6] = to_gl(nrm); V[:, 6:8] = v[:, 6:8]
    parts = []
    for ob in c['objects']:
        name = ob['name'].lower()
        if name.startswith('windsack'): continue                                # windsock marker triangles (the game draws a sock there)
        t = textures.get(find_ci(texdir, ob['texture'])) if ob['texture'] else 0
        parts.append((ob['first_index'], ob['index_count'], t, tuple(ob['color'])))
    return Mesh(V, c['indices'], parts, kind)

def build_scene(L, textures, log):
    meshes = []; t0 = time.time()
    empty = textures.get(first(os.path.join(L.dir, 'Textures', 'empty.dds')))
    for (tx, ty) in sorted(L.heights):
        tex = textures.get(L.tile_file('Textures', 't', tx, ty, '.dds')) or empty
        meshes.append(terrain_mesh(L, tx, ty, tex))
    for (tx, ty) in sorted(L.autogen):
        p = L.tile_file('Autogen', 'o', tx, ty, '.c3d')
        if not p: log(f'autogen {tx} {ty}: listed in .oha but no file'); continue
        cx, cy = (tx + 0.5) * L.T, (ty + 0.5) * L.T
        m = c3d_mesh(p, os.path.join(L.dir, 'Autogen'), textures, lambda q, n: (q + np.float32([cx, cy, 0]), n), 'autogen')
        if m: meshes.append(m)
    aptp = first(os.path.join(L.dir, L.name + '.apt'))
    L.airports = APT.unpack(open(aptp, 'rb').read()) if aptp else []
    for a in L.airports:
        ox, oy = L.world(a['lat'], a['lon']); a['x'], a['y'] = ox, oy; z0 = L.height(ox, oy); H = math.radians(a['direction'])
        R = np.float32([[math.cos(H), math.sin(H), 0], [-math.sin(H), math.cos(H), 0], [0, 0, 1]])   # rows: images of local +X, +Y, +Z
        def xf(q, n, ox=ox, oy=oy, z0=z0, R=R): return q @ R + np.float32([ox, oy, z0]), n @ R
        for suffix in ('G', 'O'):
            p = find_ci(os.path.join(L.dir, 'Airports'), f"{a['name']}{suffix}.c3d")
            if p:
                m = c3d_mesh(p, os.path.join(L.dir, 'Airports'), textures, xf, 'airports')
                if m: meshes.append(m)
    objp = first(os.path.join(L.dir, L.name + '.obj')); nobj = 0
    if objp:
        d = open(objp, 'rb').read()
        for k in range(0, len(d) - 151, 152):
            x, y, z, sc, rot = struct.unpack_from('<5f', d, k); nl = d[k + 20]; nm = d[k + 21:k + 21 + nl].decode('latin1')
            if not nm.lower().endswith('.c3d'): nm += '.c3d'
            p = find_ci(os.path.join(L.dir, 'World', 'Objects'), nm) or find_ci(os.path.join(L.game, 'World', 'Objects'), nm)
            if not p: log(f'object {nm}: model not found'); continue
            R = np.float32([[math.cos(rot), math.sin(rot), 0], [-math.sin(rot), math.cos(rot), 0], [0, 0, 1]])
            z0 = L.height(x, y)
            def xf(q, n, R=R, x=x, y=y, z0=z0, sc=sc): return (q * sc) @ R + np.float32([x, y, z0]), n @ R
            m = c3d_mesh(p, os.path.dirname(p), textures, xf, 'objects')
            if m: meshes.append(m); nobj += 1
    tris = {}
    for m in meshes: tris[m.kind] = tris.get(m.kind, 0) + m.tris
    log(f"scene: {L.name} {L.tx}x{L.ty} tiles, zone {L.zone}{'S' if L.south else 'N'}, triangles {tris}, {len(L.airports)} airports, "
        f"{nobj} world objects, textures {len(textures.cache)} ({textures.bytes >> 20} MB), {time.time() - t0:.1f} s")
    return meshes

# ------------------------------------------------------------------ camera -----------------------------------------------------------------------
def axes(yaw, pitch, roll):
    """world-frame forward / right / up of a camera with compass yaw, pitch up, roll right-wing-down (degrees)"""
    ps, th, ph = map(math.radians, (yaw, pitch, roll))
    f = np.array([-math.sin(ps) * math.cos(th), math.cos(ps) * math.cos(th), math.sin(th)])
    rl = np.array([-math.cos(ps), -math.sin(ps), 0.0])                          # level right (world x is west)
    ul = np.array([math.sin(th) * math.sin(ps), -math.sin(th) * math.cos(ps), math.cos(th)])
    r = rl * math.cos(ph) - ul * math.sin(ph); u = ul * math.cos(ph) + rl * math.sin(ph)
    return f, r, u

def perspective(fovy, aspect, near, far):
    f = 1 / math.tan(math.radians(fovy) / 2); m = np.zeros((4, 4))
    m[0, 0] = f / aspect; m[1, 1] = f; m[2, 2] = (far + near) / (near - far); m[2, 3] = 2 * far * near / (near - far); m[3, 2] = -1
    return m

class Viewer:
    def __init__(s, L, ctl, size, hidden):
        s.L = L; s.ctl = ctl; os.makedirs(os.path.join(ctl, 'shots'), exist_ok=True)
        s.logf = open(os.path.join(ctl, 'log.txt'), 'a')
        s.layers = dict(terrain=True, autogen=True, airports=True, objects=True)
        s.wire = False; s.fog = True; s.fogDist = 25000.0; s.fov = 60.6; s.speed = 60.0
        s.sun = (200.0, 45.0); s.pending_shot = None; s.quit = False; s.wait_until = 0
        if not glfw.init(): raise SystemExit('glfw init failed')
        for k, v in ((glfw.CONTEXT_VERSION_MAJOR, 4), (glfw.CONTEXT_VERSION_MINOR, 1), (glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE),
                     (glfw.OPENGL_FORWARD_COMPAT, True), (glfw.SAMPLES, 4), (glfw.DEPTH_BITS, 32)):
            glfw.window_hint(k, v)
        if hidden: glfw.window_hint(glfw.VISIBLE, False)
        s.win = glfw.create_window(size[0], size[1], f'c3view - {L.name}', None, None)
        glfw.make_context_current(s.win); glfw.swap_interval(1)
        s.prog = shader(VS, FS); s.sky = shader(SKY_VS, SKY_FS); s.skyvao = gl.glGenVertexArrays(1)
        s.tex = Textures()
        try: s.tex.aniso = min(16.0, gl.glGetFloatv(0x84FF))
        except Exception: s.tex.aniso = 0
        s.meshes = build_scene(L, s.tex, s.log)
        s.start_view(); s.home = (s.pos.copy(), s.yaw, s.pitch)
        glfw.set_scroll_callback(s.win, lambda w, dx, dy: s.set_speed(s.speed * (1.25 ** dy)))
        glfw.set_key_callback(s.win, s.on_key)
        s.drag = None; s.last_cmd = 0; s.frames = 0; s.fps = 0; s.fps_t = time.time()
        s.log('ready: drag = look, WASD/QE = move, wheel = speed, 1-4 layers, G wire, F fog, P shot, R start view, H help')

    def log(s, msg):
        line = time.strftime('%H:%M:%S ') + msg
        print(line, flush=True); s.logf.write(line + '\n'); s.logf.flush()

    def start_view(s):
        L = s.L
        if L.airports:                                                          # where the glider starts: 200 m after the runway start
            a = L.airports[0]; H = math.radians(a['direction']); d = -a['length'] / 2 + 200
            x = a['x'] - d * math.sin(H); y = a['y'] + d * math.cos(H)
            s.pos = np.array([x, y, L.height(x, y) + 60.0]); s.yaw = a['direction']; s.pitch = -8.0
        else:
            x, y = L.tx * L.T / 2, L.ty * L.T / 2; s.pos = np.array([x, y, L.height(x, y) + 500]); s.yaw = 0.0; s.pitch = -20.0
        s.roll = 0.0

    def set_speed(s, v): s.speed = min(max(v, 1.0), 5000.0)

    # ---- remote control --------------------------------------------------------------------------------------------------------------------
    def poll_cmds(s):
        if time.time() < s.wait_until: return
        p = os.path.join(s.ctl, 'cmd.txt')
        if not os.path.exists(p): return
        try: lines = open(p, encoding='utf-8').read().splitlines(); os.remove(p)
        except OSError: return
        for i, ln in enumerate(lines):
            ln = ln.strip()
            if not ln or ln.startswith('#'): continue
            if ln.split()[0].lower() == 'wait':                                 # keep the rest for later
                s.wait_until = time.time() + float(ln.split()[1]) / 1000
                rest = [x for x in lines[i + 1:] if x.strip()]
                if rest: open(p, 'w').write('\n'.join(rest) + '\n')
                return
            try: s.cmd(ln)
            except Exception as e: s.log(f'cmd {ln!r}: error {e}')

    def cmd(s, ln):
        a = ln.split(); c = a[0].lower(); f = [float(x) for x in a[1:] if s.isnum(x)]
        L = s.L
        if c == 'cam':
            s.pos = np.array(f[0:3]); s.yaw = f[3] if len(f) > 3 else s.yaw; s.pitch = f[4] if len(f) > 4 else s.pitch; s.roll = f[5] if len(f) > 5 else 0.0
        elif c == 'look': s.yaw = f[0]; s.pitch = f[1] if len(f) > 1 else s.pitch; s.roll = f[2] if len(f) > 2 else s.roll
        elif c == 'move': s.pos += np.array((f + [0, 0, 0])[:3])
        elif c == 'fly':
            fw, r, u = axes(s.yaw, s.pitch, s.roll); g = (f + [0, 0, 0])[:3]; s.pos += fw * g[0] + r * g[1] + u * g[2]
        elif c == 'turn': s.yaw = (s.yaw + f[0]) % 360; s.pitch = max(-89.9, min(89.9, s.pitch + (f[1] if len(f) > 1 else 0)))
        elif c == 'agl': s.pos[2] = L.height(s.pos[0], s.pos[1]) + f[0]
        elif c == 'goto':                                                       # numbers or a name, both in positions.txt order
            if len(f) >= 3: v = f; label = 'numbers'
            else:
                pp = os.path.join(L.game, 'postfx', 'positions.txt'); v = None; label = a[1]
                for row in open(pp, encoding='latin1').read().splitlines():
                    r = row.split()
                    if len(r) >= 4 and r[0] == a[1]: v = [float(x) for x in r[1:]]
                if v is None: s.log(f'goto: {a[1]} not in {pp}'); return
                if len(v) >= 12: v = v[6:12]; label += ' (game camera)'            # dump also saved the rendered camera: use it
            s.pos = np.array(v[0:3])
            if len(v) >= 6: s.pitch, s.roll, s.yaw = v[3], v[4], v[5]
            s.log(f'goto {label}: {s.state_str()}')
        elif c == 'shot': s.pending_shot = a[1] if len(a) > 1 else time.strftime('view_%H%M%S')
        elif c == 'state': s.log('state: ' + s.state_str())
        elif c == 'fov': s.fov = f[0]
        elif c == 'size': glfw.set_window_size(s.win, int(f[0]), int(f[1]))
        elif c == 'layer': s.layers[a[1].lower()] = a[2].lower() == 'on'
        elif c == 'wire': s.wire = a[1].lower() == 'on'
        elif c == 'fog': s.fog = a[1].lower() == 'on'
        elif c == 'sun': s.sun = (f[0], f[1])
        elif c == 'speed': s.set_speed(f[0])
        elif c == 'home': s.pos, s.yaw, s.pitch = s.home[0].copy(), s.home[1], s.home[2]; s.roll = 0.0
        elif c == 'quit': s.quit = True
        else: s.log(f'unknown command: {ln}'); return
        if c not in ('state', 'goto', 'shot'): s.log(f'{ln} -> ' + s.state_str())

    @staticmethod
    def isnum(x):
        try: float(x); return True
        except ValueError: return False

    def state_str(s):
        g = s.L.height(s.pos[0], s.pos[1])
        on = ','.join(k for k, v in s.layers.items() if v)
        return (f'pos {s.pos[0]:.1f} {s.pos[1]:.1f} {s.pos[2]:.1f} (ground {g:.1f}, AGL {s.pos[2] - g:.1f}) yaw {s.yaw:.1f} pitch {s.pitch:.1f} '
                f'roll {s.roll:.1f} fov {s.fov:.1f} layers {on}{" wire" if s.wire else ""}{"" if s.fog else " nofog"}')

    # ---- input -----------------------------------------------------------------------------------------------------------------------------
    def on_key(s, win, key, sc, action, mods):
        if action != glfw.PRESS: return
        lay = {glfw.KEY_1: 'terrain', glfw.KEY_2: 'autogen', glfw.KEY_3: 'airports', glfw.KEY_4: 'objects'}
        if key in lay: k = lay[key]; s.layers[k] = not s.layers[k]; s.log(f'layer {k} {"on" if s.layers[k] else "off"}')
        elif key == glfw.KEY_G: s.wire = not s.wire
        elif key == glfw.KEY_F: s.fog = not s.fog
        elif key == glfw.KEY_P: s.pending_shot = time.strftime('view_%H%M%S')
        elif key == glfw.KEY_R: s.pos, s.yaw, s.pitch = s.home[0].copy(), s.home[1], s.home[2]; s.roll = 0.0
        elif key == glfw.KEY_H: s.log(__doc__)
        elif key == glfw.KEY_ESCAPE: s.quit = True

    def handle_input(s, dt):
        w = s.win
        if glfw.get_mouse_button(w, glfw.MOUSE_BUTTON_LEFT) == glfw.PRESS:
            x, y = glfw.get_cursor_pos(w)
            if s.drag:
                s.yaw = (s.yaw + (x - s.drag[0]) * 0.15) % 360; s.pitch = max(-89.9, min(89.9, s.pitch - (y - s.drag[1]) * 0.15))
            s.drag = (x, y)
        else: s.drag = None
        k = lambda key: glfw.get_key(w, key) == glfw.PRESS
        v = s.speed * (5 if k(glfw.KEY_LEFT_SHIFT) or k(glfw.KEY_RIGHT_SHIFT) else 1) * dt
        f, r, u = axes(s.yaw, s.pitch, 0); d = np.zeros(3)
        if k(glfw.KEY_W): d += f
        if k(glfw.KEY_S): d -= f
        if k(glfw.KEY_D): d += r
        if k(glfw.KEY_A): d -= r
        if k(glfw.KEY_E): d[2] += 1
        if k(glfw.KEY_Q): d[2] -= 1
        if d.any(): s.pos += d * v

    # ---- draw ------------------------------------------------------------------------------------------------------------------------------
    def matrices(s, W, H):
        f, r, u = axes(s.yaw, s.pitch, s.roll)
        F, R, U, E = to_gl(f), to_gl(r), to_gl(u), to_gl(s.pos)
        view = np.eye(4); view[0, :3] = R; view[1, :3] = U; view[2, :3] = -F
        view[:3, 3] = -view[:3, :3] @ E
        proj = perspective(s.fov, W / max(H, 1), 0.5, 80000.0)
        return proj @ view, E

    def draw(s):
        W, H = glfw.get_framebuffer_size(s.win)
        gl.glViewport(0, 0, W, H)
        gl.glClearColor(0.75, 0.82, 0.90, 1); gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        vp, E = s.matrices(W, H); fogc = (0.74, 0.80, 0.88)
        gl.glDisable(gl.GL_DEPTH_TEST); gl.glUseProgram(s.sky)
        gl.glUniformMatrix4fv(gl.glGetUniformLocation(s.sky, 'invVP'), 1, True, np.linalg.inv(vp).astype(np.float32))
        gl.glUniform3f(gl.glGetUniformLocation(s.sky, 'fogColor'), *fogc)
        gl.glBindVertexArray(s.skyvao); gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
        gl.glEnable(gl.GL_DEPTH_TEST); gl.glDisable(gl.GL_CULL_FACE)               # mirrored frame + two-sided C3D: no culling
        p = s.prog; gl.glUseProgram(p); U = lambda n: gl.glGetUniformLocation(p, n)
        gl.glUniformMatrix4fv(U('mvp'), 1, True, vp.astype(np.float32)); gl.glUniform3f(U('eye'), *E)
        az, el = map(math.radians, s.sun)
        sun = to_gl(np.array([-math.sin(az) * math.cos(el), math.cos(az) * math.cos(el), math.sin(el)]))
        gl.glUniform3f(U('sun'), *sun); gl.glUniform3f(U('fogColor'), *fogc); gl.glUniform1f(U('fogDist'), s.fogDist if s.fog else 0.0)
        gl.glUniform1i(U('wire'), 0); gl.glUniform1i(U('tex'), 0); gl.glActiveTexture(gl.GL_TEXTURE0)
        for m in s.meshes:
            if not s.layers.get(m.kind, True): continue
            gl.glUniform1i(U('water'), 1 if m.kind == 'terrain' else 0)
            amb, dif = (0.72, 0.38) if m.kind == 'terrain' else (0.55, 0.55)
            gl.glUniform1f(U('ambient'), amb); gl.glUniform1f(U('diffuse'), dif)
            gl.glBindVertexArray(m.vao)
            for (fi, ni, t, col) in m.parts:
                gl.glUniform1i(U('hasTex'), 1 if t else 0); gl.glUniform4f(U('color'), *col)
                if t: gl.glBindTexture(gl.GL_TEXTURE_2D, t)
                gl.glDrawElements(gl.GL_TRIANGLES, ni, gl.GL_UNSIGNED_INT, ctypes.c_void_p(fi * 4))
        if s.wire:
            gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_LINE); gl.glUniform1i(U('wire'), 1); gl.glEnable(gl.GL_POLYGON_OFFSET_LINE)
            gl.glPolygonOffset(-1, -1)
            for m in s.meshes:
                if not s.layers.get(m.kind, True): continue
                gl.glBindVertexArray(m.vao)
                for (fi, ni, t, col) in m.parts: gl.glDrawElements(gl.GL_TRIANGLES, ni, gl.GL_UNSIGNED_INT, ctypes.c_void_p(fi * 4))
            gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL); gl.glDisable(gl.GL_POLYGON_OFFSET_LINE)
        if s.pending_shot:
            px = gl.glReadPixels(0, 0, W, H, gl.GL_RGB, gl.GL_UNSIGNED_BYTE)
            px = px.tobytes() if hasattr(px, 'tobytes') else bytes(px)
            img = Image.frombytes('RGB', (W, H), px).transpose(Image.FLIP_TOP_BOTTOM)
            out = os.path.join(s.ctl, 'shots', s.pending_shot + '.png'); img.save(out)
            s.log(f'shot {out} ({W}x{H}): {s.state_str()}'); s.pending_shot = None

    def run(s):
        t = time.time()
        while not glfw.window_should_close(s.win) and not s.quit:
            now = time.time(); dt = min(now - t, 0.1); t = now
            glfw.poll_events(); s.handle_input(dt)
            if now - s.last_cmd > 0.1: s.last_cmd = now; s.poll_cmds()
            s.draw(); glfw.swap_buffers(s.win)
            s.frames += 1
            if now - s.fps_t > 1:
                s.fps = s.frames / (now - s.fps_t); s.frames = 0; s.fps_t = now
                g = s.L.height(s.pos[0], s.pos[1])
                glfw.set_window_title(s.win, f'c3view {s.L.name} | x {s.pos[0]:.0f} y {s.pos[1]:.0f} z {s.pos[2]:.0f} (AGL {s.pos[2] - g:.0f}) '
                                             f'yaw {s.yaw:.0f} pitch {s.pitch:.0f} | {s.speed:.0f} m/s | {s.fps:.0f} fps')
        s.log('quit'); glfw.terminate()

def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if not args: raise SystemExit(__doc__)
    def opt(n, d=None): return sys.argv[sys.argv.index(n) + 1] if n in sys.argv else d
    L = Landscape(args[0])
    ctl = opt('--ctl', os.path.join(L.game, 'postfx', 'viewer'))
    size = tuple(int(v) for v in opt('--size', '1280x720').split('x'))
    Viewer(L, ctl, size, '--hidden' in sys.argv).run()

if __name__ == '__main__':
    main()
