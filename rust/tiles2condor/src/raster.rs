//! Stage "raster": top-down render of the meshes into colour + height blocks, per Condor tile at its own resolution.
use crate::glb::{load_glb, Tex};
use crate::plan::*;
use crate::{log, out_of_time, secs};
use rayon::prelude::*;
use std::sync::atomic::{AtomicBool, Ordering};

pub const BLOCK: usize = 2048; // largest raster block edge in pixels

/// z-buffer render of one textured primitive (highest wins). hole = per-vertex position in the tile's rectangle + mask of the hidden parts.
#[allow(clippy::too_many_arguments)]
pub fn raster_tris(px: &[f64], py: &[f64], z: &[f64], uv: &[[f32; 2]], tri: &[[u32; 3]], tex: &Tex, w: usize, h: usize, zbuf: &mut [f32], rgb: &mut [u8], hole: Option<(&[f64], &[f64], &Mask)>) {
    for t in tri {
        let (a, b, c) = (t[0] as usize, t[1] as usize, t[2] as usize);
        let (xa, xb, xc, ya, yb, yc) = (px[a], px[b], px[c], py[a], py[b], py[c]);
        let den = (yb - yc) * (xa - xc) + (xc - xb) * (ya - yc);
        if !(den.abs() > 1e-9) { continue; }
        let x0 = (xa.min(xb).min(xc).floor() as i64).max(0); let x1 = (xa.max(xb).max(xc).ceil() as i64).min(w as i64 - 1);
        let y0 = (ya.min(yb).min(yc).floor() as i64).max(0); let y1 = (ya.max(yb).max(yc).ceil() as i64).min(h as i64 - 1);
        if x0 > x1 || y0 > y1 { continue; }
        for iy in y0..=y1 {
            let gy = iy as f64 + 0.5;
            for ix in x0..=x1 {
                let gx = ix as f64 + 0.5;
                let l0 = ((yb - yc) * (gx - xc) + (xc - xb) * (gy - yc)) / den;
                let l1 = ((yc - ya) * (gx - xc) + (xa - xc) * (gy - yc)) / den;
                let l2 = 1.0 - l0 - l1;
                if l0 < -1e-4 || l1 < -1e-4 || l2 < -1e-4 { continue; }
                let p = iy as usize * w + ix as usize;
                let zz = (l0 * z[a] + l1 * z[b] + l2 * z[c]) as f32;
                if !(zz > zbuf[p]) { continue; }
                if let Some((hu, hv, mk)) = hole {
                    if mk.hit(l0 * hu[a] + l1 * hu[b] + l2 * hu[c], l0 * hv[a] + l1 * hv[b] + l2 * hv[c]) { continue; }
                }
                let uu = l0 * uv[a][0] as f64 + l1 * uv[b][0] as f64 + l2 * uv[c][0] as f64;
                let vv = l0 * uv[a][1] as f64 + l1 * uv[b][1] as f64 + l2 * uv[c][1] as f64;
                let tr = ((vv * tex.h as f64) as i64).clamp(0, tex.h as i64 - 1) as usize;
                let tc = ((uu * tex.w as f64) as i64).clamp(0, tex.w as i64 - 1) as usize;
                let s = (tr * tex.w + tc) * 3;
                zbuf[p] = zz;
                rgb[p * 3..p * 3 + 3].copy_from_slice(&tex.d[s..s + 3]);
            }
        }
    }
}

pub fn block_path(work: &str, tx: usize, ty: usize, i: usize, j: usize) -> String { format!("{}/ras_{:02}{:02}_{}_{}.bin", work, tx, ty, i, j) }

/// (easting of the west edge, northing of the north edge, texture size, metres per pixel, block size, blocks per side) of a Condor tile
pub fn tile_frame(plan: &Plan, tx: usize, ty: usize) -> (f64, f64, usize, f64, usize, usize) {
    let s = plan.texsize(tx, ty);
    if s == 0 { return (plan.east0 - (tx + 1) as f64 * TILE, plan.north0 + (ty + 1) as f64 * TILE, 0, 0.0, 0, 0); }
    let bs = s.min(BLOCK);
    (plan.east0 - (tx + 1) as f64 * TILE, plan.north0 + (ty + 1) as f64 * TILE, s, TILE / s as f64, bs, s / bs)
}

/// every Condor tile that has data is rendered at its own resolution, in blocks (i from the west edge, j from the north edge); blocks run in parallel
pub fn stage_raster(plan: &Plan, work: &str) -> bool {
    let mut todo = vec![];
    for tx in 0..plan.nx { for ty in 0..plan.ny {
        let (_, _, s, _, _, nb) = tile_frame(plan, tx, ty);
        if s == 0 { continue; }
        for i in 0..nb { for j in 0..nb { if !std::path::Path::new(&block_path(work, tx, ty, i, j)).exists() { todo.push((tx, ty, i, j)); } } }
    } }
    let stopped = AtomicBool::new(false);
    let grey = Tex { w: 2, h: 2, d: vec![128; 12] };
    todo.par_iter().for_each(|&(tx, ty, i, j)| {
        if out_of_time() { stopped.store(true, Ordering::Relaxed); return; }
        let (ew, nn, s, res, bs, _) = tile_frame(plan, tx, ty);
        let bp = block_path(work, tx, ty, i, j);
        let e0 = ew + (i * bs) as f64 * res; let n1 = nn - (j * bs) as f64 * res; let ext = bs as f64 * res;
        let mut src: Vec<usize> = (0..plan.tiles.len()).filter(|&k| { let t = &plan.tiles[k]; t.hidden < 1.0 && t.e1 > e0 - 2.0 && t.e0 < e0 + ext + 2.0 && t.n1 > n1 - ext - 2.0 && t.n0 < n1 + 2.0 }).collect();
        src.sort_by_key(|&k| plan.tiles[k].level);
        let mut zbuf = vec![-1e9f32; bs * bs]; let mut rgb = vec![0u8; bs * bs * 3];
        for &k in &src {
            let t = &plan.tiles[k]; let mk = plan.masks[k].as_ref();
            for pr in load_glb(&t.glb, &t.mat()) {
                let n = pr.ecef.len();
                let (mut px, mut py, mut z, mut hu, mut hv) = (Vec::with_capacity(n), Vec::with_capacity(n), Vec::with_capacity(n), Vec::with_capacity(n), Vec::with_capacity(n));
                for v in &pr.ecef {
                    let (lat, lon, hh, e, nth) = plan.project(*v);
                    px.push((e - e0) / res); py.push((n1 - nth) / res); z.push(hh + plan.height_offset);
                    if mk.is_some() { let (a, b) = plan.hole_coords(k, lat, lon); hu.push(a); hv.push(b); }
                }
                raster_tris(&px, &py, &z, &pr.uv, &pr.tri, pr.tex.as_ref().unwrap_or(&grey), bs, bs, &mut zbuf, &mut rgb, mk.map(|m| (hu.as_slice(), hv.as_slice(), m)));
            }
        }
        let mut out = rgb; out.reserve(bs * bs * 2);
        let mut cov = 0usize;
        for &zv in &zbuf {
            let q: u16 = if zv > -1e8 { cov += 1; ((zv + 500.0) * 10.0).round_ties_even().clamp(1.0, 65535.0) as u16 } else { 0 };
            out.extend_from_slice(&q.to_le_bytes());
        }
        std::fs::write(format!("{}.tmp", bp), &out).expect("cannot write raster block");
        std::fs::rename(format!("{}.tmp", bp), &bp).expect("cannot write raster block");
        log(&format!("raster tile {:02}{:02} ({} px) block {},{}: {} source tiles, coverage {:.0}%  ({:.0}s)", tx, ty, s, i, j, src.len(), cov as f64 * 100.0 / (bs * bs) as f64, secs()));
    });
    !stopped.load(Ordering::Relaxed)
}

/// assembled raster of a Condor tile: rgb (S*S*3), z (S*S; 0 = no data, else (height+500)*10), S; None when the tile has no data
pub fn load_tile_raster(plan: &Plan, work: &str, tx: usize, ty: usize) -> Option<(Vec<u8>, Vec<u16>, usize)> {
    let (_, _, s, _, bs, nb) = tile_frame(plan, tx, ty);
    if s == 0 { return None; }
    let mut rgb = vec![0u8; s * s * 3]; let mut z = vec![0u16; s * s];
    for i in 0..nb { for j in 0..nb {
        let bp = block_path(work, tx, ty, i, j);
        let d = match std::fs::read(&bp) { Ok(d) if d.len() == bs * bs * 5 => d, _ => { eprintln!("raster block {} is missing: run the raster stage first", bp); std::process::exit(1) } };
        for r in 0..bs {
            let o = ((j * bs + r) * s + i * bs) * 3;
            rgb[o..o + bs * 3].copy_from_slice(&d[r * bs * 3..(r + 1) * bs * 3]);
            let zo = (j * bs + r) * s + i * bs; let so = bs * bs * 3 + r * bs * 2;
            for c in 0..bs { z[zo + c] = u16::from_le_bytes([d[so + c * 2], d[so + c * 2 + 1]]); }
        }
    } }
    Some((rgb, z, s))
}
