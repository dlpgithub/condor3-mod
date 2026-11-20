//! Stage "plan": projection, landscape grid, which source tile is visible where (finest wins), texture size per Condor tile.
use crate::geo::*;
use crate::src::glb_positions_ecef;
use crate::{cf, log};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;

pub const TILE: f64 = 5760.0;
pub const TEX_SIZES: [u32; 4] = [1024, 2048, 4096, 8192];

#[derive(Serialize, Deserialize, Clone)]
pub struct SrcTile {
    pub name: String, pub glb: String, pub m: Vec<f64>,
    pub south: f64, pub north: f64, pub west: f64, pub east: f64, pub level: i32,
    pub e0: f64, pub e1: f64, pub n0: f64, pub n1: f64, pub hidden: f64,
}
impl SrcTile { pub fn mat(&self) -> [f64; 16] { let mut a = [0.0; 16]; a.copy_from_slice(&self.m); a } }

/// Where a tile is hidden by FINER tiles: K x K grid over its lat/lon rectangle, [row from south][column from west].
pub struct Mask { pub k: usize, pub m: Vec<bool> }
impl Mask {
    pub fn hit(&self, hu: f64, hv: f64) -> bool {
        let k = self.k as i64;
        let r = ((hv * k as f64) as i64).clamp(0, k - 1);
        let c = ((hu * k as f64) as i64).clamp(0, k - 1);
        self.m[(r * k + c) as usize]
    }
}

/// Landscape grid + source tiles. World frame of the game: x = E0 - easting (west), y = northing - N0 (north).
#[derive(Serialize, Deserialize)]
pub struct Plan {
    pub sig: String, pub zone: i32, pub south: bool, pub nx: usize, pub ny: usize,
    pub east0: f64, pub north0: f64, pub data: [f64; 4], pub height_offset: f64,
    pub tiles: Vec<SrcTile>, pub tex: BTreeMap<String, u32>,
    #[serde(skip)] pub masks: Vec<Option<Mask>>,
}
impl Plan {
    pub fn world(&self, lat: f64, lon: f64) -> (f64, f64) { let (e, n) = utm_forward(lat, lon, self.zone, self.south); (self.east0 - e, n - self.north0) }
    pub fn latlon(&self, x: f64, y: f64) -> (f64, f64) { utm_inverse(self.east0 - x, self.north0 + y, self.zone, self.south) }
    pub fn texsize(&self, tx: usize, ty: usize) -> usize { *self.tex.get(&format!("{},{}", tx, ty)).unwrap_or(&0) as usize }
    pub fn build_masks(&mut self) {
        use rayon::prelude::*;
        let t = &self.tiles;
        self.masks = (0..t.len()).into_par_iter().map(|i| {
            let a = &t[i];
            let eps = 1e-9;
            let fin: Vec<&SrcTile> = t.iter().filter(|b| b.level > a.level && b.west < a.east - eps && b.east > a.west + eps && b.south < a.north - eps && b.north > a.south + eps).collect();
            if fin.is_empty() { return None; }
            let k = 1usize << (fin.iter().map(|b| b.level).max().unwrap() - a.level).min(6);
            let mut m = vec![false; k * k];
            for b in &fin {
                for r in 0..k {
                    let cv = a.south + (r as f64 + 0.5) / k as f64 * (a.north - a.south);
                    if !(cv > b.south && cv < b.north) { continue; }
                    for c in 0..k {
                        let cu = a.west + (c as f64 + 0.5) / k as f64 * (a.east - a.west);
                        if cu > b.west && cu < b.east { m[r * k + c] = true; }
                    }
                }
            }
            if m.iter().any(|&x| x) { Some(Mask { k, m }) } else { None }
        }).collect();
    }
}

fn mul(a: &[f64; 16], b: &[f64; 16]) -> [f64; 16] {
    let mut o = [0.0; 16];
    for r in 0..4 { for c in 0..4 { o[r * 4 + c] = (0..4).map(|k| a[r * 4 + k] * b[k * 4 + c]).sum(); } }
    o
}
const EYE: [f64; 16] = [1., 0., 0., 0., 0., 1., 0., 0., 0., 0., 1., 0., 0., 0., 0., 1.];

/// every tile with content: (glb path, accumulated transform, row-major)
pub fn read_tileset(export: &str) -> Vec<(String, [f64; 16])> {
    let t: Value = serde_json::from_slice(&std::fs::read(format!("{}/tileset.json", export)).expect("tileset.json")).expect("tileset.json is not valid JSON");
    fn walk(n: &Value, m: &[f64; 16], export: &str, found: &mut Vec<(String, [f64; 16])>) {
        let mut local = EYE;
        if let Some(a) = n["transform"].as_array() {
            if a.len() == 16 { for r in 0..4 { for c in 0..4 { local[r * 4 + c] = a[c * 4 + r].as_f64().unwrap_or(0.0); } } }   // stored column-major
        }
        let m = mul(m, &local);
        if let Some(u) = n["content"]["uri"].as_str() { found.push((format!("{}/{}", export, u), m)); }
        if let Some(ch) = n["children"].as_array() { for c in ch { walk(c, &m, export, found); } }
    }
    let mut found = vec![];
    walk(&t["root"], &EYE, export, &mut found);
    found
}

fn median(mut v: Vec<f64>) -> f64 {
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let n = v.len();
    if n % 2 == 1 { v[n / 2] } else { (v[n / 2 - 1] + v[n / 2]) / 2.0 }
}

pub fn stage_plan(exports: &[String], cfg: &Value, work: &str) -> (Plan, bool) {
    let abs: Vec<String> = exports.iter().map(|e| std::fs::canonicalize(e).map(|p| p.to_string_lossy().to_string()).unwrap_or(e.clone())).collect();
    let keys = ["margin_tiles", "texture_size", "min_texture_size", "utm_zone", "height_offset", "detail_min_share"];
    let sig = format!("{:x}", md5::compute(format!("rs2 {:?} {:?}", abs, keys.iter().map(|k| cfg[*k].to_string()).collect::<Vec<_>>())));
    let pp = format!("{}/plan.json", work);
    if let Ok(d) = std::fs::read(&pp) {
        let mut p: Plan = serde_json::from_slice(&d).expect("plan.json in the work folder is damaged: use a new work folder");
        if p.sig != sig {
            eprintln!("The exports or the layout settings changed since the work folder {} was started.\nUse a new work folder (--work DIR) or delete this one, then run again.", work);
            std::process::exit(1);
        }
        p.build_masks();
        return (p, false);
    }
    // ---- collect the tiles of all exports
    let mut seen = std::collections::HashSet::new();
    let mut tiles: Vec<SrcTile> = vec![];
    let mut nodup = 0;
    for ex in exports {
        let mut loc: std::collections::HashMap<String, Value> = Default::default();
        if let Ok(d) = std::fs::read(format!("{}/location.json", ex)) {
            if let Ok(v) = serde_json::from_slice::<Value>(&d) {
                for t in v["tiles"].as_array().map(|a| a.as_slice()).unwrap_or(&[]) { if let Some(p) = t["path"].as_str() { loc.insert(p.to_string(), t.clone()); } }
            }
        }
        for (p, m) in read_tileset(ex) {
            let base = p.rsplit('/').next().unwrap_or(&p);
            let nm = base.strip_suffix(".glb").unwrap_or(base).to_string();
            if seen.contains(&nm) { nodup += 1; continue; }
            if std::fs::metadata(&p).map(|x| x.len()).unwrap_or(0) < 100 { continue; }
            let (south, north, west, east, level);
            if let Some(b) = loc.get(&nm) {
                south = b["south"].as_f64().unwrap(); north = b["north"].as_f64().unwrap(); west = b["west"].as_f64().unwrap(); east = b["east"].as_f64().unwrap();
                level = b["level"].as_i64().unwrap_or(nm.len() as i64) as i32;
            } else {                                                    // no location.json: footprint from the mesh, level from the footprint width
                let (mut s, mut n, mut w, mut e) = (f64::MAX, f64::MIN, f64::MAX, f64::MIN);
                for v in glb_positions_ecef(&p, &m).iter() { { let (la, lo, _) = ecef_to_lla(*v); s = s.min(la); n = n.max(la); w = w.min(lo); e = e.max(lo); } }
                if s > n { continue; }
                south = s; north = n; west = w; east = e;
                level = (90.0 / (east - west).max(1e-9)).log2().round() as i32 + 3;
            }
            seen.insert(nm.clone());
            tiles.push(SrcTile { name: nm, glb: p, m: m.to_vec(), south, north, west, east, level, e0: 0., e1: 0., n0: 0., n1: 0., hidden: 0. });
        }
    }
    if tiles.is_empty() { eprintln!("no tiles found in the export folder(s)"); std::process::exit(1); }
    let lat0 = median(tiles.iter().map(|t| t.south).collect());
    let lon0 = median(tiles.iter().map(|t| t.west).collect());
    let zone = cfg["utm_zone"].as_i64().filter(|z| *z != 0).unwrap_or(((lon0 + 180.0) / 6.0).floor() as i64 + 1) as i32;
    let south = lat0 < 0.0;
    for t in tiles.iter_mut() {
        let c = [(t.south, t.west), (t.south, t.east), (t.north, t.west), (t.north, t.east)].map(|(la, lo)| utm_forward(la, lo, zone, south));
        t.e0 = c.iter().map(|x| x.0).fold(f64::MAX, f64::min); t.e1 = c.iter().map(|x| x.0).fold(f64::MIN, f64::max);
        t.n0 = c.iter().map(|x| x.1).fold(f64::MAX, f64::min); t.n1 = c.iter().map(|x| x.1).fold(f64::MIN, f64::max);
    }
    let emin = tiles.iter().map(|t| t.e0).fold(f64::MAX, f64::min); let emax = tiles.iter().map(|t| t.e1).fold(f64::MIN, f64::max);
    let nmin = tiles.iter().map(|t| t.n0).fold(f64::MAX, f64::min); let nmax = tiles.iter().map(|t| t.n1).fold(f64::MIN, f64::max);
    let m = cf(cfg, "margin_tiles", 1.0) as usize;
    let nx = (((emax - emin) / TILE).ceil() as usize).max(1) + 2 * m;
    let ny = (((nmax - nmin) / TILE).ceil() as usize).max(1) + 2 * m;
    let east0 = ((emin + emax) / 2.0 + nx as f64 * TILE / 2.0).round();          // east edge easting
    let north0 = ((nmin + nmax) / 2.0 - ny as f64 * TILE / 2.0).round();         // south edge northing
    let mut p = Plan { sig, zone, south, nx, ny, east0, north0, data: [emin, emax, nmin, nmax], height_offset: cf(cfg, "height_offset", 0.0), tiles, tex: BTreeMap::new(), masks: vec![] };
    // ---- finest wins: how much of every tile is hidden by finer ones
    p.build_masks();
    for i in 0..p.tiles.len() {
        p.tiles[i].hidden = match &p.masks[i] { Some(mk) => mk.m.iter().filter(|&&x| x).count() as f64 / mk.m.len() as f64, None => 0.0 };
    }
    // ---- texture size per Condor tile from the detail of the tiles visible in it
    let smax = cf(cfg, "texture_size", 8192.0) as u32; let smin = cf(cfg, "min_texture_size", 1024.0).max(1024.0) as u32; let share = cf(cfg, "detail_min_share", 0.03);   // min size: at least one pixel per 5.6 m statistics cell
    let mut need: BTreeMap<(usize, usize), BTreeMap<u32, f64>> = BTreeMap::new();
    for t in &p.tiles {
        if t.hidden >= 1.0 { continue; }
        let texel = (t.e1 - t.e0) / 256.0;                                       // source textures are about 256 px across a tile
        let s = TEX_SIZES.iter().copied().find(|s| TILE / *s as f64 <= texel * 1.2).unwrap_or(8192).max(smin).min(smax);
        let tx0 = (((east0 - t.e1) / TILE).floor() as i64).max(0); let tx1 = (((east0 - t.e0) / TILE).floor() as i64).min(nx as i64 - 1);
        let ty0 = (((t.n0 - north0) / TILE).floor() as i64).max(0); let ty1 = (((t.n1 - north0) / TILE).floor() as i64).min(ny as i64 - 1);
        for tx in tx0..=tx1 { for ty in ty0..=ty1 {
            let ov = (t.e1.min(east0 - tx as f64 * TILE) - t.e0.max(east0 - (tx + 1) as f64 * TILE)).max(0.0) * (t.n1.min(north0 + (ty + 1) as f64 * TILE) - t.n0.max(north0 + ty as f64 * TILE)).max(0.0);
            if ov > 0.0 { *need.entry((tx as usize, ty as usize)).or_default().entry(s).or_insert(0.0) += ov * (1.0 - t.hidden); }
        } }
    }
    for ((tx, ty), by) in &need {
        let mut cum = 0.0; let mut pick = *by.keys().next().unwrap();
        for (s, a) in by.iter().rev() { cum += a; if cum >= share * TILE * TILE { pick = *s; break; } }   // the largest size that a real share of the tile needs
        p.tex.insert(format!("{},{}", tx, ty), pick);
    }
    std::fs::write(&pp, serde_json::to_vec(&p).unwrap()).expect("cannot write plan.json");
    report(&p, cfg, work, nodup);
    (p, true)
}

// ------------------------------------------------------------------ airports from the config
/// one Condor airport = one runway. Local frame: +Y = runway heading, +X = left, origin = runway centre
pub struct Airfield {
    pub name: String, pub x0: f64, pub y0: f64, pub len: f64, pub wid: f64, pub heading: f64, pub c: f64, pub s: f64,
    pub asphalt: bool, pub freq: f64, pub hx: f64, pub hy: f64, pub grass: bool, pub lat: f64, pub lon: f64, pub flags: [bool; 3],
}
impl Airfield {
    pub fn to_local(&self, x: f64, y: f64) -> (f64, f64) { let (dw, dn) = (x - self.x0, y - self.y0); (dw * self.c + dn * self.s, -dw * self.s + dn * self.c) }
    pub fn to_world(&self, x: f64, y: f64) -> (f64, f64) { (self.x0 + x * self.c - y * self.s, self.y0 + x * self.s + y * self.c) }
    pub fn outside(&self, x: f64, y: f64) -> f64 { let (xl, yl) = self.to_local(x, y); (xl.abs() - self.hx).max(0.0).hypot((yl.abs() - self.hy).max(0.0)) }
}
pub fn airfields(cfg: &Value, plan: &Plan) -> Vec<Airfield> {
    let mut out = vec![];
    for d in cfg["airports"].as_array().map(|a| a.as_slice()).unwrap_or(&[]) {
        let r = &d["runway"];
        let pt = |v: &Value| (v[0].as_f64().expect("runway from/to must be [lat, lon]"), v[1].as_f64().expect("runway from/to must be [lat, lon]"));
        let (fa, fo) = pt(&r["from"]); let (ta, to) = pt(&r["to"]);
        let (xa, ya) = plan.world(fa, fo); let (xb, yb) = plan.world(ta, to);
        let (x0, y0) = ((xa + xb) / 2.0, (ya + yb) / 2.0);
        let len = (xb - xa).hypot(yb - ya); let wid = cf(r, "width", 30.0);
        let heading = (-(xb - xa)).atan2(yb - ya).to_degrees().rem_euclid(360.0);    // world x counts west; grid heading in the landscape's UTM zone
        let surf = r["surface"].as_str().unwrap_or("asphalt").to_lowercase();
        let g = &d["ground"];
        let grass = match &d["grass"] { Value::Bool(b) => *b, Value::String(s) => s == "auto", Value::Null => true, _ => false };
        let (lat, lon) = plan.latlon(x0, y0);
        let b = |k: &str| d[k].as_bool().unwrap_or(false);
        out.push(Airfield {
            name: d["name"].as_str().expect("every airport needs a name").to_string(), x0, y0, len, wid, heading,
            c: heading.to_radians().cos(), s: heading.to_radians().sin(), asphalt: !["grass", "gras", "turf"].contains(&surf.as_str()),
            freq: cf(d, "frequency", 123.5), hx: wid / 2.0 + cf(g, "side", 100.0), hy: len / 2.0 + cf(g, "ends", 150.0), grass, lat, lon,
            flags: [b("primary_reversed"), b("tow_primary_left"), b("tow_secondary_left")],
        });
    }
    out
}

// ------------------------------------------------------------------ report
const FONT: [[u8; 5]; 11] = [[7, 5, 5, 5, 7], [2, 6, 2, 2, 7], [7, 1, 7, 4, 7], [7, 1, 7, 1, 7], [5, 5, 7, 1, 1], [7, 4, 7, 1, 7], [7, 4, 7, 5, 7], [7, 1, 1, 1, 1], [7, 5, 7, 5, 7], [7, 5, 7, 1, 7], [0, 0, 7, 0, 0]];
fn text(im: &mut image::RgbImage, x: i64, y: i64, s: &str) {
    let mut cx = x;
    for ch in s.chars() {
        let g = match ch { '0'..='9' => Some(ch as usize - '0' as usize), '-' => Some(10), _ => None };
        if let Some(g) = g {
            for r in 0..5 { for c in 0..3 { if FONT[g][r] >> (2 - c) & 1 == 1 { rect(im, cx + c as i64 * 2, y + r as i64 * 2, cx + c as i64 * 2 + 1, y + r as i64 * 2 + 1, [255, 255, 255]); } } }
        }
        cx += 8;
    }
}
fn rect(im: &mut image::RgbImage, x0: i64, y0: i64, x1: i64, y1: i64, c: [u8; 3]) {
    let (w, h) = (im.width() as i64, im.height() as i64);
    for y in y0.max(0)..=y1.min(h - 1) { for x in x0.max(0)..=x1.min(w - 1) { im.put_pixel(x as u32, y as u32, image::Rgb(c)); } }
}

/// summary on the console and plan_levels.png: which detail level is used where
pub fn report(plan: &Plan, cfg: &Value, work: &str, nodup: usize) {
    let mut lv: Vec<i32> = plan.tiles.iter().map(|t| t.level).collect(); lv.sort(); lv.dedup();
    log(&format!("plan: UTM zone {}{}, landscape {} x {} tiles = {:.1} x {:.1} km, data {:.1} x {:.1} km{}", plan.zone, if plan.south { "S" } else { "N" }, plan.nx, plan.ny,
        plan.nx as f64 * TILE / 1000.0, plan.ny as f64 * TILE / 1000.0, (plan.data[1] - plan.data[0]) / 1000.0, (plan.data[3] - plan.data[2]) / 1000.0,
        if nodup > 0 { format!(", {} duplicate tiles ignored", nodup) } else { String::new() }));
    log("  level   tiles   used whole   partly hidden   fully hidden (finer tiles cover them)   approx. source detail");
    for l in &lv {
        let ts: Vec<&SrcTile> = plan.tiles.iter().filter(|t| t.level == *l).collect();
        log(&format!("  {:5} {:7} {:12} {:15} {:14}{:27}{:.1} m/px", l, ts.len(), ts.iter().filter(|t| t.hidden == 0.0).count(), ts.iter().filter(|t| t.hidden > 0.0 && t.hidden < 1.0).count(),
            ts.iter().filter(|t| t.hidden >= 1.0).count(), "", median(ts.iter().map(|t| (t.e1 - t.e0) / 256.0).collect())));
    }
    let mut cnt: BTreeMap<u32, usize> = BTreeMap::new();
    for v in plan.tex.values() { *cnt.entry(*v).or_insert(0) += 1; }
    log(&format!("  Condor tiles by texture size: {}, {} filler", cnt.iter().rev().map(|(k, v)| format!("{} x {} px", v, k)).collect::<Vec<_>>().join(", "), plan.nx * plan.ny - plan.tex.len()));
    let sc = 900.0 / plan.nx.max(plan.ny) as f64 / TILE;
    let w = (plan.nx as f64 * TILE * sc) as i64; let h = (plan.ny as f64 * TILE * sc) as i64;
    let mut im = image::RgbImage::from_pixel((w + 110) as u32, h.max(40 + 18 * lv.len() as i64) as u32, image::Rgb([40, 40, 40]));
    let ww = plan.east0 - plan.nx as f64 * TILE; let nn = plan.north0 + plan.ny as f64 * TILE;
    let cols: [[u8; 3]; 8] = [[70, 90, 160], [60, 150, 170], [70, 170, 90], [190, 190, 60], [220, 140, 50], [220, 70, 60], [200, 60, 170], [240, 240, 240]];
    let col = |l: i32| cols[lv.iter().position(|x| *x == l).unwrap().min(7)];
    let mut order: Vec<usize> = (0..plan.tiles.len()).collect(); order.sort_by_key(|i| plan.tiles[*i].level);   // coarse first, finer painted over them
    for i in order {
        let t = &plan.tiles[i];
        if t.hidden >= 1.0 { continue; }
        rect(&mut im, ((t.e0 - ww) * sc) as i64, ((nn - t.n1) * sc) as i64, (((t.e1 - ww) * sc) as i64).min(w - 1), ((nn - t.n0) * sc) as i64, col(t.level));
    }
    for k in 0..=plan.nx { let x = (k as f64 * TILE * sc) as i64; rect(&mut im, x, 0, x, h - 1, [0, 0, 0]); }
    for k in 0..=plan.ny { let y = (k as f64 * TILE * sc) as i64; rect(&mut im, 0, y, w - 1, y, [0, 0, 0]); }
    for tx in 0..plan.nx { for ty in 0..plan.ny {
        let s = plan.texsize(tx, ty);
        text(&mut im, ((plan.nx - 1 - tx) as f64 * TILE * sc) as i64 + 4, ((plan.ny - 1 - ty) as f64 * TILE * sc) as i64 + 4, &format!("{:02}{:02} {}", tx, ty, if s > 0 { s.to_string() } else { "-".into() }));
    } }
    for af in airfields(cfg, plan) {
        let a = af.to_world(0.0, -af.len / 2.0); let b = af.to_world(0.0, af.len / 2.0);
        for k in 0..=400 {
            let f = k as f64 / 400.0; let (x, y) = (a.0 + (b.0 - a.0) * f, a.1 + (b.1 - a.1) * f);
            let (px, py) = (((plan.east0 - x - ww) * sc) as i64, ((nn - plan.north0 - y) * sc) as i64);
            rect(&mut im, px - 1, py - 1, px + 1, py + 1, [255, 255, 255]);
        }
    }
    for (k, l) in lv.iter().enumerate() {
        rect(&mut im, w + 12, 12 + 18 * k as i64, w + 26, 24 + 18 * k as i64, col(*l));
        text(&mut im, w + 34, 13 + 18 * k as i64, &l.to_string());
    }
    let pth = format!("{}/plan_levels.png", work);
    match im.save(&pth) { Ok(_) => log(&format!("  map of the detail levels (north up; labels: Condor tile and texture size; legend: level): {}", pth)), Err(e) => log(&format!("  (no preview picture: {})", e)) }
}

// ------------------------------------------------------------------ v2 helpers
/// Pixel frame of a Condor tile: easting of the west edge, northing of the north edge, texture size (0 = no data), metres per pixel
pub struct Frame { pub ew: f64, pub nn: f64, pub s: usize, pub res: f64 }
impl Plan {
    pub fn frame(&self, tx: usize, ty: usize) -> Frame {
        let s = self.texsize(tx, ty);
        Frame { ew: self.east0 - (tx + 1) as f64 * TILE, nn: self.north0 + (ty + 1) as f64 * TILE, s, res: if s > 0 { TILE / s as f64 } else { 0.0 } }
    }
    /// visible source tiles overlapping a Condor tile (2 m slack), coarse levels first
    pub fn sources_of(&self, tx: usize, ty: usize) -> Vec<usize> {
        let f = self.frame(tx, ty);
        let (e0, n1) = (f.ew, f.nn);
        let mut v: Vec<usize> = (0..self.tiles.len()).filter(|&k| { let t = &self.tiles[k]; t.hidden < 1.0 && t.e1 > e0 - 2.0 && t.e0 < e0 + TILE + 2.0 && t.n1 > n1 - TILE - 2.0 && t.n0 < n1 + 2.0 }).collect();
        v.sort_by_key(|&k| self.tiles[k].level);
        v
    }
    /// Condor tile that holds the centre of a source tile (objects of a source tile go to that tile)
    pub fn home_tile(&self, k: usize) -> (i64, i64) {
        let t = &self.tiles[k];
        let x = self.east0 - (t.e0 + t.e1) / 2.0; let y = (t.n0 + t.n1) / 2.0 - self.north0;
        ((x / TILE).floor() as i64, (y / TILE).floor() as i64)
    }
    /// all masks as one bit array for the GPU: (bits, per tile: (offset in bits, k) or (u32::MAX, 0))
    pub fn mask_bits(&self) -> (Vec<u32>, Vec<(u32, u32)>) {
        let mut bits: Vec<u32> = vec![0]; let mut nbit = 32u32; let mut idx = vec![];
        for m in &self.masks {
            match m {
                None => idx.push((u32::MAX, 0)),
                Some(mk) => {
                    idx.push((nbit, mk.k as u32));
                    for (i, &b) in mk.m.iter().enumerate() {
                        let p = nbit as usize + i;
                        while bits.len() <= p / 32 { bits.push(0); }
                        if b { bits[p / 32] |= 1 << (p % 32); }
                    }
                    nbit += mk.m.len() as u32;
                }
            }
        }
        (bits, idx)
    }
}
