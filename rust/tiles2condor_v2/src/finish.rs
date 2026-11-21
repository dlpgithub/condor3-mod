//! Stages "fill" (colours for the area without data, remaining tile textures, planner map), "base" and "airports".
use crate::fmt::*;
use crate::gpu::{Dev, CELLS};
use crate::grid::*;
use crate::plan::*;
use crate::render::{airport_cells, col_path, crop_path, part_path, part_take, Crop};
use crate::terrain::zmin_path;
use crate::{log, out_of_time, prof, secs};
use rayon::prelude::*;
use serde_json::Value;
use std::path::Path;

/// repeatable noise with standard deviation of about 4 grey levels (as v1)
fn noise(mut s: u64) -> f32 {
    s = s.wrapping_mul(0x9E3779B97F4A7C15); s ^= s >> 32; s = s.wrapping_mul(0xD6E8FEB86659FD93); s ^= s >> 32;
    let u = |k: u32| ((s >> (k * 16)) & 0xFFFF) as f32 / 65536.0;
    (u(0) + u(1) + u(2) + u(3) - 2.0) * 6.93
}
fn c8(v: f32) -> u8 { v.clamp(0.0, 255.0) as u8 }
fn fc2_path(work: &str, tx: usize, ty: usize) -> String { format!("{}/fc2_{:02}{:02}.bin", work, tx, ty) }
fn fill90_path(work: &str) -> String { format!("{}/fill90.bin", work) }
fn tex_path(out: &str, tx: usize, ty: usize) -> String { format!("{}/Textures/t{:02}{:02}.dds", out, tx, ty) }

/// fill colours per G cell (v1: push-pull of the mean colours, then three rounds of radius-16 blurs in the gaps); per tile:
/// writes the textures of filler tiles and of partly covered tiles, and keeps 90 m averages (fill90.bin) for the map and the thermals
pub fn stage_fill(plan: &Plan, _dev: &Dev, work: &str, out: &str, name: &str) -> bool {
    let tiles: Vec<(usize, usize)> = (0..plan.nx).flat_map(|tx| (0..plan.ny).map(move |ty| (tx, ty))).collect();
    let data: Vec<(usize, usize)> = tiles.iter().copied().filter(|&(tx, ty)| plan.texsize(tx, ty) > 0).collect();
    for &(tx, ty) in &data { if !crate::bg::exists(&col_path(work, tx, ty)) { eprintln!("{} is missing: run the render stage first", col_path(work, tx, ty)); std::process::exit(1); } }
    let todo = tiles.iter().any(|&(tx, ty)| !crate::bg::exists(&tex_path(out, tx, ty))) || !crate::bg::exists(&fill90_path(work));
    if !todo { return true; }
    std::fs::create_dir_all(format!("{}/Textures", out)).unwrap();
    let (wa, wb) = (work.to_string(), work.to_string());
    let cs = TileSet::new(plan, 3, move |tx, ty| col_path(&wa, tx, ty)).u16();
    let zs = TileSet::new(plan, 1, move |tx, ty| zmin_path(&wb, tx, ty));
    let valid_of = |tx: usize, ty: usize| -> Vec<bool> { zs.get(tx, ty).map(|z| z.iter().map(|v| v.is_finite()).collect()).unwrap_or(vec![false; CELLS * CELLS]) };
    // pyramid tops, landscape-wide push-pull, pull down
    let n = CELLS >> PP_LEVEL; let (gh, gw) = (plan.ny * n, plan.nx * n);
    let tops: Vec<((usize, usize), (Vec<f32>, Vec<bool>))> = prof("fill.tops", || data.par_iter().map(|&(tx, ty)| ((tx, ty), pyramid(&cs.get(tx, ty).unwrap(), &valid_of(tx, ty), 3)[PP_LEVEL].clone())).collect());
    let mut ga = vec![0f32; gh * gw * 3]; let mut gv = vec![false; gh * gw];
    for ((tx, ty), (a, v)) in &tops {
        let (r0, c0) = ((plan.ny - 1 - ty) * n, (plan.nx - 1 - tx) * n);
        for r in 0..n { for c in 0..n { let g = (r0 + r) * gw + c0 + c; gv[g] = v[r * n + c]; for k in 0..3 { ga[g * 3 + k] = a[(r * n + c) * 3 + k]; } } }
    }
    let wc = work.to_string();
    let fs = TileSet::new(plan, 3, move |tx, ty| fc2_path(&wc, tx, ty));
    let gf = prof("fill.pushpull", || pushpull(&ga, &gv, gh, gw, 3));
    prof("fill.pulldown", || tiles.par_iter().for_each(|&(tx, ty)| {
        let (r0, c0) = ((plan.ny - 1 - ty) * n, (plan.nx - 1 - tx) * n);
        let top: Vec<f32> = (0..n * n).flat_map(|i| { let g = (r0 + i / n) * gw + c0 + i % n; [gf[g * 3], gf[g * 3 + 1], gf[g * 3 + 2]] }).collect();
        let lv = match cs.get(tx, ty) {
            Some(c) => pyramid(&c, &valid_of(tx, ty), 3),
            None => (0..=PP_LEVEL).map(|l| { let m = (CELLS >> l) * (CELLS >> l); (vec![0f32; m * 3], vec![false; m]) }).collect(),
        };
        fs.put(tx, ty, pull_down(&lv, &top, 3));
    }));
    // per tile (in parallel): soften the gaps, 90 m averages, and the textures that need the fill colours
    let stopped = std::sync::atomic::AtomicBool::new(false);
    let res: Vec<((usize, usize), Vec<f32>, [f64; 3])> = tiles.par_iter().filter_map(|&(tx, ty)| {
        if out_of_time() { stopped.store(true, std::sync::atomic::Ordering::Relaxed); return None; }
        let wbx = tile_window(plan, tx, ty, 48);
        let (h, w) = (wbx.1 - wbx.0, wbx.3 - wbx.2);
        let mut f = fs.window(wbx.0, wbx.1, wbx.2, wbx.3, 0.0);
        let valid: Vec<bool> = zs.window(wbx.0, wbx.1, wbx.2, wbx.3, f32::NAN).iter().map(|v| v.is_finite()).collect();
        if valid.iter().any(|&v| !v) { prof("fill.soften", || {
            for _ in 0..3 {                                                       // three rounds, the channels are independent
                let b = blur(&f, h, w, 3, 16);
                for i in 0..h * w { if !valid[i] { f[i * 3..i * 3 + 3].copy_from_slice(&b[i * 3..i * 3 + 3]); } }
            }
        }); }
        let fill = interior(plan, tx, ty, &f, wbx, 3);
        drop(f);
        let mut total = [0f64; 3];
        for i in 0..CELLS * CELLS { for k in 0..3 { total[k] += fill[i * 3 + k] as f64; } }
        let mut blk = vec![0f32; 64 * 64 * 3];
        for r in 0..64 { for c in 0..64 {
            let mut s = [0f32; 3];
            for y in 0..16 { for x in 0..16 { for k in 0..3 { s[k] += fill[((r * 16 + y) * CELLS + c * 16 + x) * 3 + k]; } } }
            for k in 0..3 { blk[(r * 64 + c) * 3 + k] = s[k] / 256.0; }
        } }
        let tp = tex_path(out, tx, ty);
        if !crate::bg::exists(&tp) {
            let t0 = secs();
            let s = plan.texsize(tx, ty);
            let seed = ((tx * 1000 + ty) as u64) << 40;
            let fg = |r: usize, c: usize, k: usize| fill[(r * CELLS + c) * 3 + k];
            if s == 0 {                                                           // filler tile: small texture from the extended colours
                let mut img = vec![255u8; 512 * 512 * 4];
                img.par_chunks_mut(512 * 4).enumerate().for_each(|(r, row)| { for c in 0..512 {
                    let nz = noise(seed + (r * 512 + c) as u64);
                    for k in 0..3 { row[c * 4 + k] = c8((fg(2 * r, 2 * c, k) + fg(2 * r + 1, 2 * c, k) + fg(2 * r, 2 * c + 1, k) + fg(2 * r + 1, 2 * c + 1, k)) / 4.0 + nz); }
                } });
                write_dds(&tp, 512, 512, false, &dxt_cpu(&img, 512, 512, false));
            } else {                                                              // partly covered: extended colours with a little noise in the gaps
                let mut img = part_take(work, tx, ty).unwrap_or_else(|| { eprintln!("{} is missing: run the render stage again", part_path(work, tx, ty)); std::process::exit(1) });
                let f = s / CELLS;
                img.par_chunks_mut(s * 4).enumerate().for_each(|(y, row)| { for x in 0..s {
                    if row[x * 4 + 3] != 0 { continue; }
                    let nz = noise(seed + (y * s + x) as u64);
                    for k in 0..3 { row[x * 4 + k] = c8(fg(y / f, x / f, k) + nz); }
                } });
                write_dds(&tp, s, s, false, &prof("fill.dxt", || dxt_cpu(&img, s, s, false)));
            }
            log(&format!("texture t{:02}{:02} {} px  ({:.1}s)", tx, ty, if s == 0 { 512 } else { s }, secs() - t0));
        }
        Some(((tx, ty), blk, total))
    }).collect();
    let stopped = stopped.load(std::sync::atomic::Ordering::Relaxed);
    let mut f90 = vec![0f32; plan.ny * 64 * plan.nx * 64 * 3];
    let mut total = [0f64; 3];
    for ((tx, ty), blk, t) in &res {
        for k in 0..3 { total[k] += t[k]; }
        let (gr, gc) = ((plan.ny - 1 - ty) * 64, (plan.nx - 1 - tx) * 64);
        for r in 0..64 { let o = ((gr + r) * plan.nx * 64 + gc) * 3; f90[o..o + 64 * 3].copy_from_slice(&blk[r * 64 * 3..(r + 1) * 64 * 3]); }
    }
    if stopped { return false; }
    write_f32(&fill90_path(work), &f90);
    fs.remove_files();
    for &(tx, ty) in &tiles { let _ = std::fs::remove_file(part_path(work, tx, ty)); }
    let npx = (tiles.len() * CELLS * CELLS) as f64;
    let m: Vec<u8> = total.iter().map(|v| c8((v / npx) as f32)).collect();
    let mut e = vec![255u8; 256 * 256 * 4];
    for p in e.chunks_mut(4) { p[..3].copy_from_slice(&m); }
    write_dds(&format!("{}/Textures/empty.dds", out), 256, 256, true, &dxt_cpu(&e, 256, 256, true));
    // flight-planner map: 90 m per pixel, north up, bottom-up BMP with exactly the .trn dimensions
    let (bw, bh) = (plan.nx * 64, plan.ny * 64);
    let mut d = b"BM".to_vec();
    d.extend_from_slice(&((54 + bw * bh * 4) as u32).to_le_bytes()); d.extend_from_slice(&[0u8; 4]);
    for v in [54u32, 40] { d.extend_from_slice(&v.to_le_bytes()); }
    for v in [bw as i32, bh as i32] { d.extend_from_slice(&v.to_le_bytes()); }
    d.extend_from_slice(&1u16.to_le_bytes()); d.extend_from_slice(&32u16.to_le_bytes());
    for v in [0u32, (bw * bh * 4) as u32] { d.extend_from_slice(&v.to_le_bytes()); }
    for v in [2834i32, 2834] { d.extend_from_slice(&v.to_le_bytes()); }
    d.extend_from_slice(&[0u8; 8]);
    for r in (0..bh).rev() { for c in 0..bw { let o = (r * bw + c) * 3; d.extend_from_slice(&[c8(f90[o + 2]), c8(f90[o + 1]), c8(f90[o]), 255]); } }
    std::fs::write(format!("{}/{}.bmp", out, name), d).unwrap();
    true
}

pub fn stage_base(plan: &Plan, work: &str, out: &str, name: &str, trn: &[u16]) {
    std::fs::write(format!("{}/{}.ini", out, name), b"[General]\r\nVersion=1.02\r\nRealtimeShading=1").unwrap();
    std::fs::create_dir_all(format!("{}/ForestMaps", out)).unwrap();
    let zero = vec![0u8; 512 * 512]; let mut fha = String::new(); let mut keys = String::new();
    for tx in 0..plan.nx { for ty in 0..plan.ny {                                 // no generated trees: the 3D data brings its own
        crate::bg::write(format!("{}/ForestMaps/{:02}{:02}.for", out, tx, ty), zero.clone());
        fha.push_str(&format!("{:06} {}\r\n", tx * 1000 + ty, condor_hash(&zero, (plan.ny * tx + ty) as u32)));
        keys.push_str(&format!("{:06}\r\n", tx * 1000 + ty));
    } }
    std::fs::write(format!("{}/{}.fha", out, name), fha).unwrap();
    let f90 = read_f32(&fill90_path(work)).unwrap_or_else(|| { eprintln!("fill90.bin is missing: run the fill stage first"); std::process::exit(1) });
    let (tw, th) = (plan.nx * 64, plan.ny * 64);
    let mut therm = vec![0u8; tw * th];                                           // [y from south][x from east]; guess: vegetation 102, built-up / bare 178
    for r in 0..th { for c in 0..tw {
        let o = (r * tw + c) * 3; let s = [f90[o], f90[o + 1], f90[o + 2]];
        let green = s[1] > s[0] * 1.03 && s[1] > s[2] * 1.05;
        therm[(th - 1 - r) * tw + (tw - 1 - c)] = if green { 102 } else { 178 };
    } }
    std::fs::write(format!("{}/{}.tm3", out, name), make_tm3(trn, tw, th, &therm)).unwrap();
    if !Path::new(&format!("{}/{}.obj", out, name)).exists() { std::fs::write(format!("{}/{}.obj", out, name), b"").unwrap(); }
    std::fs::write(format!("{}/{}.air", out, name), b"").unwrap();
    std::fs::write(format!("{}/{}.aha", out, name), b"0\r\n").unwrap();
    if !Path::new(&format!("{}/{}.oha", out, name)).exists() { std::fs::write(format!("{}/{}.oha", out, name), keys).unwrap(); }
}

/// north-up orthophoto of a UTM rectangle at the finest resolution of the Condor tiles it touches (from the crops of stage render)
fn crop_ortho(plan: &Plan, work: &str, ai: usize, e0: f64, mut e1: f64, mut n0: f64, n1: f64) -> (Vec<u8>, usize, usize, [f64; 4]) {
    let txs = (((plan.east0 - e1) / TILE).floor() as i64).max(0) as usize..=(((plan.east0 - e0) / TILE).floor() as i64).min(plan.nx as i64 - 1) as usize;
    let tys = (((n0 - plan.north0) / TILE).floor() as i64).max(0) as usize..=(((n1 - plan.north0) / TILE).floor() as i64).min(plan.ny as i64 - 1) as usize;
    let smax = txs.clone().flat_map(|tx| tys.clone().map(move |ty| (tx, ty))).map(|(tx, ty)| plan.texsize(tx, ty)).max().unwrap_or(0);
    let mut res = TILE / if smax > 0 { smax as f64 } else { 1024.0 };
    while (e1 - e0) * (n1 - n0) / (res * res) > 20e6 { res *= 2.0; }
    let w = ((((e1 - e0) / res) as usize) / 4 * 4).max(4); let h = ((((n1 - n0) / res) as usize) / 4 * 4).max(4);
    e1 = e0 + w as f64 * res; n0 = n1 - h as f64 * res;
    let mut rgb = vec![0u8; w * h * 3];
    for tx in txs { for ty in tys.clone() {
        let f = plan.frame(tx, ty);
        if f.s == 0 { continue; }
        let Some(cr) = Crop::load(&crop_path(work, ai, tx, ty)) else { continue };
        let cols: Vec<(usize, usize)> = (0..w).filter_map(|c| { let e = e0 + (c as f64 + 0.5) * res; if e >= f.ew && e < f.ew + TILE { Some((c, (((e - f.ew) / f.res) as usize).min(f.s - 1))) } else { None } }).collect();
        let rows: Vec<(usize, usize)> = (0..h).filter_map(|r| { let n = n1 - (r as f64 + 0.5) * res; if n <= f.nn && n > f.nn - TILE { Some((r, (((f.nn - n) / f.res) as usize).min(f.s - 1))) } else { None } }).collect();
        for &(r, sr) in &rows { for &(cc, sc) in &cols {
            if sr < cr.y0 || sr >= cr.y0 + cr.h || sc < cr.x0 || sc >= cr.x0 + cr.w { continue; }
            let o = ((sr - cr.y0) * cr.w + sc - cr.x0) * 4;
            rgb[(r * w + cc) * 3..(r * w + cc) * 3 + 3].copy_from_slice(&cr.d[o..o + 3]);
        } }
    } }
    (rgb, w, h, [e0, e1, n0, n1])
}

/// a box standing on z0 (5 faces), vertices with normals and uv
fn boxmesh(cx: f32, cy: f32, z0: f32, w: f32, d: f32, h: f32) -> (Vec<[f32; 8]>, Vec<u32>) {
    let (x0, x1, y0, y1, z1) = (cx - w / 2.0, cx + w / 2.0, cy - d / 2.0, cy + d / 2.0, z0 + h);
    let faces: [([f32; 3], [[f32; 3]; 4]); 5] = [
        ([0., 0., 1.], [[x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]]), ([0., -1., 0.], [[x0, y0, z0], [x1, y0, z0], [x1, y0, z1], [x0, y0, z1]]),
        ([0., 1., 0.], [[x1, y1, z0], [x0, y1, z0], [x0, y1, z1], [x1, y1, z1]]), ([1., 0., 0.], [[x1, y0, z0], [x1, y1, z0], [x1, y1, z1], [x1, y0, z1]]),
        ([-1., 0., 0.], [[x0, y1, z0], [x0, y0, z0], [x0, y0, z1], [x0, y1, z1]])];
    let (mut v, mut i) = (vec![], vec![]);
    for (n, pts) in faces {
        let b = v.len() as u32;
        for (k, p) in pts.iter().enumerate() { v.push([p[0], p[1], p[2], n[0], n[1], n[2], (k == 1 || k == 2) as u8 as f32, (k == 2 || k == 3) as u8 as f32]); }
        i.extend_from_slice(&[b, b + 1, b + 2, b, b + 2, b + 3]);
    }
    (v, i)
}

pub fn stage_airports(plan: &Plan, cfg: &Value, work: &str, out: &str, name: &str) {
    let mut afs = airfields(cfg, plan);
    std::fs::create_dir_all(format!("{}/Airports/Textures", out)).unwrap();
    let ter = Terrain::load(plan, out);
    let (gw, gh) = (plan.nx * CELLS, plan.ny * CELLS);
    let (wa, wb) = (work.to_string(), work.to_string());
    let cs = TileSet::new(plan, 3, move |tx, ty| col_path(&wa, tx, ty)).u16();
    let zs = TileSet::new(plan, 1, move |tx, ty| zmin_path(&wb, tx, ty));
    let cny = ter.ny - 1;
    let mut claimed = vec![false; (ter.nx - 1) * cny];
    let ads: Vec<Value> = cfg["airports"].as_array().cloned().unwrap_or_default();
    let custom = |ai: usize| ads.get(ai).and_then(|d| d["runway_look"].as_str()).map(|s| s.eq_ignore_ascii_case("custom")).unwrap_or(false);
    let rtex = if (0..afs.len()).any(|ai| custom(ai)) {
        let t = crate::runway::prepare(cfg);
        if t.is_none() { log("runway textures not found (config \"runway_textures\" or assets/runway next to the program): photo runways kept"); }
        t
    } else { None };
    // custom runways: centre them on the paved strip of the photo only when asked ("align": true)
    for (ai, af) in afs.iter_mut().enumerate() {
        let surf = ads[ai]["runway"]["surface"].as_str().unwrap_or("asphalt").to_lowercase();
        if !custom(ai) || rtex.is_none() || ["grass", "gras", "turf"].contains(&surf.as_str()) || ads[ai]["runway"]["align"].as_bool() != Some(true) { continue; }
        let corners: Vec<(f64, f64)> = [(-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)].iter().map(|(a, b)| af.to_world(a * (af.wid / 2.0 + 60.0), b * af.len / 2.0)).collect();
        let (xmin, xmax) = corners.iter().fold((f64::MAX, f64::MIN), |m, c| (m.0.min(c.0), m.1.max(c.0)));
        let (ymin, ymax) = corners.iter().fold((f64::MAX, f64::MIN), |m, c| (m.0.min(c.1), m.1.max(c.1)));
        let (rgb, pw, ph, bounds) = crop_ortho(plan, work, ai, plan.east0 - xmax, plan.east0 - xmin, plan.north0 + ymin, plan.north0 + ymax);
        match crate::runway::align(af, &rgb, pw, ph, bounds, plan.east0, plan.north0) {
            Some((o, score)) if o != 0.0 => {
                let (nx, ny) = af.to_world(o, 0.0); af.x0 = nx; af.y0 = ny; let (la, lo) = plan.latlon(nx, ny); af.lat = la; af.lon = lo;
                log(&format!("airport {}: runway moved {:.0} m to the {} (seen from the 'from' end) onto the paved strip of the photo (match {:.2}; \"align\": false keeps the configured position)", af.name, o.abs(), if o > 0.0 { "left" } else { "right" }, score));
            }
            Some(_) => {}
            None => log(&format!("airport {}: no clear runway strip in the photo, configured position kept", af.name)),
        }
    }
    let strips: Vec<&Airfield> = afs.iter().enumerate().filter(|(ai, _)| custom(*ai) && rtex.is_some()).map(|(_, a)| a).collect();
    let mut recs: Vec<u8> = vec![]; let mut cup = vec!["name,code,country,lat,lon,elev,style,rwdir,rwlen,freq,desc".to_string()]; let mut info = vec![];
    for (ai, af) in afs.iter().enumerate() {
        let (i0, i1, j0, j1) = airport_cells(plan, af);
        let i1 = i1.min(ter.nx - 2); let j1 = j1.min(ter.ny - 2);
        // G cells under the candidate cells (+2 halo for the greenness blur), validity and "mostly green" per 22.5 m cell
        let gcol = |x: f64| (((plan.nx as f64 * TILE - x) / G) as i64).clamp(0, gw as i64 - 1) as usize;
        let grow = |y: f64| (((plan.ny as f64 * TILE - y) / G) as i64).clamp(0, gh as i64 - 1) as usize;
        let (r0, r1) = (grow((j1 + 1) as f64 * 22.5).saturating_sub(3), (grow(j0 as f64 * 22.5) + 4).min(gh));
        let (c0, c1) = (gcol((i1 + 1) as f64 * 22.5).saturating_sub(3), (gcol(i0 as f64 * 22.5) + 4).min(gw));
        let (h, w) = (r1 - r0, c1 - c0);
        let rg = cs.window(r0, r1, c0, c1, 0.0);
        let vz = zs.window(r0, r1, c0, c1, f32::NAN);
        let g1: Vec<f32> = (0..h * w).map(|i| (rg[i * 3 + 1] > rg[i * 3] + 6.0 && rg[i * 3 + 1] > rg[i * 3 + 2] + 10.0) as u8 as f32).collect();
        let greens = box_blur(&g1, h, w, 2, None).0;
        let mut cells: Vec<(usize, usize, bool)> = vec![];                        // (i, j, green)
        for i in i0..=i1 { for j in j0..=j1 {
            let (cx, cy) = ((i as f64 + 0.5) * 22.5, (j as f64 + 0.5) * 22.5);
            let (vr, vc) = (grow(cy) - r0, gcol(cx) - c0);
            if af.outside(cx, cy) == 0.0 && vz[vr * w + vc].is_finite() && !claimed[i * cny + j] { cells.push((i, j, af.grass && greens[vr * w + vc] > 0.85)); }
        } }
        if cells.is_empty() { log(&format!("airport {}: outside the data or fully overlapped, skipped", af.name)); continue; }
        let rw_custom = custom(ai) && rtex.is_some();
        // a 22.5 m cell reaches 16 m from its centre: cells closer than that (+2 m) to ANY custom runway (also of another airport
        // whose ground area overlaps) would lie in it and show photo or 3D grass
        let on_strip = |c: &(usize, usize, bool)| { let (x, y) = ((c.0 as f64 + 0.5) * 22.5, (c.1 as f64 + 0.5) * 22.5);
            strips.iter().any(|o| { let (xl, yl) = o.to_local(x, y); xl.abs() < o.wid / 2.0 + 18.0 && yl.abs() < o.len / 2.0 + 18.0 }) };
        // The G file only carries GRASS (Grass3D = photo x grass detail + 3D blades); paved areas are left to the plain terrain texture.
        if !strips.is_empty() { cells.retain(|c| !on_strip(c)); }                          // the custom surfaces cover their strips
        if cells.iter().filter(|c| c.2).count() >= 4 { cells.retain(|c| c.2); }
        else {                                                                    // no grass at all: one cell beside the runway keeps the mandatory file valid
            let key = |c: &(usize, usize, bool)| { let (xl, yl) = af.to_local((c.0 as f64 + 0.5) * 22.5, (c.1 as f64 + 0.5) * 22.5); xl.abs() + yl.abs() * 0.01 - af.hx };
            let best = *cells.iter().min_by(|a, b| key(a).partial_cmp(&key(b)).unwrap()).unwrap();
            cells = vec![best];
        }
        for c in &cells { claimed[c.0 * cny + c.1] = true; }
        let h0 = ter.height(af.x0, af.y0);
        // sub-texture: north-up orthophoto of the bounding box of those cells (u east, v south), alpha = 3D grass length
        let ci0 = cells.iter().map(|c| c.0).min().unwrap(); let ci1 = cells.iter().map(|c| c.0).max().unwrap();
        let cj0 = cells.iter().map(|c| c.1).min().unwrap(); let cj1 = cells.iter().map(|c| c.1).max().unwrap();
        let (xw0, xw1, yn0, yn1) = (ci0 as f64 * 22.5, (ci1 + 1) as f64 * 22.5, cj0 as f64 * 22.5, (cj1 + 1) as f64 * 22.5);
        let (rgb, pw, ph, [e0, e1, n0, n1]) = crop_ortho(plan, work, ai, plan.east0 - xw1, plan.east0 - xw0, plan.north0 + yn0, plan.north0 + yn1);
        let alpha: Vec<u8> = (0..pw * ph).map(|i| { let (r, g, b) = (rgb[i * 3] as i32, rgb[i * 3 + 1] as i32, rgb[i * 3 + 2] as i32); if af.grass && g > r + 6 && g > b + 10 { 110 } else { 0 } }).collect();
        let mut rw_info = "photo runway".to_string();
        let td = format!("{}/Airports/Textures/{}", out, af.name); std::fs::create_dir_all(&td).unwrap();
        write_bgra(&format!("{}/{}_Ground.dds", td, af.name), &rgb, &alpha, pw, ph);
        // ground mesh = the terrain triangles themselves
        let mut pid: std::collections::HashMap<(usize, usize), u32> = Default::default();
        let mut v: Vec<[f32; 8]> = vec![]; let mut idx: Vec<u32> = vec![];
        let mut post = |i: usize, j: usize, v: &mut Vec<[f32; 8]>| -> u32 {
            *pid.entry((i, j)).or_insert_with(|| {
                let (x, y) = (i as f64 * 22.5, j as f64 * 22.5); let (xl, yl) = af.to_local(x, y); let (e, n) = (plan.east0 - x, plan.north0 + y);
                v.push([xl as f32, yl as f32, (ter.post(i, j) as f64 - h0) as f32, 0., 0., 1., ((e - e0) / (e1 - e0)) as f32, ((n1 - n) / (n1 - n0)) as f32]);
                v.len() as u32 - 1
            })
        };
        for &(i, j, _) in &cells {
            let (a, b, c, d) = (post(i, j, &mut v), post(i + 1, j, &mut v), post(i + 1, j + 1, &mut v), post(i, j + 1, &mut v));
            let t: [[u32; 3]; 2] = if (i + j) % 2 == 0 { [[a, b, c], [a, c, d]] } else { [[a, b, d], [b, c, d]] };
            for t in t {
                let p = |k: usize| (v[t[k] as usize][0], v[t[k] as usize][1]);
                let area = (p(1).0 - p(0).0) * (p(2).1 - p(0).1) - (p(2).0 - p(0).0) * (p(1).1 - p(0).1);
                if area < 0.0 { idx.extend_from_slice(&[t[2], t[1], t[0]]); } else { idx.extend_from_slice(&t); }   // counter-clockwise in local (x, y)
            }
        }
        let ncell = cells.len();
        let gobj = Obj { name: "Grass3D".into(), texture: format!("Textures\\{}\\{}_Ground.dds", af.name, af.name), color: [1.; 4], params: [0.; 3], verts: v, idx };
        let mut gobjs = vec![gobj];
        if rw_custom {
            if let Some(b) = crate::runway::build(af, &ads[ai], &ter, h0, rtex.as_ref().unwrap(), out, ai) { gobjs.extend(b.objs); rw_info = b.info; }
        }
        std::fs::write(format!("{}/Airports/{}G.c3d", out, af.name), c3d_bytes(&gobjs)).unwrap();
        // objects: a windsock beside the runway start
        let (wx, wy) = (af.wid / 2.0 + 35.0, -af.len / 2.0 + 320.0);
        let ww = af.to_world(wx, wy); let wz = (ter.height(ww.0, ww.1) - h0) as f32; let (wx, wy) = (wx as f32, wy as f32);
        let (pv, pi) = boxmesh(wx, wy, wz, 0.25, 0.25, 8.0); let r = 0.05f32;
        let tv = vec![[wx, wy, wz + 8.0, -1., 0., 0., 0.75, 0.51], [wx - r * 0.866, wy + r * 0.5, wz + 8.0, -1., 0., 0., 0.958, 0.87], [wx - r * 0.866, wy - r * 0.5, wz + 8.0, -1., 0., 0., 0.542, 0.87]];
        let o = [Obj { name: "WindsockPole".into(), texture: String::new(), color: [0.9, 0.9, 0.9, 1.0], params: [0.5, 2.5, 0.0], verts: pv, idx: pi },
                 Obj { name: "Windsack1".into(), texture: String::new(), color: [0.8, 0.8, 0.8, 1.0], params: [0.8, 5.0, 0.0], verts: tv, idx: vec![0, 2, 1] }];
        std::fs::write(format!("{}/Airports/{}O.c3d", out, af.name), c3d_bytes(&o)).unwrap();
        let stale = format!("{}/Airports/{}.grs", out, af.name);
        if Path::new(&stale).exists() && std::fs::remove_file(&stale).is_err() { log(&format!("  note: remove the stale grass cache {} by hand", stale)); }
        recs.extend_from_slice(&apt_record(&af.name, af.lat, af.lon, h0.round(), af.heading, af.len.round() as i32, af.wid.round() as i32, af.asphalt, af.freq, af.flags));
        let (la, lo) = (af.lat.abs(), af.lon.abs());
        cup.push(format!("\"{}\",{},,{:02}{:06.3}{},{:03}{:06.3}{},{:.1}m,{},{:03},{:.1}m,{:.3},", af.name, af.name.chars().take(8).collect::<String>(),
            la as i64, (la - la.floor()) * 60.0, if af.lat < 0.0 { "S" } else { "N" }, lo as i64, (lo - lo.floor()) * 60.0, if af.lon < 0.0 { "W" } else { "E" },
            h0, if af.asphalt { 5 } else { 2 }, af.heading.round() as i64, af.len, af.freq));
        info.push(serde_json::json!({"name": af.name, "x": af.x0, "y": af.y0, "z": h0, "heading": af.heading, "length": af.len}));
        log(&format!("airport {}: heading {:.2}, {:.0} x {:.0} m, elevation {:.1} m, ground {} cells, photo {} x {} px, {}; flight plan TPPosX={:.1} TPPosY={:.1} TPPosZ={:.0}",
            af.name, af.heading, af.len, af.wid, h0, ncell, pw, ph, rw_info, af.x0, af.y0, h0));
    }
    std::fs::write(format!("{}/{}.apt", out, name), recs).unwrap();
    std::fs::write(format!("{}/{}.cup", out, name), cup.join("\r\n") + "\r\n").unwrap();
    std::fs::write(format!("{}/airports.json", work), serde_json::to_vec(&info).unwrap()).unwrap();
}
