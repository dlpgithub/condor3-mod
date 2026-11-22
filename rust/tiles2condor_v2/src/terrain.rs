//! Stage "terrain": ground model on the G grid from the lowest surface per cell (stage scan), then .trn / .tr3 / .tr3f / .tha.
//! Same model as v1 (opening removes buildings and trees, two weighted blurs, extra smoothing at airports, push-pull fill of the area
//! without data, four blurs of the filler), computed per Condor tile in four sweeps:
//!   A  data tiles: opening + blurs + airports on a window with halo  -> g1_TXTY.bin, pyramid tops
//!   B  landscape-wide push-pull on the pyramid tops (32 x 32 per tile)
//!   C  every tile: push-pull down to full resolution                 -> g2_TXTY.bin
//!   D  every tile: soften the filler on a window with 48 cells halo    -> ter_TXTY.bin (final ground, image orientation)
//! then the game files are sampled from ter_* (halo 2 cells).
use crate::fmt::condor_hash;
use crate::gpu::CELLS;
use crate::grid::*;
use crate::plan::*;
use crate::{cf, log};
use rayon::prelude::*;
use serde_json::Value;

pub fn zmin_path(work: &str, tx: usize, ty: usize) -> String { format!("{}/zmin_{:02}{:02}.bin", work, tx, ty) }
fn g1_path(work: &str, tx: usize, ty: usize) -> String { format!("{}/g1_{:02}{:02}.bin", work, tx, ty) }
fn g2_path(work: &str, tx: usize, ty: usize) -> String { format!("{}/g2_{:02}{:02}.bin", work, tx, ty) }
pub fn ter_path(work: &str, tx: usize, ty: usize) -> String { format!("{}/ter_{:02}{:02}.bin", work, tx, ty) }

/// global G box (r0, r1, c0, c1) around the airport ground + 150 m (the area v1 smooths), clipped to the landscape
fn airport_box(plan: &Plan, af: &Airfield) -> (usize, usize, usize, usize) {
    let (mut r0, mut r1, mut c0, mut c1) = (f64::MAX, f64::MIN, f64::MAX, f64::MIN);
    for (sx, sy) in [(-1., -1.), (-1., 1.), (1., -1.), (1., 1.)] {
        let (x, y) = af.to_world(sx * (af.hx + 150.0), sy * (af.hy + 150.0));
        let c = (plan.nx as f64 * TILE - x) / G - 0.5; let r = (plan.ny as f64 * TILE - y) / G - 0.5;
        r0 = r0.min(r); r1 = r1.max(r); c0 = c0.min(c); c1 = c1.max(c);
    }
    let cl = |v: f64, n: usize| (v.max(0.0) as usize).min(n);
    (cl(r0.floor() - 1.0, plan.ny * CELLS), cl(r1.ceil() + 2.0, plan.ny * CELLS), cl(c0.floor() - 1.0, plan.nx * CELLS), cl(c1.ceil() + 2.0, plan.nx * CELLS))
}

/// sweep A for one data tile: returns the pyramid top (values, valid)
fn ground_tile(plan: &Plan, cfg: &Value, g1s: &TileSet, zs: &TileSet, afs: &[(Airfield, (usize, usize, usize, usize))], tx: usize, ty: usize) -> (Vec<f32>, Vec<bool>) {
    let k = ((cf(cfg, "ground_filter_m", 35.0) / G).round() as usize).max(1);
    // airports that can influence the window (conservatively: any whose box is within the largest halo)
    let hmax = 2 * k + 6 + 10 * afs.len();
    let tb = tile_window(plan, tx, ty, hmax);
    let near: Vec<&(Airfield, (usize, usize, usize, usize))> = afs.iter().filter(|(_, b)| b.0 < tb.1 && b.1 > tb.0 && b.2 < tb.3 && b.3 > tb.2).collect();
    let wb = tile_window(plan, tx, ty, 2 * k + 6 + 10 * near.len());
    let (h, w) = (wb.1 - wb.0, wb.3 - wb.2);
    let zmin = zs.window(wb.0, wb.1, wb.2, wb.3, f32::NAN);
    let vf: Vec<f32> = zmin.iter().map(|v| v.is_finite() as u8 as f32).collect();
    let z: Vec<f32> = zmin.iter().map(|v| if v.is_finite() { *v } else { 1e9 }).collect();
    let z = slide(&slide(&z, h, w, k, false), h, w, k, true);                 // opening: removes buildings/trees narrower than 2k cells
    let z: Vec<f32> = z.iter().zip(&zmin).map(|(a, b)| if b.is_finite() { a.min(*b) } else { 0.0 }).collect();
    let (z, _) = box_blur(&z, h, w, 2, Some(&vf)); let (mut z, _) = box_blur(&z, h, w, 2, Some(&vf));
    let cy = |r: usize| plan.ny as f64 * TILE - (r as f64 + 0.5) * G; let cx = |c: usize| plan.nx as f64 * TILE - (c as f64 + 0.5) * G;
    for (af, ab) in near {                                                    // airports: extra smooth ground (no bumps under the wheels)
        let (r0, r1, c0, c1) = (ab.0.max(wb.0), ab.1.min(wb.1), ab.2.max(wb.2), ab.3.min(wb.3));
        if r0 >= r1 || c0 >= c1 { continue; }
        let (sh, sw) = (r1 - r0, c1 - c0);
        let mut sub = vec![0f32; sh * sw]; let mut sv = vec![0f32; sh * sw];
        for r in 0..sh { for c in 0..sw { let i = (r0 - wb.0 + r) * w + c0 - wb.2 + c; sub[r * sw + c] = z[i]; sv[r * sw + c] = vf[i]; } }
        let (sm, _) = box_blur(&sub, sh, sw, 5, Some(&sv)); let (sm, _) = box_blur(&sm, sh, sw, 5, Some(&sv));
        for r in 0..sh { for c in 0..sw {
            let wt = ((1.0 - smooth01(af.outside(cx(c0 + c), cy(r0 + r)) / 90.0)) * sv[r * sw + c] as f64) as f32;
            if wt > 0.0 { z[(r0 - wb.0 + r) * w + c0 - wb.2 + c] = sub[r * sw + c] * (1.0 - wt) + sm[r * sw + c] * wt; }
        } }
    }
    let g1 = interior(plan, tx, ty, &z, wb, 1);
    let valid: Vec<bool> = interior(plan, tx, ty, &vf, wb, 1).iter().map(|&v| v > 0.0).collect();
    let top = pyramid(&g1, &valid, 1).swap_remove(PP_LEVEL);
    g1s.put(tx, ty, g1);
    top
}

/// the terrain stage is complete (its game files are written; resumed runs skip the stage, like v1 kept terrain.bin)
pub fn done_path(work: &str) -> String { format!("{}/terrain.done", work) }

pub fn stage_terrain(plan: &Plan, cfg: &Value, work: &str, out: &str, name: &str) -> Vec<u16> {
    let tiles: Vec<(usize, usize)> = (0..plan.nx).flat_map(|tx| (0..plan.ny).map(move |ty| (tx, ty))).collect();
    let w0 = work.to_string();
    let zs = TileSet::new(plan, 1, move |tx, ty| zmin_path(&w0, tx, ty));
    let data: Vec<(usize, usize)> = tiles.iter().copied().filter(|&(tx, ty)| plan.texsize(tx, ty) > 0).collect();
    for &(tx, ty) in &data { if !crate::bg::exists(&zmin_path(work, tx, ty)) { eprintln!("{} is missing: run the scan stage first", zmin_path(work, tx, ty)); std::process::exit(1); } }
    let w3 = work.to_string();
    let ts = TileSet::new(plan, 1, move |tx, ty| ter_path(&w3, tx, ty));
    if tiles.iter().any(|&(tx, ty)| ts.get(tx, ty).is_none()) {
        let afs: Vec<(Airfield, (usize, usize, usize, usize))> = airfields(cfg, plan).into_iter().map(|a| { let b = airport_box(plan, &a); (a, b) }).collect();
        // A
        let (w1, w2) = (work.to_string(), work.to_string());
        let g1s = TileSet::new(plan, 1, move |tx, ty| g1_path(&w1, tx, ty));
        let g2s = TileSet::new(plan, 1, move |tx, ty| g2_path(&w2, tx, ty));
        let tops: Vec<((usize, usize), (Vec<f32>, Vec<bool>))> = crate::prof("terrain.ground", || data.par_iter().map(|&(tx, ty)| ((tx, ty), ground_tile(plan, cfg, &g1s, &zs, &afs, tx, ty))).collect());
        // B
        let n = CELLS >> PP_LEVEL; let (gh, gw) = (plan.ny * n, plan.nx * n);
        let mut ga = vec![0f32; gh * gw]; let mut gv = vec![false; gh * gw];
        for ((tx, ty), (a, v)) in &tops {
            let (r0, c0) = ((plan.ny - 1 - ty) * n, (plan.nx - 1 - tx) * n);
            for r in 0..n { for c in 0..n { ga[(r0 + r) * gw + c0 + c] = a[r * n + c]; gv[(r0 + r) * gw + c0 + c] = v[r * n + c]; } }
        }
        if !gv.iter().any(|&v| v) { eprintln!("no raster data: run the scan stage first"); std::process::exit(1); }
        let gf = pushpull(&ga, &gv, gh, gw, 1);
        // C
        tiles.par_iter().for_each(|&(tx, ty)| {
            let (r0, c0) = ((plan.ny - 1 - ty) * n, (plan.nx - 1 - tx) * n);
            let top: Vec<f32> = (0..n * n).map(|i| gf[(r0 + i / n) * gw + c0 + i % n]).collect();
            let lv = match (plan.texsize(tx, ty) > 0).then(|| g1s.get(tx, ty)).flatten() {
                Some(g1) => { let zm = zs.get(tx, ty).unwrap(); let valid: Vec<bool> = zm.iter().map(|v| v.is_finite()).collect(); pyramid(&g1, &valid, 1) }
                None => (0..=PP_LEVEL).map(|l| { let m = (CELLS >> l) * (CELLS >> l); (vec![0f32; m], vec![false; m]) }).collect(),
            };
            g2s.put(tx, ty, pull_down(&lv, &top, 1));
        });
        // D
        let res: Vec<(f32, f32, usize)> = tiles.par_iter().map(|&(tx, ty)| {
            let wb = tile_window(plan, tx, ty, 48);
            let (h, w) = (wb.1 - wb.0, wb.3 - wb.2);
            let mut zf = g2s.window(wb.0, wb.1, wb.2, wb.3, 0.0);
            let valid: Vec<bool> = zs.window(wb.0, wb.1, wb.2, wb.3, f32::NAN).iter().map(|v| v.is_finite()).collect();
            if valid.iter().any(|&v| !v) {
                for _ in 0..4 {                                                   // soften the filler, keep the data
                    let (b, _) = box_blur(&zf, h, w, 12, None);
                    for i in 0..h * w { if !valid[i] { zf[i] = b[i]; } }
                }
            }
            let mut t = interior(plan, tx, ty, &zf, wb, 1);
            for v in t.iter_mut() { *v = v.max(1.0); }
            let vi = interior(plan, tx, ty, &valid.iter().map(|&v| v as u8 as f32).collect::<Vec<_>>(), wb, 1);
            let (mut lo, mut hi, mut nv) = (f32::MAX, f32::MIN, 0usize);
            for i in 0..t.len() { if vi[i] > 0.0 { lo = lo.min(t[i]); hi = hi.max(t[i]); nv += 1; } }
            ts.put(tx, ty, t);
            (lo, hi, nv)
        }).collect();
        let lo = res.iter().map(|r| r.0).fold(f32::MAX, f32::min); let hi = res.iter().map(|r| r.1).fold(f32::MIN, f32::max);
        let nv: usize = res.iter().map(|r| r.2).sum();
        log(&format!("terrain: ground {:.0}..{:.0} m, data covers {:.0}% of the landscape", lo, hi, nv as f64 * 100.0 / (tiles.len() * CELLS * CELLS) as f64));
        g1s.remove_files(); g2s.remove_files();
    }
    let trn = write_heightmaps(plan, &ts, out, name);
    crate::bg::write(done_path(work), vec![]);                                  // queued after the height maps
    trn
}

/// .tr3 (30 m integers), .tr3f (22.5 m floats, the rendered surface), .trn (90 m), .tha; returns the 90 m grid trn[x][y]
fn write_heightmaps(plan: &Plan, ts: &TileSet, out: &str, name: &str) -> Vec<u16> {
    std::fs::create_dir_all(format!("{}/HeightMaps/22.5m", out)).unwrap();
    let (tw, th) = (plan.nx * 64, plan.ny * 64);
    let (gw, gh) = (plan.nx * CELLS, plan.ny * CELLS);
    let tiles: Vec<(usize, usize)> = (0..plan.nx).flat_map(|tx| (0..plan.ny).map(move |ty| (tx, ty))).collect();
    let parts: Vec<(Vec<u16>, String)> = tiles.par_iter().map(|&(tx, ty)| {
        let wb = tile_window(plan, tx, ty, 2);
        let zg = ts.window(wb.0, wb.1, wb.2, wb.3, 0.0); let ww = wb.3 - wb.2;
        let sample = |x: f64, y: f64| -> f64 {                                   // bilinear in the G grid at world (x west, y north)
            let fc = ((plan.nx as f64 * TILE - x) / G - 0.5).clamp(0.0, gw as f64 - 1.001); let fr = ((plan.ny as f64 * TILE - y) / G - 0.5).clamp(0.0, gh as f64 - 1.001);
            let (c, r) = (fc as usize, fr as usize); let (u, v) = (fc - c as f64, fr - r as f64);
            let at = |r: usize, c: usize| zg[(r - wb.0) * ww + c - wb.2] as f64;
            at(r, c) * (1.0 - u) * (1.0 - v) + at(r, c + 1) * u * (1.0 - v) + at(r + 1, c) * (1.0 - u) * v + at(r + 1, c + 1) * u * v
        };
        let mut b3 = Vec::with_capacity(193 * 193 * 2); let mut t64 = vec![0u16; 64 * 64];
        for ix in 0..193 { for iy in 0..193 {                                    // [x][y], y fastest
            let v = sample(tx as f64 * TILE + ix as f64 * 30.0, ty as f64 * TILE + iy as f64 * 30.0).round_ties_even() as u16;
            b3.extend_from_slice(&v.to_le_bytes());
            if ix < 192 && iy < 192 && ix % 3 == 0 && iy % 3 == 0 { t64[(ix / 3) * 64 + iy / 3] = v; }
        } }
        crate::bg::write(format!("{}/HeightMaps/h{:02}{:02}.tr3", out, tx, ty), b3);
        let mut bf = Vec::with_capacity(257 * 257 * 4);
        for ix in 0..257 { for iy in 0..257 { bf.extend_from_slice(&(sample(tx as f64 * TILE + ix as f64 * 22.5, ty as f64 * TILE + iy as f64 * 22.5) as f32).to_le_bytes()); } }
        let line = format!("{:06} {}\r\n", tx * 1000 + ty, condor_hash(&bf, (plan.ny * tx + ty) as u32));
        crate::bg::write(format!("{}/HeightMaps/22.5m/h{:02}{:02}.tr3f", out, tx, ty), bf);
        (t64, line)
    }).collect();
    let mut trn = vec![0u16; tw * th]; let mut tha = String::new();
    for (i, &(tx, ty)) in tiles.iter().enumerate() {
        for a in 0..64 { for b in 0..64 { trn[(tx * 64 + a) * th + ty * 64 + b] = parts[i].0[a * 64 + b]; } }
        tha.push_str(&parts[i].1);
    }
    std::fs::write(format!("{}/{}.tha", out, name), tha).unwrap();
    let mut d = Vec::with_capacity(36 + trn.len() * 2);
    d.extend_from_slice(&(tw as u32).to_le_bytes()); d.extend_from_slice(&(th as u32).to_le_bytes());
    for v in [90.0f32, -90.0, 90.0, plan.east0 as f32, plan.north0 as f32] { d.extend_from_slice(&v.to_le_bytes()); }
    d.extend_from_slice(&(plan.zone as u32).to_le_bytes()); d.extend_from_slice(&(if plan.south { b'S' } else { b'N' } as u32).to_le_bytes());
    for v in &trn { d.extend_from_slice(&v.to_le_bytes()); }
    std::fs::write(format!("{}/{}.trn", out, name), d).unwrap();
    trn
}
