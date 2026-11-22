//! Raster helpers on row-major f32 grids, the tiled "G grid" (5.625 m cells, 1024 per Condor tile, image orientation: row 0 = north edge,
//! column 0 = west edge of the landscape) and the 22.5 m terrain posts the game renders.
//!
//! Every landscape-wide operation of v1 (opening, blurs, push-pull fill) is done per Condor tile on a window with enough halo
//! from the neighbours that the tile itself comes out exactly as in a landscape-wide computation; push-pull uses a landscape-wide
//! pyramid only from level PP_LEVEL (32 x 32 per tile) up. Memory is a few tiles at a time, independent of the landscape size.
use crate::gpu::CELLS;
use crate::plan::*;
use rayon::prelude::*;
use std::collections::HashMap;
use std::sync::{Arc, Mutex};

pub const G: f64 = 5.625; // [m]
pub const PP_LEVEL: usize = 5;
pub fn smooth01(t: f64) -> f64 { let t = t.clamp(0.0, 1.0); t * t * (3.0 - 2.0 * t) }

pub fn read_f32(p: &str) -> Option<Vec<f32>> { crate::bg::read(p).map(|d| bytemuck::pod_collect_to_vec::<u8, f32>(&d)) }
pub fn write_f32(p: &str, a: &[f32]) { crate::fmt::write_atomic(p, bytemuck::cast_slice(a)); }

struct SendPtr(*mut f32);
unsafe impl Sync for SendPtr {}
impl SendPtr { fn at(&self, i: usize) -> *mut f32 { unsafe { self.0.add(i) } } }

/// zero-padded box sum with radius r along both axes, ch interleaved channels (same summation order as v1: f64 running sums,
/// first down the columns, then along the rows; columns are done in parallel strips)
fn box_sum(x: &[f32], h: usize, w: usize, ch: usize, r: usize) -> Vec<f32> {
    let wc = w * ch;
    let mut t = vec![0f32; h * wc];
    let tp = SendPtr(t.as_mut_ptr());
    const S: usize = 256;
    (0..wc.div_ceil(S)).into_par_iter().for_each(|s| {
        let (c0, c1) = (s * S, (s * S + S).min(wc));
        let mut acc = [0f64; S];
        for row in 0..r.min(h) { for c in c0..c1 { acc[c - c0] += x[row * wc + c] as f64; } }
        for row in 0..h {
            if row + r < h { for c in c0..c1 { acc[c - c0] += x[(row + r) * wc + c] as f64; } }
            if row > r { for c in c0..c1 { acc[c - c0] -= x[(row - r - 1) * wc + c] as f64; } }
            for c in c0..c1 { unsafe { *tp.at(row * wc + c) = acc[c - c0] as f32; } }   // each strip writes its own columns only
        }
    });
    let mut o = vec![0f32; h * wc];
    o.par_chunks_mut(wc).zip(t.par_chunks(wc)).for_each(|(orow, trow)| {
        for k in 0..ch {
            let mut a = 0f64;
            for c in 0..r.min(w) { a += trow[c * ch + k] as f64; }
            for c in 0..w {
                if c + r < w { a += trow[(c + r) * ch + k] as f64; }
                if c > r { a -= trow[(c - r - 1) * ch + k] as f64; }
                orow[c * ch + k] = a as f32;
            }
        }
    });
    o
}

/// normalised box blur (radius r cells); wgt = weights / validity (0..1). Returns (blurred, weight sum)
pub fn box_blur(a: &[f32], h: usize, w: usize, r: usize, wgt: Option<&[f32]>) -> (Vec<f32>, Vec<f32>) {
    match wgt {
        None => { let b = blur(a, h, w, 1, r); (b, vec![]) }
        Some(wg) => {
            let aw: Vec<f32> = a.par_iter().zip(wg).map(|(v, k)| if *k > 0.0 { v * k } else { 0.0 }).collect();
            let num = box_sum(&aw, h, w, 1, r); let den = box_sum(wg, h, w, 1, r);
            (num.par_iter().zip(&den).map(|(n, d)| if *d > 1e-6 { n / d.max(1e-6) } else { 0.0 }).collect(), den)
        }
    }
}
/// unweighted normalised box blur of ch interleaved channels (zero padding; the divisor is the number of cells inside the grid)
pub fn blur(a: &[f32], h: usize, w: usize, ch: usize, r: usize) -> Vec<f32> {
    let num = box_sum(a, h, w, ch, r);
    let cnt = |i: usize, n: usize| (i.min(r) + r.min(n - 1 - i) + 1) as f32;
    let mut o = num;
    o.par_chunks_mut(w * ch).enumerate().for_each(|(row, orow)| {
        let cr = cnt(row, h);
        for c in 0..w { let d = cr * cnt(c, w); for k in 0..ch { orow[c * ch + k] /= d; } }
    });
    o
}

/// separable sliding min (mx = false) / max (mx = true) with window 2k+1, clamped at the edges
pub fn slide(a: &[f32], h: usize, w: usize, k: usize, mx: bool) -> Vec<f32> {
    let f = |x: f32, y: f32| if mx { x.max(y) } else { x.min(y) };
    let mut t = vec![0f32; h * w];
    t.par_chunks_mut(w).enumerate().for_each(|(r, row)| {
        let (r0, r1) = (r.saturating_sub(k), (r + k).min(h - 1));
        row.copy_from_slice(&a[r0 * w..(r0 + 1) * w]);
        for rr in r0 + 1..=r1 { let s = &a[rr * w..(rr + 1) * w]; for c in 0..w { row[c] = f(row[c], s[c]); } }
    });
    let mut o = vec![0f32; h * w];
    o.par_chunks_mut(w).zip(t.par_chunks(w)).for_each(|(orow, trow)| {
        for c in 0..w {
            let (c0, c1) = (c.saturating_sub(k), (c + k).min(w - 1));
            let mut v = trow[c0];
            for cc in c0 + 1..=c1 { v = f(v, trow[cc]); }
            orow[c] = v;
        }
    });
    o
}

/// fill invalid cells from coarser and coarser averages (v1 semantics); a has ch channels per cell
pub fn pushpull(a: &[f32], valid: &[bool], h: usize, w: usize, ch: usize) -> Vec<f32> {
    let all = valid.iter().all(|&v| v);
    if all || h < 2 || w < 2 {
        let n = valid.iter().filter(|&&v| v).count();
        if n == 0 { return a.to_vec(); }
        let mut m = vec![0f64; ch];
        for i in 0..h * w { if valid[i] { for c in 0..ch { m[c] += a[i * ch + c] as f64; } } }
        let mut o = a.to_vec();
        for i in 0..h * w { if !valid[i] { for c in 0..ch { o[i * ch + c] = (m[c] / n as f64) as f32; } } }
        return o;
    }
    let (h2, w2) = ((h + 1) / 2, (w + 1) / 2);
    let (sa, lv) = down2(a, valid, h, w, ch);
    let low = pushpull(&sa, &lv, h2, w2, ch);
    let mut o = a.to_vec();
    for r in 0..h { for c in 0..w {
        let i = r * w + c;
        if !valid[i] { let q = (r / 2) * w2 + c / 2; for k in 0..ch { o[i * ch + k] = low[q * ch + k]; } }
    } }
    o
}

/// one pyramid step: average of the valid children
fn down2(a: &[f32], valid: &[bool], h: usize, w: usize, ch: usize) -> (Vec<f32>, Vec<bool>) {
    let (h2, w2) = ((h + 1) / 2, (w + 1) / 2);
    let mut sa = vec![0f32; h2 * w2 * ch]; let mut sv = vec![0f32; h2 * w2];
    for r in 0..h { for c in 0..w {
        let i = r * w + c;
        if valid[i] { let q = (r / 2) * w2 + c / 2; sv[q] += 1.0; for k in 0..ch { sa[q * ch + k] += a[i * ch + k]; } }
    } }
    for q in 0..h2 * w2 { let d = sv[q].max(1e-6); for k in 0..ch { sa[q * ch + k] /= d; } }
    (sa, sv.iter().map(|&s| s > 0.0).collect())
}

/// pyramid of one tile block (CELLS x CELLS) up to PP_LEVEL: levels[l] = (values, valid)
pub fn pyramid(a: &[f32], valid: &[bool], ch: usize) -> Vec<(Vec<f32>, Vec<bool>)> {
    let mut lv = vec![(a.to_vec(), valid.to_vec())];
    for l in 1..=PP_LEVEL { let n = CELLS >> (l - 1); let (sa, sv) = down2(&lv[l - 1].0, &lv[l - 1].1, n, n, ch); lv.push((sa, sv)); }
    lv
}
/// push-pull down through a tile's pyramid, given the filled values at PP_LEVEL
pub fn pull_down(lv: &[(Vec<f32>, Vec<bool>)], top: &[f32], ch: usize) -> Vec<f32> {
    let mut cur = top.to_vec();
    for l in (0..PP_LEVEL).rev() {
        let n = CELLS >> l; let n2 = n / 2;
        let (a, v) = &lv[l];
        let mut o = a.clone();
        for r in 0..n { for c in 0..n {
            let i = r * n + c;
            if !v[i] { let q = (r / 2) * n2 + c / 2; for k in 0..ch { o[i * ch + k] = cur[q * ch + k]; } }
        } }
        cur = o;
    }
    cur
}

// ------------------------------------------------------------------ tiled access
/// per-tile arrays of CELLS x CELLS x ch, loaded on demand and cached (cache is skipped when the landscape is large)
pub struct TileSet { pub nx: usize, pub ny: usize, pub ch: usize, path: Box<dyn Fn(usize, usize) -> String + Sync + Send>, cache: Mutex<HashMap<(usize, usize), Option<Arc<Vec<f32>>>>>, keep: bool, pub u16: bool }
impl TileSet {
    pub fn new(plan: &Plan, ch: usize, path: impl Fn(usize, usize) -> String + Sync + Send + 'static) -> TileSet {
        let keep = plan.nx * plan.ny * CELLS * CELLS * ch * 4 < 1_500_000_000;
        TileSet { nx: plan.nx, ny: plan.ny, ch, path: Box::new(path), cache: Mutex::new(HashMap::new()), keep, u16: false }
    }
    /// files hold u16 = value * 256 (the colour statistics)
    pub fn u16(mut self) -> TileSet { self.u16 = true; self }
    pub fn get(&self, tx: usize, ty: usize) -> Option<Arc<Vec<f32>>> {
        if let Some(v) = self.cache.lock().unwrap().get(&(tx, ty)) { return v.clone(); }
        let v = if self.u16 { crate::bg::read(&(self.path)(tx, ty)).map(|d| bytemuck::pod_collect_to_vec::<u8, u16>(&d).iter().map(|&q| q as f32 / 256.0).collect()) } else { read_f32(&(self.path)(tx, ty)) };
        let v = v.filter(|d: &Vec<f32>| d.len() == CELLS * CELLS * self.ch).map(Arc::new);
        if self.keep { self.cache.lock().unwrap().insert((tx, ty), v.clone()); }
        v
    }
    /// store a tile: kept in memory when the whole set fits, otherwise written to its work file
    pub fn put(&self, tx: usize, ty: usize, d: Vec<f32>) {
        if self.keep { self.cache.lock().unwrap().insert((tx, ty), Some(Arc::new(d))); } else { write_f32(&(self.path)(tx, ty), &d); }
    }
    /// remove the work files of this set
    pub fn remove_files(&self) { for tx in 0..self.nx { for ty in 0..self.ny { let _ = std::fs::remove_file((self.path)(tx, ty)); } } }
    /// global rows [r0, r1) x columns [c0, c1); cells of missing tiles get `fill`
    pub fn window(&self, r0: usize, r1: usize, c0: usize, c1: usize, fill: f32) -> Vec<f32> {
        let (w, ch) = (c1 - c0, self.ch);
        let mut o = vec![fill; (r1 - r0) * w * ch];
        let mut tr = r0;
        while tr < r1 {
            let ty = self.ny - 1 - tr / CELLS; let tre = ((tr / CELLS + 1) * CELLS).min(r1);
            let mut tc = c0;
            while tc < c1 {
                let tx = self.nx - 1 - tc / CELLS; let tce = ((tc / CELLS + 1) * CELLS).min(c1);
                if let Some(d) = self.get(tx, ty) {
                    for r in tr..tre {
                        let src = ((r % CELLS) * CELLS + tc % CELLS) * ch; let dst = ((r - r0) * w + tc - c0) * ch;
                        o[dst..dst + (tce - tc) * ch].copy_from_slice(&d[src..src + (tce - tc) * ch]);
                    }
                }
                tc = tce;
            }
            tr = tre;
        }
        o
    }
}
/// global G window of tile (tx, ty) with halo, clipped to the landscape: (r0, r1, c0, c1)
pub fn tile_window(plan: &Plan, tx: usize, ty: usize, halo: usize) -> (usize, usize, usize, usize) {
    let (r, c) = ((plan.ny - 1 - ty) * CELLS, (plan.nx - 1 - tx) * CELLS);
    (r.saturating_sub(halo), (r + CELLS + halo).min(plan.ny * CELLS), c.saturating_sub(halo), (c + CELLS + halo).min(plan.nx * CELLS))
}
/// copy the tile itself out of a window array
pub fn interior(plan: &Plan, tx: usize, ty: usize, win: &[f32], wb: (usize, usize, usize, usize), ch: usize) -> Vec<f32> {
    let (r, c) = ((plan.ny - 1 - ty) * CELLS, (plan.nx - 1 - tx) * CELLS);
    let w = wb.3 - wb.2;
    let mut o = Vec::with_capacity(CELLS * CELLS * ch);
    for rr in r..r + CELLS { let s = ((rr - wb.0) * w + c - wb.2) * ch; o.extend_from_slice(&win[s..s + CELLS * ch]); }
    o
}

/// the surface the game renders: 22.5 m posts of the .tr3f files, each cell split by the diagonal rule of the stock patches:
/// cell (i, j) with i+j even -> diagonal (i,j)-(i+1,j+1), odd -> (i+1,j)-(i,j+1)   (i counts west, j north, global post indices)
pub struct Terrain { pub nx: usize, pub ny: usize, pub p: Vec<f32> }
impl Terrain {
    pub fn load(plan: &Plan, out: &str) -> Terrain {
        let (nx, ny) = (plan.nx * 256 + 1, plan.ny * 256 + 1);
        let mut p = vec![0f32; nx * ny];
        for tx in 0..plan.nx { for ty in 0..plan.ny {
            let f = format!("{}/HeightMaps/22.5m/h{:02}{:02}.tr3f", out, tx, ty);
            let d = match read_f32(&f) { Some(d) if d.len() == 257 * 257 => d, _ => { eprintln!("{} is missing: run the terrain stage first", f); std::process::exit(1) } };
            for i in 0..257 { for j in 0..257 { p[(tx * 256 + i) * ny + ty * 256 + j] = d[i * 257 + j]; } }
        } }
        Terrain { nx, ny, p }
    }
    pub fn post(&self, i: usize, j: usize) -> f32 { self.p[i * self.ny + j] }
    pub fn height(&self, x: f64, y: f64) -> f64 {
        let fx = (x / 22.5).clamp(0.0, self.nx as f64 - 1.000001); let fy = (y / 22.5).clamp(0.0, self.ny as f64 - 1.000001);
        let (i, j) = (fx as usize, fy as usize); let (u, v) = (fx - i as f64, fy - j as f64);
        let (a, b, c, d) = (self.post(i, j) as f64, self.post(i + 1, j) as f64, self.post(i + 1, j + 1) as f64, self.post(i, j + 1) as f64);
        if (i + j) % 2 == 0 { if u >= v { a + u * (b - a) + v * (c - b) } else { a + v * (d - a) + u * (c - d) } }
        else if u + v <= 1.0 { a + u * (b - a) + v * (d - a) } else { c + (1.0 - u) * (d - c) + (1.0 - v) * (b - c) }
    }
}
