//! Stage "render": every data tile once, every source tile decoded once (plus the few that overlap a tile border):
//!   - top-down colour render -> Textures/tXXYY.dds directly when the tile is fully covered (mips + DXT1 on the GPU),
//!     otherwise the picture waits in the work folder (part_TXTY.lz4) until stage "fill" knows the colours for the gaps
//!   - mean colour per 5.625 m cell (col_TXTY.bin) for the fill colours, the thermal map, the planner map and the airport grass
//!   - photo crops around the airports (aptN_TXTY.bin) for the airport ground textures
//!   - 3D objects of the source tiles whose centre lies in this tile -> Autogen/oXXYY.c3d + atlas pages Autogen/Textures/aXXYY_NNN.dds
use crate::fmt::*;
use crate::gpu::*;
use crate::grid::Terrain;
use crate::plan::*;
use crate::scan::{prefetched, CHUNK};
use crate::src::{Prim, Tex};
use crate::{cf, log, out_of_time, prof, secs};
use rayon::prelude::*;
use serde_json::Value;
use std::collections::BTreeMap;

pub fn col_path(work: &str, tx: usize, ty: usize) -> String { format!("{}/col_{:02}{:02}.bin", work, tx, ty) }
pub fn part_path(work: &str, tx: usize, ty: usize) -> String { format!("{}/part_{:02}{:02}.lz4", work, tx, ty) }
pub fn crop_path(work: &str, ai: usize, tx: usize, ty: usize) -> String { format!("{}/apt{}_{:02}{:02}.bin", work, ai, tx, ty) }
fn done_path(work: &str, tx: usize, ty: usize) -> String { format!("{}/render_{:02}{:02}.done", work, tx, ty) }

// ------------------------------------------------------------------ pictures of partly covered tiles, waiting for stage fill
static PARTS: std::sync::Mutex<Vec<((usize, usize), Vec<u8>)>> = std::sync::Mutex::new(Vec::new());
const PART_MEM: usize = 2 << 30;
const PART_CHUNK: usize = 8 << 20;
fn part_write(work: &str, tx: usize, ty: usize, d: &[u8]) {
    let parts: Vec<Vec<u8>> = d.par_chunks(PART_CHUNK).map(lz4_flex::compress_prepend_size).collect();
    let mut o = (parts.len() as u32).to_le_bytes().to_vec();
    for p in &parts { o.extend_from_slice(&(p.len() as u32).to_le_bytes()); o.extend_from_slice(p); }
    write_atomic(&part_path(work, tx, ty), &o);
}
/// keep the picture in memory (up to PART_MEM in all), else compressed in the work folder
fn part_put(work: &str, tx: usize, ty: usize, d: Vec<u8>) {
    let mut m = PARTS.lock().unwrap();
    if m.iter().map(|p| p.1.len()).sum::<usize>() + d.len() <= PART_MEM { m.push(((tx, ty), d)); } else { drop(m); part_write(work, tx, ty, &d); }
}
pub fn part_take(work: &str, tx: usize, ty: usize) -> Option<Vec<u8>> {
    { let mut m = PARTS.lock().unwrap(); if let Some(i) = m.iter().position(|p| p.0 == (tx, ty)) { return Some(m.swap_remove(i).1); } }
    let d = std::fs::read(part_path(work, tx, ty)).ok()?;
    let n = u32::from_le_bytes([d[0], d[1], d[2], d[3]]) as usize;
    let mut sl = vec![]; let mut o = 4;
    for _ in 0..n { let l = u32::from_le_bytes([d[o], d[o + 1], d[o + 2], d[o + 3]]) as usize; sl.push(&d[o + 4..o + 4 + l]); o += 4 + l; }
    let parts: Vec<Vec<u8>> = sl.par_iter().map(|s| lz4_flex::decompress_size_prepended(s).expect("damaged part file")).collect();
    Some(parts.concat())
}
/// write the pictures still in memory to the work folder (before the program ends without running stage fill)
pub fn parts_flush(work: &str) { let m = std::mem::take(&mut *PARTS.lock().unwrap()); for ((tx, ty), d) in m { part_write(work, tx, ty, &d); } }

/// cells of the 22.5 m terrain grid that can belong to an airport's ground (v1 rule): (i0, i1, j0, j1) inclusive
pub fn airport_cells(plan: &Plan, af: &Airfield) -> (usize, usize, usize, usize) {
    let cs: Vec<(f64, f64)> = [(-1., -1.), (-1., 1.), (1., -1.), (1., 1.)].iter().map(|(sx, sy)| af.to_world(sx * af.hx, sy * af.hy)).collect();
    let fmin = |f: fn(&(f64, f64)) -> f64| cs.iter().map(f).fold(f64::MAX, f64::min); let fmax = |f: fn(&(f64, f64)) -> f64| cs.iter().map(f).fold(f64::MIN, f64::max);
    let i0 = ((fmin(|c| c.0) / 22.5).floor() as i64).max(0); let i1 = ((fmax(|c| c.0) / 22.5).floor() as i64 + 1).min((plan.nx * 256) as i64 - 1);
    let j0 = ((fmin(|c| c.1) / 22.5).floor() as i64).max(0); let j1 = ((fmax(|c| c.1) / 22.5).floor() as i64 + 1).min((plan.ny * 256) as i64 - 1);
    (i0 as usize, i1.max(i0) as usize, j0 as usize, j1.max(j0) as usize)
}

/// stored photo crop: pixel rectangle of a tile + rgba
pub struct Crop { pub x0: usize, pub y0: usize, pub w: usize, pub h: usize, pub d: Vec<u8> }
impl Crop {
    pub fn load(p: &str) -> Option<Crop> {
        let d = crate::bg::read(p)?;
        let u = |i: usize| u32::from_le_bytes([d[i * 4], d[i * 4 + 1], d[i * 4 + 2], d[i * 4 + 3]]) as usize;
        Some(Crop { x0: u(0), y0: u(1), w: u(2), h: u(3), d: d[16..].to_vec() })
    }
    fn save(&self, p: &str) { let mut o = vec![]; for v in [self.x0, self.y0, self.w, self.h] { o.extend_from_slice(&(v as u32).to_le_bytes()); } o.extend_from_slice(&self.d); crate::bg::write(p.to_string(), o); }
}

/// MD5 of the object files written in this run (for the .oha)
static OHA: std::sync::Mutex<BTreeMap<(usize, usize), String>> = std::sync::Mutex::new(BTreeMap::new());

const CELL: usize = 256;  // atlas cell [px]
const PAGE: usize = 2048; // atlas page [px] (8 x 8 cells)

/// objects of one Condor tile (one detail set: sfx "" = full detail, "_L19" = finest-wins up to level 19, ...):
/// atlas pages are written as soon as they are full (only the last page is filled, as in v1)
struct Objects { ad: String, sfx: String, tx: usize, ty: usize, cur: Option<Vec<u8>>, grid: Vec<bool>, np: usize,
    meshes: BTreeMap<usize, (Vec<[f32; 8]>, Vec<[u32; 3]>)>, flip: i64, nt: usize }
impl Objects {
    fn new(ad: &str, sfx: String, tx: usize, ty: usize) -> Objects { Objects { ad: ad.to_string(), sfx, tx, ty, cur: None, grid: vec![], np: 0, meshes: BTreeMap::new(), flip: 0, nt: 0 } }
    fn page_name(&self, pg: usize) -> String { format!("a{:02}{:02}{}_{:03}.dds", self.tx, self.ty, self.sfx, pg) }
    fn page_path(&self, pg: usize) -> String { format!("{}/Textures/{}", self.ad, self.page_name(pg)) }
    /// a finished page is compressed on the idle CPU cores and written in the background (same bytes as the GPU encoder)
    fn flush(&mut self) {
        if let Some(pg) = self.cur.take() { let p = self.page_path(self.np - 1); crate::bg::cpu(move || write_dds(&p, PAGE, PAGE, false, &dxt_cpu(&pg, PAGE, PAGE, false))); }
    }
    /// put a texture into the atlas; returns (page, x0, y0, w, h) in pixels
    fn place(&mut self, tex: &Tex) -> (usize, usize, usize, usize, usize) {
        let small;
        let t = if tex.w > PAGE || tex.h > PAGE {
            let im = image::RgbaImage::from_raw(tex.w as u32, tex.h as u32, tex.d.clone()).unwrap();
            let r = image::imageops::resize(&im, tex.w.min(PAGE) as u32, tex.h.min(PAGE) as u32, image::imageops::FilterType::Triangle);
            small = Tex { w: r.width() as usize, h: r.height() as usize, d: r.into_raw() }; &small
        } else { tex };
        let (w, h) = (t.w, t.h); let (cw, ch) = (w.div_ceil(CELL), h.div_ceil(CELL)); let n = PAGE / CELL;
        loop {
            if let Some(pg) = self.cur.as_mut() {
                let found = prof("atlas.search", || { let mut f = None; 'o: for gy in 0..=n - ch { for gx in 0..=n - cw {
                    if !(gy..gy + ch).any(|y| (gx..gx + cw).any(|x| self.grid[y * n + x])) { f = Some((gx, gy)); break 'o; } } } f });
                if let Some((gx, gy)) = found {
                    for y in gy..gy + ch { for x in gx..gx + cw { self.grid[y * n + x] = true; } }
                    prof("atlas.copy", || for y in 0..ch * CELL {                  // pad to the cell with edge pixels
                        let sy = y.min(h - 1); let src = &t.d[sy * w * 4..(sy + 1) * w * 4];
                        let o = ((gy * CELL + y) * PAGE + gx * CELL) * 4;
                        pg[o..o + w * 4].copy_from_slice(src);
                        let last = [src[w * 4 - 4], src[w * 4 - 3], src[w * 4 - 2], src[w * 4 - 1]];
                        for x in w..cw * CELL { pg[o + x * 4..o + x * 4 + 4].copy_from_slice(&last); }
                    });
                    return (self.np - 1, gx * CELL, gy * CELL, w, h);
                }
            }
            self.flush();
            self.cur = Some(vec![0u8; PAGE * PAGE * 4]); self.grid = vec![false; n * n]; self.np += 1;
        }
    }
    fn add(&mut self, vp: &[[f64; 3]], vuv: &[[f32; 2]], tri: &[[u32; 3]], tex: &Tex) {
        let (cx, cy) = ((self.tx as f64 + 0.5) * TILE, (self.ty as f64 + 0.5) * TILE);
        let (pg, px, py, w, hh) = self.place(tex);
        let (w, hh) = (w as f32, hh as f32);
        let e = self.meshes.entry(pg).or_default(); let base = e.0.len() as u32;
        for (p, uv) in vp.iter().zip(vuv) {
            let u = (px as f32 + uv[0].clamp(0.5 / w, 1.0 - 0.5 / w) * w) / PAGE as f32; let v = (py as f32 + uv[1].clamp(0.5 / hh, 1.0 - 0.5 / hh) * hh) / PAGE as f32;
            e.0.push([(p[0] - cx) as f32, (p[1] - cy) as f32, p[2] as f32, 0., 0., 1., u, v]);
        }
        for t in tri {
            let (a, b, c) = (vp[t[0] as usize], vp[t[1] as usize], vp[t[2] as usize]);
            let nz = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
            if nz < 0.0 { self.flip += 1 } else if nz > 0.0 { self.flip -= 1 }                  // roofs must face up after the mirrored axes
            e.1.push([t[0] + base, t[1] + base, t[2] + base]);
        }
        self.nt += tri.len();
    }
    /// the C3D file of this set: (path, bytes; empty when there are no objects), triangles, atlas pages
    fn finish(mut self) -> ((String, Vec<u8>), usize, usize) {
        self.flush();
        let fnm = format!("{}/o{:02}{:02}{}.c3d", self.ad, self.tx, self.ty, self.sfx);
        let mut objs = vec![];
        for (pg, (v, tri)) in std::mem::take(&mut self.meshes) {
            let idx: Vec<u32> = tri.iter().flat_map(|t| if self.flip > 0 { [t[2], t[1], t[0]] } else { *t }).collect();
            objs.push(Obj { name: format!("tiles_{:03}", pg), texture: format!("Textures\\{}", self.page_name(pg)), color: [1.; 4], params: [0.; 3], verts: v, idx });
        }
        let d = if objs.is_empty() { vec![] } else { prof("c3d.bytes", || c3d_bytes(&objs)) };
        ((fnm, d), self.nt, objs.len())
    }
}

/// the 3D part of one source tile: triangles with a corner more than min_h above the ground, not in a hole (finer tiles replace those)
/// height above the ground model of every vertex, per primitive (shared by all detail sets)
fn heights_above(ter: &Terrain, prims: &[Prim]) -> Vec<Vec<f64>> { prims.iter().map(|pr| pr.p.iter().map(|p| p[2] - ter.height(p[0], p[1])).collect()).collect() }

fn object_parts<'a>(hags: &[Vec<f64>], mk: Option<&Mask>, prims: &'a [Prim], min_h: f64, sink: f64) -> Vec<(Vec<[f64; 3]>, Vec<[f32; 2]>, Vec<[u32; 3]>, &'a Tex)> {
    let mut res = vec![];
    for (pi, pr) in prims.iter().enumerate() {
        let Some(tex) = pr.tex.as_ref() else { continue };
        if pr.tri.is_empty() { continue; }
        let n = pr.p.len();
        let hag = &hags[pi];
        let mut used = vec![false; n]; let mut kept = vec![];
        for t3 in &pr.tri {
            let [a, b, c] = t3.map(|i| i as usize);
            if let Some(m) = mk {
                let (hu, hv) = ((pr.hc[a][0] + pr.hc[b][0] + pr.hc[c][0]) as f64 / 3.0, (pr.hc[a][1] + pr.hc[b][1] + pr.hc[c][1]) as f64 / 3.0);
                if m.hit(hu, hv) { continue; }
            }
            if !(hag[a].max(hag[b]).max(hag[c]) > min_h) { continue; }
            used[a] = true; used[b] = true; used[c] = true; kept.push([a, b, c]);
        }
        let mut remap = vec![0u32; n]; let (mut vp, mut vuv) = (vec![], vec![]);
        for i in 0..n { if used[i] {
            remap[i] = vp.len() as u32;
            let mut p = pr.p[i]; if hag[i] < 1.0 { p[2] -= sink; }                         // push the feet of walls and trunks into the ground
            vp.push(p); vuv.push(pr.uv[i]);
        } }
        let tri: Vec<[u32; 3]> = kept.iter().map(|t| [remap[t[0]], remap[t[1]], remap[t[2]]]).collect();
        if !tri.is_empty() { res.push((vp, vuv, tri, tex)); }
    }
    res
}

pub fn stage_render(plan: &Plan, cfg: &Value, dev: &Dev, work: &str, out: &str, name: &str) -> bool {
    let oc = &cfg["objects"];
    let objects = !(oc.as_bool() == Some(false) || oc["enabled"].as_bool() == Some(false));
    let min_h = cf(oc, "min_height", 2.5); let sink = cf(oc, "sink", 1.5); let min_level = cf(oc, "min_level", 0.0) as i32;
    let ad = format!("{}/Autogen", out);
    std::fs::create_dir_all(format!("{}/Textures", ad)).unwrap();
    std::fs::create_dir_all(format!("{}/Textures", out)).unwrap();
    let afs = airfields(cfg, plan);
    // distant detail sets: one per source level below the finest (not below `distant_min_level`, default 17)
    let mut caps: Vec<i32> = plan.tiles.iter().map(|t| t.level).filter(|&l| l >= min_level.max(cf(oc, "distant_min_level", 17.0) as i32)).collect();
    caps.sort(); caps.dedup(); caps.pop(); caps.reverse();
    if !objects || oc["distant_sets"].as_bool() == Some(false) { caps.clear(); }
    let capmasks: Vec<(i32, Vec<Option<Mask>>)> = caps.iter().map(|&c| (c, plan.masks_cap(c))).collect();
    if !caps.is_empty() { log(&format!("distant autogen sets: levels {:?} (finest wins up to that level)", caps)); }
    let mut ter: Option<Terrain> = None;
    for tx in 0..plan.nx { for ty in 0..plan.ny {
        let f = plan.frame(tx, ty);
        if f.s == 0 || crate::bg::exists(&done_path(work, tx, ty)) { continue; }
        if out_of_time() { return false; }
        if objects && ter.is_none() { ter = Some(Terrain::load(plan, out)); }
        let t0 = secs();
        // ground photo: the visible tiles; objects: the tiles whose centre is here, per detail set (full = as the photo)
        let srcs = plan.sources_of(tx, ty); let draw_n = srcs.len();
        let homes: Vec<usize> = if objects { (0..plan.tiles.len()).filter(|&k| plan.home_tile(k) == (tx as i64, ty as i64) && plan.tiles[k].level >= min_level).collect() } else { vec![] };
        let gone = |m: &Option<Mask>| m.as_ref().is_some_and(|m| m.all());
        let sets: Vec<usize> = (0..capmasks.len()).filter(|&i| homes.iter().any(|&k| plan.tiles[k].hidden < 1.0 && plan.tiles[k].level > capmasks[i].0)).collect();
        let mut list = srcs.clone();
        { let have: std::collections::HashSet<usize> = srcs.iter().copied().collect();
          for &k in &homes { if !have.contains(&k) && sets.iter().any(|&i| !gone(&capmasks[i].1[k])) { list.push(k); } } }
        let is_home: std::collections::HashSet<usize> = homes.iter().copied().collect();
        // membership per set and list position (set 0 = full detail)
        let mut member: Vec<Vec<bool>> = vec![list.iter().map(|&k| is_home.contains(&k) && plan.tiles[k].hidden < 1.0).collect()];
        for &i in &sets { member.push(list.iter().map(|&k| is_home.contains(&k) && !gone(&capmasks[i].1[k])).collect()); }
        let mask_of = |s: usize, k: usize| -> Option<&Mask> { if s == 0 { plan.masks[k].as_ref() } else { capmasks[sets[s - 1]].1[k].as_ref() } };
        let mut cv = dev.canvas(f.s, true);
        let mut obs: Vec<Objects> = vec![Objects::new(&ad, String::new(), tx, ty)];
        for &i in &sets { obs.push(Objects::new(&ad, format!("_L{}", capmasks[i].0), tx, ty)); }
        let mut ntri = 0usize;
        let ter_ref = ter.as_ref();
        prefetched(plan, dev, &f, &list, draw_n, true, |ci, loaded, prep, texs| {
            ntri += prep.tris();
            prof("render.draw", || cv.draw(prep, texs));
            if let Some(ter) = ter_ref {
                let hags: Vec<Vec<Vec<f64>>> = prof("render.heights", || loaded.par_iter().enumerate().map(|(i, (_, prims))| if member.iter().any(|m| m[ci * CHUNK + i]) { heights_above(ter, prims) } else { vec![] }).collect());
                for (s, ob) in obs.iter_mut().enumerate() {
                    let parts: Vec<_> = prof("render.objects", || loaded.par_iter().enumerate().filter(|(i, _)| member[s][ci * CHUNK + i]).map(|(i, (k, prims))| object_parts(&hags[i], mask_of(s, *k), prims, min_h, sink)).collect());
                    prof("render.atlas", || for (vp, vuv, tri, tex) in parts.iter().flatten() { ob.add(vp, vuv, tri, tex); });
                }
            }
        });
        let (st, cov) = prof("render.stats", || cv.stats());
        let col: Vec<u16> = st.iter().flat_map(|s| [s[1], s[2], s[3]]).map(|v| (v * 256.0).round() as u16).collect();   // 1/256 grey level steps
        crate::bg::write(col_path(work, tx, ty), bytemuck::cast_slice(&col).to_vec());
        let full = cov == (f.s * f.s) as u64;
        if full { let b = prof("render.dxt", || cv.dxt()); write_dds(&format!("{}/Textures/t{:02}{:02}.dds", out, tx, ty), f.s, f.s, false, &b); }
        else { let d = prof("render.readback", || cv.rgba()); prof("render.part", || part_put(work, tx, ty, d)); }
        // airport photo crops
        for (ai, af) in afs.iter().enumerate() {
            let (i0, i1, j0, j1) = airport_cells(plan, af);
            let (xw0, xw1, yn0, yn1) = (i0 as f64 * 22.5, (i1 + 1) as f64 * 22.5, j0 as f64 * 22.5, (j1 + 1) as f64 * 22.5);
            let c0 = (((plan.east0 - xw1 - f.ew) / f.res).floor() as i64 - 1).max(0); let c1 = (((plan.east0 - xw0 - f.ew) / f.res).ceil() as i64 + 1).min(f.s as i64);
            let r0 = (((f.nn - plan.north0 - yn1) / f.res).floor() as i64 - 1).max(0); let r1 = (((f.nn - plan.north0 - yn0) / f.res).ceil() as i64 + 1).min(f.s as i64);
            if c0 >= c1 || r0 >= r1 { continue; }
            let (x0, y0, w, h) = (c0 as usize, r0 as usize, (c1 - c0) as usize, (r1 - r0) as usize);
            prof("render.crops", || Crop { x0, y0, w, h, d: cv.crop(x0, y0, w, h) }.save(&crop_path(work, ai, tx, ty)));
        }
        // object files of all sets, then the done marker, in one background job (so the marker never lands before them)
        let mut files = vec![]; let mut stat = String::new(); let (mut nt, mut np, mut nb) = (0, 0, 0);
        for (s, ob) in obs.into_iter().enumerate() {
            let sfx = ob.sfx.clone();
            let (file, t, p) = prof("render.c3d", || ob.finish());
            if s == 0 { nt = t; np = p; nb = file.1.len(); files.push(file); }
            else { stat.push_str(&format!(", {} {:.2} M", &sfx[1..], t as f64 / 1e6)); if !file.1.is_empty() { files.push(file); } }
        }
        { let marker = done_path(work, tx, ty); let key = (tx, ty);
          crate::bg::cpu(move || {
              for (i, (p, d)) in files.into_iter().enumerate() { if i == 0 && !d.is_empty() { OHA.lock().unwrap().insert(key, format!("{:x}", md5::compute(&d))); } crate::bg::write(p, d); }
              crate::bg::write(marker, vec![]);
          }); }
        log(&format!("render tile {:02}{:02} ({} px): {} source tiles, {:.1} M triangles, coverage {:.1}%{}; objects {} triangles, {} atlas pages, {:.0} MB{}  ({:.1}s)",
            tx, ty, f.s, draw_n, ntri as f64 / 1e6, cov as f64 * 100.0 / (f.s * f.s) as f64, if full { ", texture written" } else { "" }, nt, np, nb as f64 / 1e6,
            if stat.is_empty() { String::new() } else { format!("; distant sets{}", stat) }, secs() - t0));
    } }
    // .oha: MD5 of every object file (computed in the background; files from an earlier run are read)
    crate::bg::wait_cpu();
    let known = std::mem::take(&mut *OHA.lock().unwrap());
    let mut oha = String::new();
    for tx in 0..plan.nx { for ty in 0..plan.ny {
        let key = format!("{:06}", tx * 1000 + ty);
        let h = known.get(&(tx, ty)).cloned().or_else(|| std::fs::read(format!("{}/o{:02}{:02}.c3d", ad, tx, ty)).ok().filter(|d| !d.is_empty()).map(|d| format!("{:x}", md5::compute(&d))));
        match h { Some(h) => oha.push_str(&format!("{} {}\r\n", key, h)), None => oha.push_str(&format!("{}\r\n", key)) }
    } }
    std::fs::write(format!("{}/{}.oha", out, name), oha).unwrap();
    // list of the detail sets for the streaming helper (the game itself only reads oXXYY.c3d through the .oha)
    if !caps.is_empty() {
        let mut l = String::from("# autogen detail sets per Condor tile, finest first: tile level file   (level = finest source level in the file;\r\n# o*_L<level>.c3d = finest wins up to that level, coarser tiles from the export instead of the hidden finer ones)\r\n");
        for tx in 0..plan.nx { for ty in 0..plan.ny {
            let full = format!("o{:02}{:02}.c3d", tx, ty);
            if crate::bg::len(&format!("{}/{}", ad, full)).unwrap_or(0) == 0 { continue; }
            let lv = (0..plan.tiles.len()).filter(|&k| plan.tiles[k].hidden < 1.0 && plan.home_tile(k) == (tx as i64, ty as i64)).map(|k| plan.tiles[k].level).max().unwrap_or(0);
            l.push_str(&format!("{:02}{:02} {} {}\r\n", tx, ty, lv, full));
            for &c in &caps {
                let fnm = format!("o{:02}{:02}_L{}.c3d", tx, ty, c);
                if crate::bg::len(&format!("{}/{}", ad, fnm)).unwrap_or(0) > 0 { l.push_str(&format!("{:02}{:02} {} {}\r\n", tx, ty, c, fnm)); }
            }
        } }
        std::fs::write(format!("{}/lods.txt", ad), l).unwrap();
    }
    true
}
