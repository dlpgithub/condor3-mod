//! Minimal GLB reader for the tiles of a 3D Tiles export: float positions, uv, indexed triangles, one embedded PNG/JPEG per primitive.
use serde_json::Value;

pub struct Tex { pub w: usize, pub h: usize, pub d: Vec<u8> }   // RGB8, row 0 = top
pub struct Prim { pub ecef: Vec<[f64; 3]>, pub uv: Vec<[f32; 2]>, pub tri: Vec<[u32; 3]>, pub tex: Option<Tex> }

fn u32le(d: &[u8], o: usize) -> usize { u32::from_le_bytes([d[o], d[o + 1], d[o + 2], d[o + 3]]) as usize }

/// m = accumulated tileset transform, row-major 4x4. glTF is Y-up: ecef = m * (x, -z, y, 1).
pub fn load_glb(path: &str, m: &[f64; 16]) -> Vec<Prim> {
    let d = match std::fs::read(path) { Ok(d) => d, Err(_) => return vec![] };
    if d.len() < 28 || &d[0..4] != b"glTF" { return vec![]; }
    let jl = u32le(&d, 12);
    let j: Value = match serde_json::from_slice(&d[20..20 + jl]) { Ok(v) => v, Err(_) => return vec![] };
    let bo = 20 + jl + 8;
    let acc = |i: usize| -> Option<(usize, usize, usize, u64)> {
        let a = &j["accessors"][i];
        let bv = &j["bufferViews"][a["bufferView"].as_u64()? as usize];
        let n = match a["type"].as_str()? { "SCALAR" => 1, "VEC2" => 2, "VEC3" => 3, _ => return None };
        let off = bo + bv["byteOffset"].as_u64().unwrap_or(0) as usize + a["byteOffset"].as_u64().unwrap_or(0) as usize;
        Some((off, a["count"].as_u64()? as usize, n, a["componentType"].as_u64()?))
    };
    let f32at = |o: usize| f32::from_le_bytes([d[o], d[o + 1], d[o + 2], d[o + 3]]);
    let mut out = vec![];
    for mesh in j["meshes"].as_array().map(|v| v.as_slice()).unwrap_or(&[]) {
        for pr in mesh["primitives"].as_array().map(|v| v.as_slice()).unwrap_or(&[]) {
            if pr["mode"].as_u64().unwrap_or(4) != 4 || pr["indices"].is_null() { continue; }
            let Some((po, pc, pn, pt)) = pr["attributes"]["POSITION"].as_u64().and_then(|i| acc(i as usize)) else { continue };
            if pn != 3 || pt != 5126 || po + pc * 12 > d.len() { continue; }
            let mut ecef = Vec::with_capacity(pc);
            for v in 0..pc {
                let (x, y, z) = (f32at(po + v * 12) as f64, f32at(po + v * 12 + 4) as f64, f32at(po + v * 12 + 8) as f64);
                let l = [x, -z, y];
                ecef.push([m[0] * l[0] + m[1] * l[1] + m[2] * l[2] + m[3], m[4] * l[0] + m[5] * l[1] + m[6] * l[2] + m[7], m[8] * l[0] + m[9] * l[1] + m[10] * l[2] + m[11]]);
            }
            let mut uv = vec![[0f32; 2]; pc];
            if let Some((uo, uc, un, ut)) = pr["attributes"]["TEXCOORD_0"].as_u64().and_then(|i| acc(i as usize)) {
                if un == 2 && ut == 5126 && uc == pc && uo + uc * 8 <= d.len() {
                    for v in 0..pc { uv[v] = [f32at(uo + v * 8), f32at(uo + v * 8 + 4)]; }
                }
            }
            let Some((io, ic, _, it)) = pr["indices"].as_u64().and_then(|i| acc(i as usize)) else { continue };
            let idx = |k: usize| -> u32 {
                match it { 5123 => u16::from_le_bytes([d[io + k * 2], d[io + k * 2 + 1]]) as u32, 5125 => u32le(&d, io + k * 4) as u32, _ => d[io + k] as u32 }
            };
            let isz = match it { 5123 => 2, 5125 => 4, _ => 1 };
            if io + ic * isz > d.len() { continue; }
            let mut tri = Vec::with_capacity(ic / 3);
            for t in 0..ic / 3 {
                let (a, b, c) = (idx(t * 3), idx(t * 3 + 1), idx(t * 3 + 2));
                if (a as usize) < pc && (b as usize) < pc && (c as usize) < pc { tri.push([a, b, c]); }
            }
            let mut tex = None;
            if let Some(ti) = j["materials"][pr["material"].as_u64().unwrap_or(0) as usize]["pbrMetallicRoughness"]["baseColorTexture"]["index"].as_u64() {
                if let Some(src) = j["textures"][ti as usize]["source"].as_u64() {
                    let bv = &j["bufferViews"][j["images"][src as usize]["bufferView"].as_u64().unwrap_or(0) as usize];
                    let o = bo + bv["byteOffset"].as_u64().unwrap_or(0) as usize;
                    let l = bv["byteLength"].as_u64().unwrap_or(0) as usize;
                    if l > 0 && o + l <= d.len() {
                        if let Ok(im) = image::load_from_memory(&d[o..o + l]) {
                            let im = im.to_rgb8();
                            tex = Some(Tex { w: im.width() as usize, h: im.height() as usize, d: im.into_raw() });
                        }
                    }
                }
            }
            out.push(Prim { ecef, uv, tri, tex });
        }
    }
    out
}
