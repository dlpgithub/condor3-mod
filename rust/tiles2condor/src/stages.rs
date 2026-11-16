//! Stages "textures", "base", "airports", "objects".
use crate::fmt::*;
use crate::glb::load_glb;
use crate::grid::*;
use crate::plan::*;
use crate::raster::{load_tile_raster, tile_frame};
use crate::{log, out_of_time, secs};
use rayon::prelude::*;
use serde_json::Value;
use std::path::Path;

/// repeatable noise with standard deviation of about 4 grey levels
fn noise(mut s: u64) -> f32 {
    s = s.wrapping_mul(0x9E3779B97F4A7C15); s ^= s >> 32; s = s.wrapping_mul(0xD6E8FEB86659FD93); s ^= s >> 32;
    let u = |k: u32| ((s >> (k * 16)) & 0xFFFF) as f32 / 65536.0;
    (u(0) + u(1) + u(2) + u(3) - 2.0) * 6.93
}
fn c8(v: f32) -> u8 { v.clamp(0.0, 255.0) as u8 }

fn fill_colours(plan: &Plan, work: &str) -> Vec<f32> {
    let (w, h) = (plan.nx * 1024, plan.ny * 1024);
    let fp = format!("{}/fillrgb.bin", work);
    if let Some(d) = read_f32(&fp) { if d.len() == h * w * 3 { return d; } }
    let (zmin, rgbg, _, _) = grid_stats(plan, work);
    let valid: Vec<bool> = zmin.iter().map(|v| v.is_finite()).collect();
    let mut f = pushpull(&rgbg, &valid, h, w, 3);
    for _ in 0..3 { for ch in 0..3 {
        let c: Vec<f32> = (0..h * w).map(|i| f[i * 3 + ch]).collect();
        let (b, _) = box_blur(&c, h, w, 16, None);
        for i in 0..h * w { if !valid[i] { f[i * 3 + ch] = b[i]; } }
    } }
    write_f32(&fp, &f);
    f
}

pub fn stage_textures(plan: &Plan, work: &str, out: &str, name: &str) -> bool {
    std::fs::create_dir_all(format!("{}/Textures", out)).unwrap();
    let (w, h) = (plan.nx * 1024, plan.ny * 1024);
    let fill = fill_colours(plan, work);
    for tx in 0..plan.nx { for ty in 0..plan.ny {
        let tp = format!("{}/Textures/t{:02}{:02}.dds", out, tx, ty);
        if Path::new(&tp).exists() { continue; }
        if out_of_time() { return false; }
        let (cw, rn) = (plan.nx - 1 - tx, plan.ny - 1 - ty);                      // tile position counted from the west / north edge
        let fg = |r: usize, c: usize, k: usize| fill[((rn * 1024 + r) * w + cw * 1024 + c) * 3 + k];
        let seed = ((tx * 1000 + ty) as u64) << 40;
        let size;
        match load_tile_raster(plan, work, tx, ty) {
            None => {                                                             // filler tile: small texture from the extended colours
                let mut img = vec![0u8; 512 * 512 * 3];
                for r in 0..512 { for c in 0..512 {
                    let n = noise(seed + (r * 512 + c) as u64);
                    for k in 0..3 { img[(r * 512 + c) * 3 + k] = c8((fg(2 * r, 2 * c, k) + fg(2 * r + 1, 2 * c, k) + fg(2 * r, 2 * c + 1, k) + fg(2 * r + 1, 2 * c + 1, k)) / 4.0 + n); }
                } }
                size = 512; write_dxt(&tp, &img, None, 512, 512);
            }
            Some((mut img, z, s)) => {                                            // where the tile has no data: extended colours with a little noise
                let f = s / 1024;
                img.par_chunks_mut(s * 3).enumerate().for_each(|(y, row)| {
                    for x in 0..s {
                        if z[y * s + x] > 0 { continue; }
                        let n = noise(seed + (y * s + x) as u64);
                        for k in 0..3 { row[x * 3 + k] = c8(fg(y / f, x / f, k) + n); }
                    }
                });
                size = s; write_dxt(&tp, &img, None, s, s);
            }
        }
        log(&format!("texture t{:02}{:02} {} px  ({:.0}s)", tx, ty, size, secs()));
    } }
    let mut mean = [0f64; 3];
    for i in 0..h * w { for k in 0..3 { mean[k] += fill[i * 3 + k] as f64; } }
    let m: Vec<u8> = mean.iter().map(|v| c8((v / (h * w) as f64) as f32)).collect();
    let e: Vec<u8> = (0..256 * 256).flat_map(|_| m.clone()).collect();
    write_dxt(&format!("{}/Textures/empty.dds", out), &e, Some(&vec![255u8; 256 * 256]), 256, 256);
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
    for r in (0..bh).rev() { for c in 0..bw {
        let mut s = [0f32; 3];
        for y in 0..16 { for x in 0..16 { for k in 0..3 { s[k] += fill[((r * 16 + y) * w + c * 16 + x) * 3 + k]; } } }
        d.extend_from_slice(&[c8(s[2] / 256.0), c8(s[1] / 256.0), c8(s[0] / 256.0), 255]);
    } }
    std::fs::write(format!("{}/{}.bmp", out, name), d).unwrap();
    true
}

pub fn stage_base(plan: &Plan, work: &str, out: &str, name: &str, trn: &[u16]) {
    std::fs::write(format!("{}/{}.ini", out, name), b"[General]\r\nVersion=1.02\r\nRealtimeShading=1").unwrap();
    std::fs::create_dir_all(format!("{}/ForestMaps", out)).unwrap();
    let zero = vec![0u8; 512 * 512]; let mut fha = String::new(); let mut keys = String::new();
    for tx in 0..plan.nx { for ty in 0..plan.ny {                                 // no generated trees: the 3D data brings its own
        std::fs::write(format!("{}/ForestMaps/{:02}{:02}.for", out, tx, ty), &zero).unwrap();
        fha.push_str(&format!("{:06} {}\r\n", tx * 1000 + ty, condor_hash(&zero, (plan.ny * tx + ty) as u32)));
        keys.push_str(&format!("{:06}\r\n", tx * 1000 + ty));
    } }
    std::fs::write(format!("{}/{}.fha", out, name), fha).unwrap();
    let fill = fill_colours(plan, work);
    let (w, tw, th) = (plan.nx * 1024, plan.nx * 64, plan.ny * 64);
    let mut therm = vec![0u8; tw * th];                                           // [y from south][x from east]; guess: vegetation 102, built-up / bare 178
    for r in 0..th { for c in 0..tw {
        let mut s = [0f32; 3];
        for y in 0..16 { for x in 0..16 { for k in 0..3 { s[k] += fill[((r * 16 + y) * w + c * 16 + x) * 3 + k]; } } }
        let green = s[1] > s[0] * 1.03 && s[1] > s[2] * 1.05;
        therm[(th - 1 - r) * tw + (tw - 1 - c)] = if green { 102 } else { 178 };
    } }
    std::fs::write(format!("{}/{}.tm3", out, name), make_tm3(trn, tw, th, &therm)).unwrap();
    if !Path::new(&format!("{}/{}.obj", out, name)).exists() { std::fs::write(format!("{}/{}.obj", out, name), b"").unwrap(); }
    std::fs::write(format!("{}/{}.air", out, name), b"").unwrap();
    std::fs::write(format!("{}/{}.aha", out, name), b"0\r\n").unwrap();
    if !Path::new(&format!("{}/{}.oha", out, name)).exists() { std::fs::write(format!("{}/{}.oha", out, name), keys).unwrap(); }
}

/// north-up orthophoto of a UTM rectangle at the finest resolution of the Condor tiles it touches (coarsened to stay below max_px);
/// returns rgb, valid, width, height and the exact bounds used
fn crop_ortho(plan: &Plan, work: &str, e0: f64, mut e1: f64, mut n0: f64, n1: f64) -> (Vec<u8>, Vec<bool>, usize, usize, [f64; 4]) {
    let txs = (((plan.east0 - e1) / TILE).floor() as i64).max(0) as usize..=(((plan.east0 - e0) / TILE).floor() as i64).min(plan.nx as i64 - 1) as usize;
    let tys = (((n0 - plan.north0) / TILE).floor() as i64).max(0) as usize..=(((n1 - plan.north0) / TILE).floor() as i64).min(plan.ny as i64 - 1) as usize;
    let smax = txs.clone().flat_map(|tx| tys.clone().map(move |ty| (tx, ty))).map(|(tx, ty)| plan.texsize(tx, ty)).max().unwrap_or(0);
    let mut res = TILE / if smax > 0 { smax as f64 } else { 1024.0 };
    while (e1 - e0) * (n1 - n0) / (res * res) > 20e6 { res *= 2.0; }
    let w = ((((e1 - e0) / res) as usize) / 4 * 4).max(4); let h = ((((n1 - n0) / res) as usize) / 4 * 4).max(4);
    e1 = e0 + w as f64 * res; n0 = n1 - h as f64 * res;
    let mut rgb = vec![0u8; w * h * 3]; let mut val = vec![false; w * h];
    for tx in txs { for ty in tys.clone() {
        let (ew, nn, s, rt, _, _) = tile_frame(plan, tx, ty);
        if s == 0 { continue; }
        let cols: Vec<(usize, usize)> = (0..w).filter_map(|c| { let e = e0 + (c as f64 + 0.5) * res; if e >= ew && e < ew + TILE { Some((c, (((e - ew) / rt) as usize).min(s - 1))) } else { None } }).collect();
        let rows: Vec<(usize, usize)> = (0..h).filter_map(|r| { let n = n1 - (r as f64 + 0.5) * res; if n <= nn && n > nn - TILE { Some((r, (((nn - n) / rt) as usize).min(s - 1))) } else { None } }).collect();
        if cols.is_empty() || rows.is_empty() { continue; }
        let Some((c, z, _)) = load_tile_raster(plan, work, tx, ty) else { continue };
        for &(r, sr) in &rows { for &(cc, sc) in &cols {
            rgb[(r * w + cc) * 3..(r * w + cc) * 3 + 3].copy_from_slice(&c[(sr * s + sc) * 3..(sr * s + sc) * 3 + 3]);
            val[r * w + cc] = z[sr * s + sc] > 0;
        } }
    } }
    (rgb, val, w, h, [e0, e1, n0, n1])
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
    let afs = airfields(cfg, plan);
    std::fs::create_dir_all(format!("{}/Airports/Textures", out)).unwrap();
    let ter = Terrain::load(plan, out);
    let (gw, gh) = (plan.nx * 1024, plan.ny * 1024);
    let valid = match std::fs::read(format!("{}/valid.bin", work)) { Ok(d) if d.len() == gw * gh => d, _ => { eprintln!("valid.bin is missing in the work folder: run the terrain stage first"); std::process::exit(1) } };
    let (_, rg, _, _) = grid_stats(plan, work);                                  // greenness per G cell, then "mostly green" over each 22.5 m cell
    let g1: Vec<f32> = (0..gw * gh).map(|i| (rg[i * 3 + 1] > rg[i * 3] + 6.0 && rg[i * 3 + 1] > rg[i * 3 + 2] + 10.0) as u8 as f32).collect();
    let greens = box_blur(&g1, gh, gw, 2, None).0;
    let cny = ter.ny - 1;
    let mut claimed = vec![false; (ter.nx - 1) * cny];
    let mut recs: Vec<u8> = vec![]; let mut cup = vec!["name,code,country,lat,lon,elev,style,rwdir,rwlen,freq,desc".to_string()]; let mut info = vec![];
    for af in &afs {
        // cells of the 22.5 m terrain grid whose centre lies in the airport's ground rectangle (and has data, and is not taken by an earlier airport)
        let cs: Vec<(f64, f64)> = [(-1., -1.), (-1., 1.), (1., -1.), (1., 1.)].iter().map(|(sx, sy)| af.to_world(sx * af.hx, sy * af.hy)).collect();
        let fmin = |f: fn(&(f64, f64)) -> f64| cs.iter().map(f).fold(f64::MAX, f64::min); let fmax = |f: fn(&(f64, f64)) -> f64| cs.iter().map(f).fold(f64::MIN, f64::max);
        let i0 = ((fmin(|c| c.0) / 22.5).floor() as i64).max(0); let i1 = ((fmax(|c| c.0) / 22.5).floor() as i64 + 1).min(ter.nx as i64 - 2);
        let j0 = ((fmin(|c| c.1) / 22.5).floor() as i64).max(0); let j1 = ((fmax(|c| c.1) / 22.5).floor() as i64 + 1).min(ter.ny as i64 - 2);
        let mut cells: Vec<(usize, usize, bool)> = vec![];                        // (i, j, green)
        for i in i0..=i1 { for j in j0..=j1 {
            let (i, j) = (i as usize, j as usize); let (cx, cy) = ((i as f64 + 0.5) * 22.5, (j as f64 + 0.5) * 22.5);
            let vr = (((plan.ny as f64 * TILE - cy) / G) as i64).clamp(0, gh as i64 - 1) as usize; let vc = (((plan.nx as f64 * TILE - cx) / G) as i64).clamp(0, gw as i64 - 1) as usize;
            if af.outside(cx, cy) == 0.0 && valid[vr * gw + vc] != 0 && !claimed[i * cny + j] { cells.push((i, j, af.grass && greens[vr * gw + vc] > 0.85)); }
        } }
        if cells.is_empty() { log(&format!("airport {}: outside the data or fully overlapped, skipped", af.name)); continue; }
        // The G file only carries GRASS (Grass3D = photo x grass detail + 3D blades). Paved areas are left to the plain terrain texture:
        // drawn through Grass3D they come out bleached and grainy. A cell counts as grass when most of its orthophoto pixels are green.
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
        let (rgb, _, pw, ph, [e0, e1, n0, n1]) = crop_ortho(plan, work, plan.east0 - xw1, plan.east0 - xw0, plan.north0 + yn0, plan.north0 + yn1);
        let alpha: Vec<u8> = (0..pw * ph).map(|i| { let (r, g, b) = (rgb[i * 3] as i32, rgb[i * 3 + 1] as i32, rgb[i * 3 + 2] as i32); if af.grass && g > r + 6 && g > b + 10 { 110 } else { 0 } }).collect();
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
        std::fs::write(format!("{}/Airports/{}G.c3d", out, af.name), c3d_bytes(&[gobj])).unwrap();
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
        log(&format!("airport {}: heading {:.2}, {:.0} x {:.0} m, elevation {:.1} m, ground {} cells, photo {} x {} px; flight plan TPPosX={:.1} TPPosY={:.1} TPPosZ={:.0}",
            af.name, af.heading, af.len, af.wid, h0, ncell, pw, ph, af.x0, af.y0, h0));
    }
    std::fs::write(format!("{}/{}.apt", out, name), recs).unwrap();
    std::fs::write(format!("{}/{}.cup", out, name), cup.join("\r\n") + "\r\n").unwrap();
    std::fs::write(format!("{}/airports.json", work), serde_json::to_vec(&info).unwrap()).unwrap();
}

const CELL: usize = 256;  // atlas cell [px]
const PAGE: usize = 2048; // atlas page [px] (8 x 8 cells)

struct Atlas { pages: Vec<Vec<u8>>, grid: Vec<bool> }
impl Atlas {
    /// put a texture into the atlas; returns (page, x0, y0, w, h) in pixels
    fn place(&mut self, tex: &crate::glb::Tex) -> (usize, usize, usize, usize, usize) {
        let small;
        let t = if tex.w > PAGE || tex.h > PAGE {
            let im = image::RgbImage::from_raw(tex.w as u32, tex.h as u32, tex.d.clone()).unwrap();
            let r = image::imageops::resize(&im, tex.w.min(PAGE) as u32, tex.h.min(PAGE) as u32, image::imageops::FilterType::Triangle);
            small = crate::glb::Tex { w: r.width() as usize, h: r.height() as usize, d: r.into_raw() }; &small
        } else { tex };
        let (w, h) = (t.w, t.h); let (cw, ch) = ((w + CELL - 1) / CELL, (h + CELL - 1) / CELL); let n = PAGE / CELL;
        loop {
            if !self.pages.is_empty() {
                for gy in 0..=n - ch { for gx in 0..=n - cw {
                    if (gy..gy + ch).any(|y| (gx..gx + cw).any(|x| self.grid[y * n + x])) { continue; }
                    for y in gy..gy + ch { for x in gx..gx + cw { self.grid[y * n + x] = true; } }
                    let pg = self.pages.last_mut().unwrap();
                    for y in 0..ch * CELL {                                        // pad to the cell with edge pixels
                        let sy = y.min(h - 1);
                        for x in 0..cw * CELL {
                            let sx = x.min(w - 1); let o = ((gy * CELL + y) * PAGE + gx * CELL + x) * 3;
                            pg[o..o + 3].copy_from_slice(&t.d[(sy * w + sx) * 3..(sy * w + sx) * 3 + 3]);
                        }
                    }
                    return (self.pages.len() - 1, gx * CELL, gy * CELL, w, h);
                } }
            }
            self.pages.push(vec![0u8; PAGE * PAGE * 3]); self.grid = vec![false; n * n];
        }
    }
}

/// Everything that rises above the ground model (buildings, trees, bridges) is kept as textured mesh, one Autogen/oXXYY.c3d per Condor tile
/// (coordinates relative to the tile centre: x west, y north, z = altitude). Textures are packed into atlas pages; <name>.oha lists the MD5s.
pub fn stage_objects(plan: &Plan, cfg: &Value, out: &str, name: &str) -> bool {
    let oc = &cfg["objects"];
    if oc.as_bool() == Some(false) || oc["enabled"].as_bool() == Some(false) { return true; }
    let min_h = crate::cf(oc, "min_height", 2.5); let sink = crate::cf(oc, "sink", 1.5); let min_level = crate::cf(oc, "min_level", 0.0) as i32;
    let ter = Terrain::load(plan, out); let ad = format!("{}/Autogen", out);
    std::fs::create_dir_all(format!("{}/Textures", ad)).unwrap();
    let mut groups: std::collections::BTreeMap<(i64, i64), Vec<usize>> = Default::default();
    for (k, t) in plan.tiles.iter().enumerate() {                                 // every source tile belongs to the Condor tile of its centre
        if t.hidden >= 1.0 || t.level < min_level { continue; }                   // fully covered by finer tiles / too coarse for 3D objects
        let x = plan.east0 - (t.e0 + t.e1) / 2.0; let y = (t.n0 + t.n1) / 2.0 - plan.north0;
        groups.entry(((x / TILE).floor() as i64, (y / TILE).floor() as i64)).or_default().push(k);
    }
    for ((tx, ty), src) in &groups {
        let (tx, ty) = (*tx, *ty);
        let fnm = format!("{}/o{:02}{:02}.c3d", ad, tx, ty);
        if Path::new(&fnm).exists() || tx < 0 || ty < 0 || tx >= plan.nx as i64 || ty >= plan.ny as i64 { continue; }
        if out_of_time() { return false; }
        let (cx, cy) = ((tx as f64 + 0.5) * TILE, (ty as f64 + 0.5) * TILE);
        let mut atlas = Atlas { pages: vec![], grid: vec![] };
        let mut meshes: std::collections::BTreeMap<usize, (Vec<[f32; 8]>, Vec<[u32; 3]>)> = Default::default();
        let (mut nt, mut flip_votes) = (0usize, 0i64);
        for chunk in src.chunks(64) {
            // decode and filter the tiles of a chunk in parallel, then place them into the atlas one by one
            let parts: Vec<Vec<(Vec<[f64; 3]>, Vec<[f32; 2]>, Vec<[u32; 3]>, crate::glb::Tex)>> = chunk.par_iter().map(|&k| {
                let t = &plan.tiles[k]; let mk = plan.masks[k].as_ref(); let mut res = vec![];
                for pr in load_glb(&t.glb, &t.mat()) {
                    let Some(tex) = pr.tex else { continue };
                    if pr.tri.is_empty() { continue; }
                    let n = pr.ecef.len();
                    let (mut pos, mut hag, mut hu, mut hv) = (Vec::with_capacity(n), Vec::with_capacity(n), Vec::with_capacity(n), Vec::with_capacity(n));
                    for v in &pr.ecef {
                        let (lat, lon, h, e, nth) = plan.project(*v);
                        let (x, y, z) = (plan.east0 - e, nth - plan.north0, h + plan.height_offset);
                        pos.push([x, y, z]); hag.push(z - ter.height(x, y));
                        if mk.is_some() { let (a, b) = plan.hole_coords(k, lat, lon); hu.push(a); hv.push(b); }
                    }
                    let mut used = vec![false; n]; let mut kept = vec![];
                    for t3 in &pr.tri {
                        let [a, b, c] = t3.map(|i| i as usize);
                        if let Some(m) = mk { if m.hit((hu[a] + hu[b] + hu[c]) / 3.0, (hv[a] + hv[b] + hv[c]) / 3.0) { continue; } }   // drop what finer tiles replace
                        if !(hag[a].max(hag[b]).max(hag[c]) > min_h) { continue; }
                        used[a] = true; used[b] = true; used[c] = true; kept.push([a, b, c]);
                    }
                    let mut remap = vec![0u32; n]; let (mut vp, mut vuv) = (vec![], vec![]);
                    for i in 0..n { if used[i] {
                        remap[i] = vp.len() as u32;
                        let mut p = pos[i]; if hag[i] < 1.0 { p[2] -= sink; }                                                      // push the feet of walls and trunks into the ground
                        vp.push(p); vuv.push(pr.uv[i]);
                    } }
                    let tri: Vec<[u32; 3]> = kept.iter().map(|t| [remap[t[0]], remap[t[1]], remap[t[2]]]).collect();
                    if !tri.is_empty() { res.push((vp, vuv, tri, tex)); }
                }
                res
            }).collect();
            for (vp, vuv, tri, tex) in parts.into_iter().flatten() {
                let (pg, px, py, w, hh) = atlas.place(&tex);
                let (w, hh) = (w as f32, hh as f32);
                let e = meshes.entry(pg).or_default(); let base = e.0.len() as u32;
                for (p, uv) in vp.iter().zip(&vuv) {
                    let u = (px as f32 + uv[0].clamp(0.5 / w, 1.0 - 0.5 / w) * w) / PAGE as f32; let v = (py as f32 + uv[1].clamp(0.5 / hh, 1.0 - 0.5 / hh) * hh) / PAGE as f32;
                    e.0.push([(p[0] - cx) as f32, (p[1] - cy) as f32, p[2] as f32, 0., 0., 1., u, v]);
                }
                for t in &tri {
                    let (a, b, c) = (vp[t[0] as usize], vp[t[1] as usize], vp[t[2] as usize]);
                    let nz = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
                    if nz < 0.0 { flip_votes += 1 } else if nz > 0.0 { flip_votes -= 1 }                                           // roofs must face up after the mirrored axes
                    e.1.push([t[0] + base, t[1] + base, t[2] + base]);
                }
                nt += tri.len();
            }
        }
        let mut objs = vec![];
        for (pg, (v, tri)) in meshes {
            write_dxt(&format!("{}/Textures/a{:02}{:02}_{:03}.dds", ad, tx, ty, pg), &atlas.pages[pg], None, PAGE, PAGE);
            let idx: Vec<u32> = tri.iter().flat_map(|t| if flip_votes > 0 { [t[2], t[1], t[0]] } else { *t }).collect();
            objs.push(Obj { name: format!("tiles_{:03}", pg), texture: format!("Textures\\a{:02}{:02}_{:03}.dds", tx, ty, pg), color: [1.; 4], params: [0.; 3], verts: v, idx });
        }
        if objs.is_empty() { std::fs::write(&fnm, b"").unwrap(); }
        else {
            let d = c3d_bytes(&objs);
            std::fs::write(format!("{}.tmp", fnm), &d).and_then(|_| std::fs::rename(format!("{}.tmp", fnm), &fnm)).unwrap();
            log(&format!("objects o{:02}{:02}: {} source tiles, {} triangles, {} atlas pages, {:.0} MB  ({:.0}s)", tx, ty, src.len(), nt, objs.len(), d.len() as f64 / 1e6, secs()));
        }
    }
    let mut oha = String::new();
    for tx in 0..plan.nx { for ty in 0..plan.ny {
        let key = format!("{:06}", tx * 1000 + ty);
        match std::fs::read(format!("{}/o{:02}{:02}.c3d", ad, tx, ty)) { Ok(d) if !d.is_empty() => oha.push_str(&format!("{} {:x}\r\n", key, md5::compute(&d))), _ => oha.push_str(&format!("{}\r\n", key)) }
    } }
    std::fs::write(format!("{}/{}.oha", out, name), oha).unwrap();
    true
}
