//! Source tiles: GLB reading (geometry only, or with uv + texture) and the projection into the game's world frame
//! (x = E0 - easting, y = northing - N0, z = ellipsoidal height + height_offset).
//!
//! Projection: instead of ECEF -> lat/lon (iterative) -> UTM (series) for every vertex, each source tile gets a quadratic polynomial in its
//! local glTF coordinates, fitted on 75 exactly projected points of its bounding box and checked on 32 others. A tile of a few hundred
//! metres is reproduced to well below a millimetre; if the check fails (very large tiles) the exact formulas are used for that tile.
use crate::geo::*;
use crate::plan::Plan;
use serde_json::Value;

/// RGBA8, row 0 = top
pub struct Tex { pub w: usize, pub h: usize, pub d: Vec<u8> }
/// one primitive in world coordinates; hc = position in the source tile's lat/lon rectangle (only when the tile has a hole mask)
pub struct Prim { pub p: Vec<[f64; 3]>, pub hc: Vec<[f32; 2]>, pub uv: Vec<[f32; 2]>, pub tri: Vec<[u32; 3]>, pub tex: Option<Tex> }

struct Raw { pos: Vec<[f32; 3]>, uv: Vec<[f32; 2]>, tri: Vec<[u32; 3]>, tex: Option<Tex> }

fn u32le(d: &[u8], o: usize) -> usize { u32::from_le_bytes([d[o], d[o + 1], d[o + 2], d[o + 3]]) as usize }
fn f32at(d: &[u8], o: usize) -> f32 { f32::from_le_bytes([d[o], d[o + 1], d[o + 2], d[o + 3]]) }

/// PNG straight to RGBA (png crate with expansion, no extra conversion pass); anything else through the image crate
fn decode_image(b: &[u8]) -> Option<Tex> {
    if b.starts_with(b"\x89PNG") {
        let mut dec = png::Decoder::new(std::io::Cursor::new(b));
        dec.set_transformations(png::Transformations::EXPAND | png::Transformations::STRIP_16);
        dec.ignore_checksums(true);
        if let Ok(mut r) = dec.read_info() {
            if let Some(n) = r.output_buffer_size() {
                let mut d = vec![0u8; n];
                if let Ok(info) = r.next_frame(&mut d) {
                    if info.bit_depth == png::BitDepth::Eight {
                        let px = (info.width * info.height) as usize;
                        let o = match info.color_type {
                            png::ColorType::Rgba => { d.truncate(px * 4); Some(d) }
                            png::ColorType::Rgb => { let mut o = Vec::with_capacity(px * 4); for c in d[..px * 3].chunks_exact(3) { o.extend_from_slice(&[c[0], c[1], c[2], 255]); } Some(o) }
                            _ => None,
                        };
                        if let Some(o) = o { return Some(Tex { w: info.width as usize, h: info.height as usize, d: o }); }
                    }
                }
            }
        }
    }
    let im = image::load_from_memory(b).ok()?.to_rgba8();
    Some(Tex { w: im.width() as usize, h: im.height() as usize, d: im.into_raw() })
}

/// glTF reader: float positions, uv, indexed triangle lists, one embedded PNG/JPEG per primitive (decoded only when full)
fn parse(path: &str, full: bool) -> Vec<Raw> {
    let d = match std::fs::read(path) { Ok(d) => d, Err(_) => return vec![] };
    if d.len() < 28 || &d[0..4] != b"glTF" { return vec![]; }
    let jl = u32le(&d, 12);
    if 20 + jl > d.len() { return vec![]; }
    let j: Value = match serde_json::from_slice(&d[20..20 + jl]) { Ok(v) => v, Err(_) => return vec![] };
    let bo = 20 + jl + 8;
    let acc = |i: usize| -> Option<(usize, usize, usize, u64)> {
        let a = &j["accessors"][i];
        let bv = &j["bufferViews"][a["bufferView"].as_u64()? as usize];
        let n = match a["type"].as_str()? { "SCALAR" => 1, "VEC2" => 2, "VEC3" => 3, _ => return None };
        let off = bo + bv["byteOffset"].as_u64().unwrap_or(0) as usize + a["byteOffset"].as_u64().unwrap_or(0) as usize;
        Some((off, a["count"].as_u64()? as usize, n, a["componentType"].as_u64()?))
    };
    let mut out = vec![];
    for mesh in j["meshes"].as_array().map(|v| v.as_slice()).unwrap_or(&[]) {
        for pr in mesh["primitives"].as_array().map(|v| v.as_slice()).unwrap_or(&[]) {
            if pr["mode"].as_u64().unwrap_or(4) != 4 || pr["indices"].is_null() { continue; }
            let Some((po, pc, pn, pt)) = pr["attributes"]["POSITION"].as_u64().and_then(|i| acc(i as usize)) else { continue };
            if pn != 3 || pt != 5126 || po + pc * 12 > d.len() { continue; }
            let pos: Vec<[f32; 3]> = (0..pc).map(|v| [f32at(&d, po + v * 12), f32at(&d, po + v * 12 + 4), f32at(&d, po + v * 12 + 8)]).collect();
            let Some((io, ic, _, it)) = pr["indices"].as_u64().and_then(|i| acc(i as usize)) else { continue };
            let isz = match it { 5123 => 2, 5125 => 4, _ => 1 };
            if io + ic * isz > d.len() { continue; }
            let ix: Vec<u32> = match it {
                5123 => d[io..io + ic * 2].chunks_exact(2).map(|c| u16::from_le_bytes([c[0], c[1]]) as u32).collect(),
                5125 => d[io..io + ic * 4].chunks_exact(4).map(|c| u32::from_le_bytes([c[0], c[1], c[2], c[3]])).collect(),
                _ => d[io..io + ic].iter().map(|&b| b as u32).collect(),
            };
            let tri: Vec<[u32; 3]> = ix.chunks_exact(3).filter(|t| (t[0] as usize) < pc && (t[1] as usize) < pc && (t[2] as usize) < pc).map(|t| [t[0], t[1], t[2]]).collect();
            let mut uv = vec![];
            let mut tex = None;
            if full {
                uv = vec![[0f32; 2]; pc];
                if let Some((uo, uc, un, ut)) = pr["attributes"]["TEXCOORD_0"].as_u64().and_then(|i| acc(i as usize)) {
                    if un == 2 && ut == 5126 && uc == pc && uo + uc * 8 <= d.len() { for v in 0..pc { uv[v] = [f32at(&d, uo + v * 8), f32at(&d, uo + v * 8 + 4)]; } }
                }
                if let Some(ti) = j["materials"][pr["material"].as_u64().unwrap_or(0) as usize]["pbrMetallicRoughness"]["baseColorTexture"]["index"].as_u64() {
                    if let Some(src) = j["textures"][ti as usize]["source"].as_u64() {
                        let bv = &j["bufferViews"][j["images"][src as usize]["bufferView"].as_u64().unwrap_or(0) as usize];
                        let o = bo + bv["byteOffset"].as_u64().unwrap_or(0) as usize;
                        let l = bv["byteLength"].as_u64().unwrap_or(0) as usize;
                        if l > 0 && o + l <= d.len() {
                            tex = crate::prof("src.image(cpu)", || decode_image(&d[o..o + l]));
                        }
                    }
                }
            }
            out.push(Raw { pos, uv, tri, tex });
        }
    }
    out
}

fn to_ecef(m: &[f64; 16], p: [f64; 3]) -> [f64; 3] {
    let l = [p[0], -p[2], p[1]];   // glTF is Y-up
    [m[0] * l[0] + m[1] * l[1] + m[2] * l[2] + m[3], m[4] * l[0] + m[5] * l[1] + m[6] * l[2] + m[7], m[8] * l[0] + m[9] * l[1] + m[10] * l[2] + m[11]]
}

/// ECEF positions of all vertices of a GLB (used by the plan when an export has no location.json)
pub fn glb_positions_ecef(path: &str, m: &[f64; 16]) -> Vec<[f64; 3]> {
    parse(path, false).iter().flat_map(|r| r.pos.iter().map(|p| to_ecef(m, [p[0] as f64, p[1] as f64, p[2] as f64])).collect::<Vec<_>>()).collect()
}

/// local glTF coordinates -> (x, y, z, hu, hv)
struct Proj<'a> { plan: &'a Plan, k: usize, m: [f64; 16], c: [f64; 3], s: [f64; 3], coef: Option<Box<[[f64; 10]; 5]>> }
impl<'a> Proj<'a> {
    fn exact(&self, p: [f64; 3]) -> [f64; 5] {
        let (lat, lon, h) = ecef_to_lla(to_ecef(&self.m, p));
        let (e, n) = utm_forward(lat, lon, self.plan.zone, self.plan.south);
        let t = &self.plan.tiles[self.k];
        [self.plan.east0 - e, n - self.plan.north0, h + self.plan.height_offset, (lon - t.west) / (t.east - t.west), (lat - t.south) / (t.north - t.south)]
    }
    fn basis(&self, p: [f64; 3]) -> [f64; 10] {
        let q = [(p[0] - self.c[0]) / self.s[0], (p[1] - self.c[1]) / self.s[1], (p[2] - self.c[2]) / self.s[2]];
        [1.0, q[0], q[1], q[2], q[0] * q[0], q[1] * q[1], q[2] * q[2], q[0] * q[1], q[0] * q[2], q[1] * q[2]]
    }
    fn eval(&self, p: [f64; 3]) -> [f64; 5] {
        match &self.coef {
            None => self.exact(p),
            Some(cf) => { let b = self.basis(p); let mut o = [0.0; 5]; for k in 0..5 { o[k] = (0..10).map(|i| cf[k][i] * b[i]).sum(); } o }
        }
    }
    fn new(plan: &'a Plan, k: usize, raws: &[Raw]) -> Proj<'a> {
        let (mut lo, mut hi) = ([f64::MAX; 3], [f64::MIN; 3]);
        for r in raws { for p in &r.pos { for a in 0..3 { lo[a] = lo[a].min(p[a] as f64); hi[a] = hi[a].max(p[a] as f64); } } }
        let mut pr = Proj { plan, k, m: plan.tiles[k].mat(), c: [0.0; 3], s: [1.0; 3], coef: None };
        if lo[0] > hi[0] { return pr; }
        for a in 0..3 { pr.c[a] = (lo[a] + hi[a]) / 2.0; pr.s[a] = ((hi[a] - lo[a]) / 2.0).max(1.0); }
        let pt = |fx: f64, fy: f64, fz: f64| [pr.c[0] + fx * pr.s[0], pr.c[1] + fy * pr.s[1], pr.c[2] + fz * pr.s[2]];
        let lin = |n: usize| (0..n).map(move |i| -1.0 + 2.0 * i as f64 / (n - 1) as f64);
        let mut fitp = vec![];
        for a in lin(5) { for b in lin(5) { for c in lin(3) { fitp.push(pt(a, b, c)); } } }
        let mut ata = [[0.0f64; 10]; 10]; let mut atb = [[0.0f64; 10]; 5];
        for p in &fitp {
            let b = pr.basis(*p); let y = pr.exact(*p);
            for i in 0..10 { for j in 0..10 { ata[i][j] += b[i] * b[j]; } for k in 0..5 { atb[k][i] += b[i] * y[k]; } }
        }
        let Some(sol) = solve10(ata, atb) else { return pr };
        pr.coef = Some(Box::new(sol));
        // check on other points; tolerance 0.5 mm (and the equivalent for the rectangle coordinates)
        let t = &plan.tiles[k];
        let tol_h = 0.5e-3 / ((t.e1 - t.e0).max(t.n1 - t.n0)).max(1.0);
        let lin2 = |n: usize| (0..n).map(move |i| -0.9 + 1.8 * (i as f64 + 0.37) / n as f64);
        for a in lin2(4) { for b in lin2(4) { for c in lin2(2) {
            let p = pt(a, b, c); let (f, e) = (pr.eval(p), pr.exact(p));
            if (0..3).any(|i| (f[i] - e[i]).abs() > 0.5e-3) || (3..5).any(|i| (f[i] - e[i]).abs() > tol_h) { pr.coef = None; return pr; }
        } } }
        pr
    }
}

/// Gaussian elimination with partial pivoting for 5 right-hand sides
fn solve10(mut a: [[f64; 10]; 10], b: [[f64; 10]; 5]) -> Option<[[f64; 10]; 5]> {
    let mut x = [[0.0f64; 10]; 5];
    let mut r: Vec<[f64; 5]> = (0..10).map(|i| [b[0][i], b[1][i], b[2][i], b[3][i], b[4][i]]).collect();
    for col in 0..10 {
        let piv = (col..10).max_by(|&i, &j| a[i][col].abs().partial_cmp(&a[j][col].abs()).unwrap())?;
        if a[piv][col].abs() < 1e-12 { return None; }
        a.swap(col, piv); r.swap(col, piv);
        for i in col + 1..10 {
            let f = a[i][col] / a[col][col];
            for j in col..10 { a[i][j] -= f * a[col][j]; }
            for k in 0..5 { r[i][k] -= f * r[col][k]; }
        }
    }
    for i in (0..10).rev() {
        for k in 0..5 {
            let mut s = r[i][k];
            for j in i + 1..10 { s -= a[i][j] * x[k][j]; }
            x[k][i] = s / a[i][i];
        }
    }
    Some(x)
}

/// read source tile k and project it. full = also uv + decoded texture
pub fn load(plan: &Plan, k: usize, full: bool) -> Vec<Prim> {
    let raws = crate::prof("src.parse(cpu)", || parse(&plan.tiles[k].glb, full));
    if raws.is_empty() { return vec![]; }
    let pr = crate::prof("src.fit(cpu)", || Proj::new(plan, k, &raws));
    let mask = true;   // hole coordinates for every vertex: the distant autogen sets have their own masks
    raws.into_iter().map(|r| {
        let mut p = Vec::with_capacity(r.pos.len()); let mut hc = Vec::with_capacity(if mask { r.pos.len() } else { 0 });
        for v in &r.pos {
            let o = pr.eval([v[0] as f64, v[1] as f64, v[2] as f64]);
            p.push([o[0], o[1], o[2]]);
            if mask { hc.push([o[3] as f32, o[4] as f32]); }
        }
        Prim { p, hc, uv: r.uv, tri: r.tri, tex: r.tex }
    }).collect()
}

