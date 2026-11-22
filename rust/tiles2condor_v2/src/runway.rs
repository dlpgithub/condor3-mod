//! Custom runway surface for an airport ("runway_look": "custom") instead of the photo: a high-resolution runway with ICAO-style
//! markings, made from the textures in the runway texture folder (assets/runway: asphalt, touchdown, patches, edge, concrete,
//! grass, paint).
//!
//! How the game draws airport surfaces (FUN_006e0d04 / FUN_006e2aec, shader PS_AirportSurface_SampleShadowMap): of the G file only
//! the FIRST part named "Asphalt", "AsphaltPaint", "AsphaltPaintYellow", "GrassPaint", "Gravel" and "Grass3D" is drawn, each with
//!     colour = part texture (u along the runway, v across, CLAMP) x (0.5 + detail) x (0.5 + big detail) x light
//! where detail = Airports/Textures/<airport>/Asphalt.dds (AsphaltPaint.dds for paint) repeating every 2 m and big detail a
//! game texture repeating every 20 m. The part named "Asphalt" also makes the wheels roll on asphalt.
//! So a custom runway is: one "Asphalt" part over the runway whose texture holds the whole runway at ~0.1-0.25 m per pixel
//! (asphalt, rubber of the touchdown zones, repairs, worn edges; baked from the 10 m textures), a per-airport Asphalt.dds with the
//! fine grain (2 m, grey around 0.5), and one "AsphaltPaint" part with the markings as exact rectangles (sharp at any distance).
//! Grass runways: the same with mown grass (stripes, worn wheel tracks in the middle), drawn as the stock "GrassPaint" part.
//! The airport's Grass3D cells are left out under the runway, and the runway surface lies a few centimetres above the highest
//! terrain point around each vertex, so the photo cannot show through anywhere.
use crate::fmt::{dxt_cpu, write_dds, Obj};
use crate::grid::Terrain;
use crate::log;
use crate::plan::Airfield;
use image::{imageops::FilterType, RgbImage};
use serde_json::Value;
use std::path::PathBuf;

const REPEAT: f64 = 10.0;           // metres per repeat of the surface textures as delivered (10 m), paint 2 m
const CELL: f64 = 5.0;              // mesh cell (follows the terrain)
const Z_SURF: f32 = 0.15;           // metres above the rendered terrain (the highest point around each vertex)
const Z_PAINT: f32 = 0.22;
const Z_STEP: f32 = 0.04;           // extra per airport, so crossing runways do not fight over the same depth
const EDGE_OUT: f64 = 1.0;          // the custom surface reaches this far beyond the runway edge (worn edge into the grass)

/// folder with the runway textures: config "runway_textures", else assets/runway next to the program (bin/../assets/runway), or in
/// the current folder
pub fn texture_dir(cfg: &Value) -> Option<PathBuf> {
    let mut c: Vec<PathBuf> = vec![];
    if let Some(s) = cfg["runway_textures"].as_str() { c.push(PathBuf::from(s)); }
    if let Ok(exe) = std::env::current_exe() {
        let exe = exe.canonicalize().unwrap_or(exe);
        if let Some(d) = exe.parent() { c.push(d.join("../assets/runway")); c.push(d.join("assets/runway")); c.push(d.join("../../../assets/runway")); }
    }
    c.push(PathBuf::from("assets/runway"));
    c.into_iter().find(|d| ["jpg", "jpeg", "png"].iter().any(|e| d.join(format!("asphalt_base.{}", e)).exists()))
}

/// the textures of the folder, loaded on first use (full resolution)
pub struct RunwayTextures { dir: PathBuf, cache: std::cell::RefCell<std::collections::HashMap<String, Option<RgbImage>>> }
pub fn prepare(cfg: &Value) -> Option<RunwayTextures> {
    let dir = texture_dir(cfg)?;
    log(&format!("runway textures from {}", dir.display()));
    Some(RunwayTextures { dir, cache: Default::default() })
}
impl RunwayTextures {
    fn get(&self, name: &str) -> Option<RgbImage> {
        self.cache.borrow_mut().entry(name.to_string()).or_insert_with(|| {
            let p = ["jpg", "jpeg", "png"].iter().map(|e| self.dir.join(format!("{}.{}", name, e))).find(|p| p.exists())?;
            image::open(&p).ok().map(|i| i.to_rgb8())
        }).clone()
    }
}

/// fades a texture into a copy shifted by half a repeat towards the borders, so it repeats without a visible seam
fn seamless(im: &RgbImage, x: bool, y: bool) -> RgbImage {
    let (w, h) = im.dimensions();
    let wt = |i: u32, n: u32| { let t = (i as f32 + 0.5) / n as f32; ((t.min(1.0 - t) * 2.0) / 0.35).clamp(0.0, 1.0) };
    RgbImage::from_fn(w, h, |c, r| {
        let k = if x { wt(c, w) } else { 1.0 } * if y { wt(r, h) } else { 1.0 };
        let a = im.get_pixel(c, r); let b = im.get_pixel(if x { (c + w / 2) % w } else { c }, if y { (r + h / 2) % h } else { r });
        image::Rgb([0, 1, 2].map(|i| (k * a[i] as f32 + (1.0 - k) * b[i] as f32).round() as u8))
    })
}

/// a repeating texture prefiltered for sampling at `pa` x `pc` metres per pixel (along x across); one repeat = `rep_a` x `rep_c` m.
/// The texture's own x axis runs across the runway, its y axis along it (the prompts asked for marks running "top to bottom").
struct Pattern { img: RgbImage, w: u32, h: u32, rep_a: f64, rep_c: f64 }
impl Pattern {
    fn new(src: &RgbImage, rep_a: f64, rep_c: f64, pa: f64, pc: f64, seam_x: bool, seam_y: bool) -> Pattern {
        let s = seamless(src, seam_x, seam_y);
        let (w, h) = (((rep_c / pc).round() as u32).clamp(2, 4096), ((rep_a / pa).round() as u32).clamp(2, 4096));
        Pattern { img: image::imageops::resize(&s, w, h, FilterType::Lanczos3), w, h, rep_a, rep_c }
    }
    /// colour at along a, across c (metres), repeating
    fn at(&self, a: f64, c: f64) -> [f32; 3] {
        let x = ((c / self.rep_c).rem_euclid(1.0) * self.w as f64) as u32 % self.w;
        let y = ((1.0 - (a / self.rep_a).rem_euclid(1.0)) * self.h as f64) as u32 % self.h;
        let p = self.img.get_pixel(x, y); [p[0] as f32, p[1] as f32, p[2] as f32]
    }
}

fn hash(a: i64, b: i64, seed: u64) -> f64 {
    let mut h = (a as u64).wrapping_mul(0x9E3779B97F4A7C15) ^ (b as u64).wrapping_mul(0xC2B2AE3D27D4EB4F) ^ seed;
    h ^= h >> 31; h = h.wrapping_mul(0xBF58476D1CE4E5B9); h ^= h >> 29;
    (h >> 11) as f64 / (1u64 << 53) as f64
}
fn smooth_noise(x: f64, y: f64, seed: u64) -> f64 {   // value noise 0..1, cell 1
    let (ix, iy) = (x.floor() as i64, y.floor() as i64); let (fx, fy) = (x - ix as f64, y - iy as f64);
    let (sx, sy) = (fx * fx * (3.0 - 2.0 * fx), fy * fy * (3.0 - 2.0 * fy));
    let l = |a: f64, b: f64, t: f64| a + (b - a) * t;
    l(l(hash(ix, iy, seed), hash(ix + 1, iy, seed), sx), l(hash(ix, iy + 1, seed), hash(ix + 1, iy + 1, seed), sx), sy)
}
fn pow2(v: f64, lo: u32, hi: u32) -> u32 { let mut p = lo; while (p as f64) < v && p < hi { p *= 2; } p }

/// the fine grain for the game's 2 m detail slot: a 2 m piece of `src` (one repeat = `src_m` metres) starting at (x0, y0) metres,
/// luminance only, mean 0.5
fn detail_texture(src: &RgbImage, src_m: f64, x0: f64, y0: f64, contrast: f32) -> Vec<u8> {
    let (w, h) = src.dimensions();
    let px = ((2.0f64.min(src_m) / src_m) * w as f64) as u32;
    let (cx, cy) = (((x0 / src_m) * w as f64) as u32, ((y0 / src_m) * h as f64) as u32);
    let crop = image::imageops::crop_imm(src, cx.min(w - px), cy.min(h - px), px, px).to_image();
    let s = seamless(&image::imageops::resize(&crop, 512, 512, FilterType::Lanczos3), true, true);
    let lum: Vec<f32> = s.pixels().map(|p| 0.299 * p[0] as f32 + 0.587 * p[1] as f32 + 0.114 * p[2] as f32).collect();
    let mean = lum.iter().sum::<f32>() / lum.len() as f32;
    lum.iter().flat_map(|&l| { let v = (127.5 + (l - mean) * contrast).clamp(0.0, 255.0) as u8; [v, v, v, 255] }).collect()
}

struct Mesh { v: Vec<[f32; 8]>, i: Vec<u32> }
impl Mesh {
    fn new() -> Mesh { Mesh { v: vec![], i: vec![] } }
    /// a rectangle x0..x1 (across) y0..y1 (along) in the airport frame, cut into cells, on the terrain; uv(x, y) per vertex
    fn rect(&mut self, z: &dyn Fn(f64, f64) -> f32, x0: f64, x1: f64, y0: f64, y1: f64, cell: f64, dz: f32, uv: &dyn Fn(f64, f64) -> (f64, f64)) {
        if x1 - x0 < 1e-3 || y1 - y0 < 1e-3 { return; }
        let cuts = |a0: f64, a1: f64| -> Vec<f64> { let n = ((a1 - a0) / cell).ceil().max(1.0) as usize; (0..=n).map(|k| a0 + (a1 - a0) * k as f64 / n as f64).collect() };
        let (xs, ys) = (cuts(x0, x1), cuts(y0, y1));
        let b = self.v.len() as u32; let nx = xs.len() as u32;
        for &y in &ys { for &x in &xs { let (u, v) = uv(x, y); self.v.push([x as f32, y as f32, z(x, y) + dz, 0., 0., 1., u as f32, v as f32]); } }
        for r in 0..ys.len() as u32 - 1 { for c in 0..nx - 1 {
            let (a, bb, cc, d) = (b + r * nx + c, b + r * nx + c + 1, b + (r + 1) * nx + c + 1, b + (r + 1) * nx + c);
            self.i.extend_from_slice(&[a, bb, cc, a, cc, d]);        // counter-clockwise in local (x, y), like the ground mesh
        } }
    }
}

/// block digits of the runway designators: rectangles (x0, y0, x1, y1) in a 1 x 2 box (y up)
fn digit(d: u8) -> Vec<[f64; 4]> {
    let t = 0.24; let tv = 0.12;
    let (top, mid, bot) = ([0.0, 2.0 - tv, 1.0, 2.0], [0.0, 1.0 - tv / 2.0, 1.0, 1.0 + tv / 2.0], [0.0, 0.0, 1.0, tv]);
    let (lu, ll, ru, rl) = ([0.0, 1.0, t, 2.0], [0.0, 0.0, t, 1.0], [1.0 - t, 1.0, 1.0, 2.0], [1.0 - t, 0.0, 1.0, 1.0]);
    match d {
        0 => vec![top, bot, lu, ll, ru, rl], 1 => vec![[0.5 - t / 2.0, 0.0, 0.5 + t / 2.0, 2.0]], 2 => vec![top, mid, bot, ru, ll],
        3 => vec![top, mid, bot, ru, rl], 4 => vec![mid, lu, ru, rl], 5 => vec![top, mid, bot, lu, rl], 6 => vec![top, mid, bot, lu, ll, rl],
        7 => vec![top, ru, rl], 8 => vec![top, mid, bot, lu, ll, ru, rl], _ => vec![top, mid, bot, lu, ru, rl],
    }
}
/// marking rectangles of one runway end: (t0, s0, t1, s1), s from the threshold into the runway, t to the right of the landing direction
fn end_markings(len: f64, wid: f64, num: &str) -> Vec<[f64; 4]> {
    let mut r: Vec<[f64; 4]> = vec![];
    let mirror = |r: &mut Vec<[f64; 4]>, t0: f64, s0: f64, t1: f64, s1: f64| { r.push([t0, s0, t1, s1]); r.push([-t1, s0, -t0, s1]); };
    let per_side = if wid >= 60.0 { 8 } else if wid >= 45.0 { 6 } else if wid >= 30.0 { 4 } else if wid >= 23.0 { 3 } else { 2 };
    for k in 0..per_side { let t0 = 1.8 + k as f64 * 3.6; mirror(&mut r, t0, 6.0, t0 + 1.8, 36.0); }
    let (dw, dh, gap) = (4.5, 9.0, 2.5);
    let digits: Vec<u8> = num.bytes().filter(|b| b.is_ascii_digit()).map(|b| b - b'0').collect();
    let total = digits.len() as f64 * dw + (digits.len().max(1) - 1) as f64 * gap;
    for (k, &d) in digits.iter().enumerate() {
        let tx = -total / 2.0 + k as f64 * (dw + gap);
        for b in digit(d) { r.push([tx + b[0] * dw, 48.0 + b[1] * dh / 2.0, tx + b[2] * dw, 48.0 + b[3] * dh / 2.0]); }
    }
    let (aim_s, aim_len, aim_w, inner) = if len >= 2400.0 { (400.0, 60.0, 10.0, 9.0) } else if len >= 1200.0 { (300.0, 45.0, 6.0, 9.0) }
                                         else if len >= 800.0 { (250.0, 30.0, 4.0, 4.5) } else { (150.0, 30.0, 4.0, 3.0) };
    if wid >= 18.0 && len >= 600.0 {
        mirror(&mut r, inner, aim_s, inner + aim_w, aim_s + aim_len);
        if wid >= 30.0 && len >= 1200.0 {
            let pairs: &[(f64, usize)] = if len >= 2400.0 { &[(150.0, 3), (300.0, 3), (600.0, 2), (750.0, 2), (900.0, 1), (1050.0, 1)] }
                                         else { &[(150.0, 2), (450.0, 1), (600.0, 1)] };
            for &(s, n) in pairs {
                if s + 22.5 > len / 2.0 - 30.0 || (s < aim_s + aim_len && s + 22.5 > aim_s) { continue; }
                for k in 0..n { let t0 = inner + k as f64 * 4.5; mirror(&mut r, t0, s, t0 + 3.0, s + 22.5); }
            }
        }
    }
    r
}

pub fn kind(ad: &Value) -> &'static str {
    let s = ad["runway"]["surface"].as_str().unwrap_or("asphalt").to_lowercase();
    if ["grass", "gras", "turf"].contains(&s.as_str()) { "grass" } else if s == "concrete" { "concrete" } else { "asphalt" }
}

pub struct Built { pub objs: Vec<Obj>, pub info: String }

/// paved runway (asphalt / concrete): the "Asphalt" and "AsphaltPaint" parts, their textures in Airports/Textures/<airport>/
pub fn build(af: &Airfield, ad: &Value, ter: &Terrain, h0: f64, tex: &RunwayTextures, out: &str, index: usize) -> Option<Built> {
    let kind = kind(ad);
    let base_name = match kind { "concrete" => "concrete_base", "grass" => "grass_runway", _ => "asphalt_base" };
    let Some(base) = tex.get(base_name) else { log(&format!("  {}: {} missing in the runway texture folder, photo runway kept", af.name, base_name)); return None; };
    let td = format!("{}/Airports/Textures/{}", out, af.name); std::fs::create_dir_all(&td).unwrap();
    let (hw, hl) = (af.wid / 2.0, af.len / 2.0);
    let (cw, cl) = (hw + EDGE_OUT, hl);                                   // covered half width / half length
    // ---- the whole runway in one picture: u along (from the 'from' end), v across (from the right edge seen from 'from')
    let w = pow2(2.0 * cl / 0.12, 1024, 16384); let h = pow2(2.0 * cw / 0.1 * 0.8, 128, 1024);
    let (pa, pc) = (2.0 * cl / w as f64, 2.0 * cw / h as f64);
    let seed = af.name.bytes().fold(7u64, |a, b| a.wrapping_mul(31).wrapping_add(b as u64));
    let mut pat_base = Pattern::new(&base, REPEAT, REPEAT, pa, pc, true, true);
    if kind == "concrete" {                                              // the joints of the delivered texture are too dark and wide
        let n = (pat_base.w * pat_base.h) as f32;
        let m = pat_base.img.pixels().map(|p| p[0] as f32 + p[1] as f32 + p[2] as f32).sum::<f32>() / (3.0 * n);
        for p in pat_base.img.pixels_mut() {
            let l = (p[0] as f32 + p[1] as f32 + p[2] as f32) / 3.0;
            if l < 0.85 * m { let k = (0.85 * m - l) * 0.65; for i in 0..3 { p[i] = (p[i] as f32 + k).min(255.0) as u8; } }
        }
    }
    // rubber streaks stretched along the runway (one repeat 40 m long, 20 m wide), so they run on instead of repeating every 10 m
    const TD_A: f64 = 4.0 * REPEAT;
    let pat_td = if kind == "asphalt" { tex.get("asphalt_touchdown").map(|t| Pattern::new(&t, TD_A, 2.0 * REPEAT, pa, pc, false, true)) } else { None };
    // repairs: 2x larger than delivered (20 m per repeat), applied relative to its own mean so only the patches and cracks darken
    let pat_patch = if kind == "asphalt" { tex.get("asphalt_patches").map(|t| Pattern::new(&t, 2.0 * REPEAT, 2.0 * REPEAT, pa, pc, true, true)) } else { None };
    let patch_mean = pat_patch.as_ref().map(|p| { let n = (p.w * p.h) as f32; let s = p.img.pixels().fold([0f32; 3], |a, q| [a[0] + q[0] as f32, a[1] + q[1] as f32, a[2] + q[2] as f32]); [s[0] / n, s[1] / n, s[2] / n] }).unwrap_or([1.0; 3]);
    let pat_edge = if kind != "grass" { tex.get("asphalt_edge").map(|t| Pattern::new(&t, REPEAT, REPEAT, pa, pc, false, true)) } else { None };
    let pat_worn = if kind == "grass" { tex.get("grass_runway_worn").map(|t| Pattern::new(&t, REPEAT, REPEAT, pa, pc, false, true)) } else { None };
    let td_len = (af.len * 0.25).clamp(150.0, 900.0);
    let mut rgba = vec![0u8; (w * h * 4) as usize];
    for (r, row) in rgba.chunks_mut((w * 4) as usize).enumerate() {
        let c = cw - (r as f64 + 0.5) * pc;                                  // across: airport x (positive = left)
        for (k, px) in row.chunks_mut(4).enumerate() {
            let a = (k as f64 + 0.5) * pa;                                   // along from the 'from' threshold, 0 .. len
            let mut col = pat_base.at(a, c);
            if let Some(p) = &pat_worn {                                     // grass: worn wheel tracks along the middle 10 m
                let k = (1.0 - ((c.abs() - 3.5) / 1.5).clamp(0.0, 1.0)) as f32;
                if k > 0.0 { let t = p.at(a, c + REPEAT / 2.0); for i in 0..3 { col[i] = col[i] * (1.0 - k) + t[i] * k; } }
            }
            if let Some(p) = &pat_patch {                                    // repaired areas: irregular soft regions, tens of metres
                let n = smooth_noise(a / 70.0, c / 18.0, seed ^ 0x55) * 0.7 + smooth_noise(a / 15.0, c / 6.0, seed ^ 0x56) * 0.3;
                let k = (((n - 0.68) / 0.08).clamp(0.0, 1.0) * (1.0 - ((c.abs() - (hw - 4.0)) / 2.0).clamp(0.0, 1.0))) as f32 * 0.8;
                if k > 0.0 { let t = p.at(a, c); for i in 0..3 { col[i] *= 1.0 - k + k * (t[i] / patch_mean[i]).min(1.15); } }
            }
            if let Some(p) = &pat_td {                                       // rubber: solid, then thinning out, soft towards the sides
                let ds = a.min(af.len - a);
                if ds > 100.0 && ds < td_len && c.abs() < REPEAT {
                    let f = (ds - 100.0) / (td_len - 100.0);
                    let n = smooth_noise(a / 12.0, c / 6.0, seed);
                    let along = 0.65 + 0.35 * smooth_noise(a / 25.0, c / 4.0, seed ^ 0x77);   // heavier and lighter stretches
                    let k = (if f < 0.6 { 1.0 } else { (1.0 - (f - 0.6) / 0.4) * (0.4 + 1.2 * n) }).clamp(0.0, 1.0)
                          * (1.0 - ((c.abs() - 7.0) / 3.0).clamp(0.0, 1.0)) * along * 0.7;
                    let t = p.at(a, c + REPEAT);
                    for i in 0..3 { col[i] = col[i] * (1.0 - k as f32) + t[i] * k as f32; }
                }
            }
            if let Some(p) = &pat_edge {                                     // worn edges: 2 m of runway, the edge itself, 1 m of grass
                let e = c.abs() - (hw - 2.0);
                if e > 0.0 {
                    let t = p.at(a, 4.3 + e);                                // asphalt .. edge (6.3 m) .. grass in the texture
                    let k = (e / 0.6).clamp(0.0, 1.0) as f32;
                    for i in 0..3 { col[i] = col[i] * (1.0 - k) + t[i] * k; }
                }
            }
            let v = 0.94 + 0.12 * smooth_noise(a / 40.0, c / 25.0, seed ^ 0x99) as f32;   // large-scale tone, no visible repeats
            px.copy_from_slice(&[(col[0] * v).min(255.0) as u8, (col[1] * v).min(255.0) as u8, (col[2] * v).min(255.0) as u8, 255]);
        }
    }
    write_dds(&format!("{}/Runway.dds", td), w as usize, h as usize, false, &dxt_cpu(&rgba, w as usize, h as usize, false));
    // fine grain for the game's 2 m detail slots (concrete: from inside a slab, away from the joints)
    let d = if kind == "concrete" { detail_texture(&base, REPEAT, 0.8, 0.8, 1.6) } else { detail_texture(&base, REPEAT, 4.0, 4.0, 1.6) };
    if kind != "grass" { write_dds(&format!("{}/Asphalt.dds", td), 512, 512, false, &dxt_cpu(&d, 512, 512, false)); }
    if let Some(p) = tex.get("paint_white") { let d = detail_texture(&p, 2.0, 0.0, 0.0, 1.2); write_dds(&format!("{}/AsphaltPaint.dds", td), 512, 512, false, &dxt_cpu(&d, 512, 512, false)); }
    // ---- geometry
    // height of a vertex: the highest terrain within half a cell, so the terrain never pokes through between the vertices
    let hgt = |x: f64, y: f64| -> f64 { let (wx, wy) = af.to_world(x, y); ter.height(wx, wy) - h0 };
    let z = |x: f64, y: f64| -> f32 {
        let r = CELL * 0.6;
        [(0.0, 0.0), (r, 0.0), (-r, 0.0), (0.0, r), (0.0, -r), (r, r), (-r, r), (r, -r), (-r, -r)].iter()
            .map(|(dx, dy)| hgt(x + dx, y + dy)).fold(f64::MIN, f64::max) as f32 + Z_STEP * index as f32
    };
    let uv = |x: f64, y: f64| ((y + cl) / (2.0 * cl), (cw - x) / (2.0 * cw));
    let mut surf = Mesh::new();
    surf.rect(&z, -cw, cw, -cl, cl, CELL, Z_SURF, &uv);
    let tname = format!("Textures\\{}\\Runway.dds", af.name);
    let mut objs = if kind == "grass" {
        // drawn as the stock "GrassPaint" part (its own texture; "Gravel" came out blue); the never drawn "Grass" part makes the
        // wheels roll on grass
        let fr = Obj { name: "Grass".into(), texture: String::new(), color: [1.0; 4], params: [0.0; 3], verts: surf.v.clone(), idx: surf.i.clone() };
        vec![Obj { name: "GrassPaint".into(), texture: tname, color: [1.0; 4], params: [0.0; 3], verts: surf.v, idx: surf.i }, fr]
    } else { vec![Obj { name: "Asphalt".into(), texture: tname, color: [1.0; 4], params: [0.0; 3], verts: surf.v, idx: surf.i }] };
    let mut nmark = 0;
    if kind != "grass" && ad["runway"]["markings"].as_bool().unwrap_or(true) {
        let hd = af.heading.round() as i64;
        let num = |h: i64| { let n = ((h as f64 / 10.0).round() as i64 - 1).rem_euclid(36) + 1; format!("{:02}", n) };
        let des = ad["runway"]["designators"].as_array();
        let n_from = des.and_then(|a| a.first()).and_then(|v| v.as_str()).map(String::from).unwrap_or_else(|| num(hd));
        let n_to = des.and_then(|a| a.get(1)).and_then(|v| v.as_str()).map(String::from).unwrap_or_else(|| num(hd + 180));
        let mut rects: Vec<[f64; 4]> = vec![];   // (x0, y0, x1, y1) in the airport frame (x to the left of the direction from -y to +y)
        for (from_end, n) in [(true, &n_from), (false, &n_to)] {
            for m in end_markings(af.len, af.wid, n) {
                let (s0, s1, t0, t1) = (m[1], m[3], m[0], m[2]);
                if from_end { rects.push([-t1, -hl + s0, -t0, -hl + s1]); } else { rects.push([t0, hl - s1, t1, hl - s0]); }
            }
        }
        let cwid = if af.wid >= 45.0 { 0.9 } else if af.wid >= 30.0 { 0.45 } else { 0.3 };
        let (c0, c1) = (-hl + 72.0, hl - 72.0);
        let n = ((c1 - c0 + 20.0) / 50.0).floor().max(0.0) as i64;
        let off = c0 + ((c1 - c0) - (n as f64 * 50.0 - 20.0)) / 2.0;
        for k in 0..n { let s = off + k as f64 * 50.0; rects.push([-cwid / 2.0, s, cwid / 2.0, s + 30.0]); }
        if af.wid >= 18.0 { let sw = if af.wid >= 30.0 { 0.9 } else { 0.45 }; rects.push([hw - 0.5 - sw, -hl + 2.0, hw - 0.5, hl - 2.0]); rects.push([-hw + 0.5, -hl + 2.0, -hw + 0.5 + sw, hl - 2.0]); }
        let mut paint = Mesh::new();
        for r in &rects { paint.rect(&z, r[0], r[2], r[1], r[3], CELL, Z_PAINT, &uv); }
        nmark = rects.len();
        objs.push(Obj { name: "AsphaltPaint".into(), texture: String::new(), color: [0.85, 0.85, 0.85, 1.0], params: [0.0; 3], verts: paint.v, idx: paint.i });
    }
    let nt: usize = objs.iter().map(|o| o.idx.len() / 3).sum();
    Some(Built { objs, info: format!("custom {} runway ({} x {} px, {:.2} x {:.2} m per pixel), {} markings, {} triangles", kind, w, h, pa, pc, nmark, nt) })
}

/// Sideways correction (metres, along the airport's x axis) that centres the runway on the paved strip of the photo.
/// Runway ends typed from a map are often 10-30 m off the photo; a custom runway beside the real one looks wrong.
/// Paved = grey or bluish-grey pixels (vegetation and soil have clearly less blue than red); the best offset within +-40 m maximises
/// paved inside minus paved in 15 m bands beside it. None when the photo shows no clear strip (then nothing is moved).
pub fn align(af: &Airfield, rgb: &[u8], w: usize, h: usize, bounds: [f64; 4], east0: f64, north0: f64) -> Option<(f64, f64)> {
    let [e0, e1, n0, n1] = bounds;
    let (rx, ry) = ((e1 - e0) / w as f64, (n1 - n0) / h as f64);
    let hw = af.wid / 2.0; let span = 40.0 + hw + 16.0;
    let nb = (2.0 * span) as usize + 1;
    let (mut paved, mut all) = (vec![0f64; nb], vec![0f64; nb]);
    for r in 0..h { for c in 0..w {
        let (e, n) = (e0 + (c as f64 + 0.5) * rx, n1 - (r as f64 + 0.5) * ry);
        let (xl, yl) = af.to_local(east0 - e, n - north0);
        if xl.abs() >= span || yl.abs() > af.len / 2.0 - 100.0 { continue; }
        let p = &rgb[(r * w + c) * 3..(r * w + c) * 3 + 3];
        if p == [0, 0, 0] { continue; }
        let (mx, mn) = (p[0].max(p[1]).max(p[2]) as f64, p[0].min(p[1]).min(p[2]) as f64);
        let b = (xl + span) as usize;
        all[b] += 1.0;
        if mx > 40.0 && p[2] as f64 >= p[0] as f64 - 4.0 && (mx - mn) < 60.0 { paved[b] += 1.0; }
    } }
    let frac = |x0: f64, x1: f64| -> f64 {
        let (mut p, mut a) = (0.0, 0.0);
        for b in ((x0 + span).max(0.0) as usize)..((x1 + span).min(nb as f64) as usize) { p += paved[b]; a += all[b]; }
        if a > 0.0 { p / a } else { 0.0 }
    };
    let (mut best, mut bo) = (f64::MIN, 0.0f64);
    for k in -40..=40 {
        let o = k as f64;
        let s = frac(o - hw, o + hw) - 0.5 * (frac(o - hw - 15.0, o - hw) + frac(o + hw, o + hw + 15.0));
        if s > best + 1e-9 || ((s - best).abs() < 1e-9 && o.abs() < bo.abs()) { best = s; bo = o; }
    }
    if best < 0.25 { None } else { Some((bo, best)) }
}
