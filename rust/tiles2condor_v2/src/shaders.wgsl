// tiles2condor_v2 GPU kernels. Pixel frame of a Condor tile: x from the west edge, y from the north edge, in pixels of the tile texture.

// ---------------------------------------------------------------- raster: top-down orthographic, highest surface wins (depth Greater)
struct U { s: f32, zlo: f32, zinv: f32, pad: f32 };
@group(0) @binding(0) var<uniform> u: U;
@group(0) @binding(1) var<storage, read> mask: array<u32>;
@group(0) @binding(2) var<storage, read> vtx: array<u32>;   // 9 words per vertex: x, y, z, u, v, hu, hv, mask offset, mask k
@group(0) @binding(3) var<storage, read> ind: array<u32>;   // triangle list
@group(1) @binding(0) var tex: texture_2d<f32>;

// Vertex pulling: draw(n) without vertex buffers; each invocation reads its own vertex and the other two corners of its triangle
// (a, b), used to grow the triangle by EPS pixels.
struct VI { p: vec3<f32>, uv: vec2<f32>, hc: vec2<f32>, m: vec2<u32>, a: vec3<f32>, b: vec3<f32> };
fn pos(i: u32) -> vec3<f32> { return vec3<f32>(bitcast<f32>(vtx[i * 9u]), bitcast<f32>(vtx[i * 9u + 1u]), bitcast<f32>(vtx[i * 9u + 2u])); }
fn fetch(vid: u32) -> VI {   // vertex pulling: own vertex + the two others of its triangle
    let t = vid / 3u; let k = vid % 3u;
    let i = ind[vid];
    var o: VI;
    o.p = pos(i);
    o.uv = vec2<f32>(bitcast<f32>(vtx[i * 9u + 3u]), bitcast<f32>(vtx[i * 9u + 4u]));
    o.hc = vec2<f32>(bitcast<f32>(vtx[i * 9u + 5u]), bitcast<f32>(vtx[i * 9u + 6u]));
    o.m = vec2<u32>(vtx[i * 9u + 7u], vtx[i * 9u + 8u]);
    o.a = pos(ind[t * 3u + (k + 1u) % 3u]);
    o.b = pos(ind[t * 3u + (k + 2u) % 3u]);
    return o;
}
struct VO {
    @builtin(position) pos: vec4<f32>,
    @location(0) uv: vec2<f32>,
    @location(1) hc: vec2<f32>,
    @location(2) @interpolate(flat) m: vec2<u32>,
    @location(3) z: f32,
};

// Every triangle is grown by EPS pixels (edges moved outwards, corners on the miter, height extended along the triangle's plane).
// The GPU snaps corners to 1/256 px, which opens hairline cracks where meshes have T-junctions; a pixel seen through such a crack
// shows the ground under a roof and spoils the lowest-surface statistics. The CPU raster (and v1) close them with an edge tolerance.
@vertex fn vs(@builtin(vertex_index) vid: u32) -> VO {
    let i = fetch(vid);
    var o: VO;
    var p = i.p;
    let EPS = u.pad;
    let da = i.a.xy - p.xy;
    let db = i.b.xy - p.xy;
    let cr = da.x * db.y - da.y * db.x;
    if (abs(cr) > 1e-6) {
        let sg = sign(cr);
        let e1 = normalize(da);
        let e2 = normalize(-db);
        let n1 = vec2<f32>(e1.y, -e1.x) * sg;                          // outward normals of the edges p->a and b->p
        let n2 = vec2<f32>(e2.y, -e2.x) * sg;
        let off = EPS * (n1 + n2) / max(1.0 + dot(n1, n2), 0.25);
        let g = vec2<f32>((i.a.z - p.z) * db.y - (i.b.z - p.z) * da.y, da.x * (i.b.z - p.z) - db.x * (i.a.z - p.z)) / cr;
        let zr = vec2<f32>(min(p.z, min(i.a.z, i.b.z)), max(p.z, max(i.a.z, i.b.z)));   // steep walls: stay within the triangle's heights
        p = vec3<f32>(p.xy + off, clamp(p.z + dot(g, off), zr.x, zr.y));
    }
    let d = clamp((p.z - u.zlo) * u.zinv, 1e-6, 1.0);
    o.pos = vec4<f32>(p.x / u.s * 2.0 - 1.0, 1.0 - p.y / u.s * 2.0, d, 1.0);
    o.uv = i.uv; o.hc = i.hc; o.m = i.m; o.z = p.z;
    return o;
}

// hole mask of the source tile: K x K cells over its lat/lon rectangle, [row from south][column from west]
fn hidden(hc: vec2<f32>, m: vec2<u32>) -> bool {
    if (m.x == 0xffffffffu) { return false; }
    let k = i32(m.y);
    let r = clamp(i32(floor(hc.y * f32(k))), 0, k - 1);
    let c = clamp(i32(floor(hc.x * f32(k))), 0, k - 1);
    let b = m.x + u32(r * k + c);
    return ((mask[b >> 5u] >> (b & 31u)) & 1u) == 1u;
}

struct FO { @location(0) c: vec4<f32>, @location(1) z: f32 };
@fragment fn fs_color(i: VO) -> FO {
    if (hidden(i.hc, i.m)) { discard; }
    let dim = vec2<i32>(textureDimensions(tex));
    let t = clamp(vec2<i32>(floor(i.uv * vec2<f32>(dim))), vec2<i32>(0, 0), dim - vec2<i32>(1, 1));   // nearest texel
    var o: FO;
    o.c = vec4<f32>(textureLoad(tex, t, 0).rgb, 1.0);
    o.z = i.z;
    return o;
}
@fragment fn fs_height(i: VO) -> @location(0) f32 {
    if (hidden(i.hc, i.m)) { discard; }
    return i.z;
}

// ---------------------------------------------------------------- statistics per 5.625 m cell: lowest height, mean colour
@group(0) @binding(0) var st_c: texture_2d<f32>;
@group(0) @binding(1) var st_h: texture_2d<f32>;
@group(0) @binding(2) var<storage, read_write> st_out: array<vec4<f32>>;
@group(0) @binding(3) var<uniform> st_u: vec4<u32>;   // pixels per cell, cells per side, colour flag
@group(0) @binding(4) var<storage, read_write> st_cnt: array<atomic<u32>>;   // [0] = pixels with data

@compute @workgroup_size(8, 8) fn stats(@builtin(global_invocation_id) g: vec3<u32>) {
    let n = st_u.y;
    if (g.x >= n || g.y >= n) { return; }
    let f = st_u.x;
    var m = 1e30;
    var cnt = 0u;
    var s = vec3<u32>(0u, 0u, 0u);
    for (var y = 0u; y < f; y++) {
        for (var x = 0u; x < f; x++) {
            let p = vec2<i32>(i32(g.x * f + x), i32(g.y * f + y));
            let z = textureLoad(st_h, p, 0).r;
            if (z > -1e8) {
                cnt += 1u;
                m = min(m, z);
                if (st_u.z == 1u) { s += vec3<u32>(round(textureLoad(st_c, p, 0).rgb * 255.0)); }
            }
        }
    }
    var o = vec4<f32>(1e30, 0.0, 0.0, 0.0);
    if (cnt > 0u) { o = vec4<f32>(m, vec3<f32>(s) / f32(cnt)); atomicAdd(&st_cnt[0], cnt); }
    st_out[g.y * n + g.x] = o;
}

// ---------------------------------------------------------------- mip level: box average, integer truncation
@group(0) @binding(0) var mp_src: texture_2d<f32>;
@group(0) @binding(1) var mp_dst: texture_storage_2d<rgba8unorm, write>;

@compute @workgroup_size(8, 8) fn mip(@builtin(global_invocation_id) g: vec3<u32>) {
    let d = textureDimensions(mp_dst);
    if (g.x >= d.x || g.y >= d.y) { return; }
    let sd = textureDimensions(mp_src);
    let fx = sd.x / d.x;
    let fy = sd.y / d.y;
    var s = vec4<u32>(0u, 0u, 0u, 0u);
    for (var yy = 0u; yy < fy; yy++) {
        for (var xx = 0u; xx < fx; xx++) {
            s += vec4<u32>(round(textureLoad(mp_src, vec2<u32>(g.x * fx + xx, g.y * fy + yy), 0) * 255.0));
        }
    }
    textureStore(mp_dst, g.xy, vec4<f32>(s / (fx * fy)) / 255.0);
}

// ---------------------------------------------------------------- BC1: bounding-box endpoints, projection on the axis (same integers as fmt.rs)
@group(0) @binding(0) var bc_src: texture_2d<f32>;
@group(0) @binding(1) var<storage, read_write> bc_out: array<u32>;
@group(0) @binding(2) var<uniform> bc_u: vec4<u32>;   // word offset, blocks per row, block rows

fn to565(c: vec3<i32>) -> u32 {
    return u32((((c.x * 31 + 127) / 255) << 11u) | (((c.y * 63 + 127) / 255) << 5u) | ((c.z * 31 + 127) / 255));
}
fn from565(v: u32) -> vec3<i32> {
    return vec3<i32>(i32(((v >> 11u) & 31u) * 255u / 31u), i32(((v >> 5u) & 63u) * 255u / 63u), i32((v & 31u) * 255u / 31u));
}

@compute @workgroup_size(8, 8) fn bc1(@builtin(global_invocation_id) g: vec3<u32>) {
    if (g.x >= bc_u.y || g.y >= bc_u.z) { return; }
    let d = textureDimensions(bc_src);
    var px: array<vec3<i32>, 16>;
    var mx = vec3<i32>(0, 0, 0);
    var mn = vec3<i32>(255, 255, 255);
    for (var k = 0u; k < 16u; k++) {
        let x = min(g.x * 4u + (k % 4u), d.x - 1u);
        let y = min(g.y * 4u + (k / 4u), d.y - 1u);
        let c = vec3<i32>(round(textureLoad(bc_src, vec2<u32>(x, y), 0).rgb * 255.0));
        px[k] = c;
        mx = max(mx, c);
        mn = min(mn, c);
    }
    let a = to565(mx);
    let b = to565(mn);
    let c0 = max(a, b);
    let c1 = min(a, b);
    let p0 = from565(c0);
    let p1 = from565(c1);
    let ax = p0 - p1;
    let den = max(dot(ax, ax), 1);
    var bits = 0u;
    var lut = array<u32, 4>(1u, 3u, 2u, 0u);
    if (c0 != c1) {
        for (var k = 0u; k < 16u; k++) {
            let d6 = 6 * dot(px[k] - p1, ax);   // t = clamp(floor((6 dot + den) / (2 den)), 0, 3), as comparisons (same as fmt.rs)
            let t = u32(d6 >= den) + u32(d6 >= 3 * den) + u32(d6 >= 5 * den);
            bits |= lut[t] << (2u * k);
        }
    }
    let o = bc_u.x + (g.y * bc_u.y + g.x) * 2u;
    bc_out[o] = c0 | (c1 << 16u);
    bc_out[o + 1u] = bits;
}
