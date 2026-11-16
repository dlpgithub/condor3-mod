//! Small raster helpers on row-major f32 grids, grid statistics, and the terrain stage.
use crate::fmt::condor_hash;
use crate::plan::*;
use crate::raster::load_tile_raster;
use crate::{cf, log};
use rayon::prelude::*;
use serde_json::Value;

pub const G: f64 = 5.625; // working grid for terrain and colour statistics [m] (1024 cells per Condor tile)
pub fn smooth01(t: f64) -> f64 { let t = t.clamp(0.0, 1.0); t * t * (3.0 - 2.0 * t) }

pub fn read_f32(p: &str) -> Option<Vec<f32>> { std::fs::read(p).ok().map(|d| d.chunks_exact(4).map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]])).collect()) }
pub fn write_f32(p: &str, a: &[f32]) {
    let mut b = Vec::with_capacity(a.len() * 4);
    for v in a { b.extend_from_slice(&v.to_le_bytes()); }
    std::fs::write(format!("{}.tmp", p), &b).and_then(|_| std::fs::rename(format!("{}.tmp", p), p)).expect("cannot write work file");
}

/// zero-padded box sum with radius r along both axes
fn box_sum(x: &[f32], h: usize, w: usize, r: usize) -> Vec<f32> {
    let mut t = vec![0f32; h * w];
    // along columns (axis 0)
    let mut acc = vec![0f64; w];
    for row in 0..r.min(h) { for c in 0..w { acc[c] += x[row * w + c] as f64; } }
    for row in 0..h {
        if row + r < h { for c in 0..w { acc[c] += x[(row + r) * w + c] as f64; } }
        if row > r { for c in 0..w { acc[c] -= x[(row - r - 1) * w + c] as f64; } }
        for c in 0..w { t[row * w + c] = acc[c] as f32; }
    }
    // along rows (axis 1)
    let mut o = vec![0f32; h * w];
    o.par_chunks_mut(w).zip(t.par_chunks(w)).for_each(|(orow, trow)| {
        let mut a = 0f64;
        for c in 0..r.min(w) { a += trow[c] as f64; }
        for c in 0..w {
            if c + r < w { a += trow[c + r] as f64; }
            if c > r { a -= trow[c - r - 1] as f64; }
            orow[c] = a as f32;
        }
    });
    o
}

/// normalised box blur (radius r cells); wgt = weights / validity (0..1). Returns (blurred, weight sum)
pub fn box_blur(a: &[f32], h: usize, w: usize, r: usize, wgt: Option<&[f32]>) -> (Vec<f32>, Vec<f32>) {
    let ones;
    let wg = match wgt { Some(x) => x, None => { ones = vec![1f32; h * w]; &ones } };
    let aw: Vec<f32> = a.iter().zip(wg).map(|(v, k)| if *k > 0.0 { v * k } else { 0.0 }).collect();
    let num = box_sum(&aw, h, w, r); let den = box_sum(wg, h, w, r);
    (num.iter().zip(&den).map(|(n, d)| if *d > 1e-6 { n / d.max(1e-6) } else { 0.0 }).collect(), den)
}

/// separable sliding min (mx = false) / max (mx = true) with window 2k+1
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

/// fill invalid cells from coarser and coarser averages; a has ch channels per cell
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
    let mut sa = vec![0f32; h2 * w2 * ch]; let mut sv = vec![0f32; h2 * w2];
    for r in 0..h { for c in 0..w {
        let i = r * w + c;
        if valid[i] { let q = (r / 2) * w2 + c / 2; sv[q] += 1.0; for k in 0..ch { sa[q * ch + k] += a[i * ch + k]; } }
    } }
    for q in 0..h2 * w2 { let d = sv[q].max(1e-6); for k in 0..ch { sa[q * ch + k] /= d; } }
    let lv: Vec<bool> = sv.iter().map(|&s| s > 0.0).collect();
    let low = pushpull(&sa, &lv, h2, w2, ch);
    let mut o = a.to_vec();
    for r in 0..h { for c in 0..w {
        let i = r * w + c;
        if !valid[i] { let q = (r / 2) * w2 + c / 2; for k in 0..ch { o[i * ch + k] = low[q * ch + k]; } }
    } }
    o
}

/// per G-cell statistics of the rasters, image orientation (row 0 = north edge, column 0 = west edge): zmin (NaN = no data), mean rgb
pub fn grid_stats(plan: &Plan, work: &str) -> (Vec<f32>, Vec<f32>, usize, usize) {
    let (w, h) = (plan.nx * 1024, plan.ny * 1024);
    let p = format!("{}/grid.bin", work);
    if let Some(d) = read_f32(&p) { if d.len() == h * w * 4 { return (d[..h * w].to_vec(), d[h * w..].to_vec(), h, w); } }
    let mut zmin = vec![f32::NAN; h * w]; let mut rgb = vec![0f32; h * w * 3];
    for tx in 0..plan.nx { for ty in 0..plan.ny {
        let Some((c, z, s)) = load_tile_raster(plan, work, tx, ty) else { continue };
        let f = s / 1024; let r0 = (plan.ny - 1 - ty) * 1024; let c0 = (plan.nx - 1 - tx) * 1024;
        let rows: Vec<(Vec<f32>, Vec<f32>)> = (0..1024usize).into_par_iter().map(|r| {
            let mut zr = vec![f32::NAN; 1024]; let mut cr = vec![0f32; 1024 * 3];
            for cc in 0..1024 {
                let (mut m, mut n, mut sum) = (f32::INFINITY, 0u32, [0u32; 3]);
                for y in r * f..(r + 1) * f { for x in cc * f..(cc + 1) * f {
                    let zz = z[y * s + x];
                    if zz > 0 { n += 1; m = m.min(zz as f32 / 10.0 - 500.0); for k in 0..3 { sum[k] += c[(y * s + x) * 3 + k] as u32; } }
                } }
                if n > 0 { zr[cc] = m; for k in 0..3 { cr[cc * 3 + k] = sum[k] as f32 / n as f32; } }
            }
            (zr, cr)
        }).collect();
        for (r, (zr, cr)) in rows.iter().enumerate() {
            let o = (r0 + r) * w + c0;
            zmin[o..o + 1024].copy_from_slice(zr); rgb[o * 3..(o + 1024) * 3].copy_from_slice(cr);
        }
    } }
    let mut all = zmin.clone(); all.extend_from_slice(&rgb);
    write_f32(&p, &all);
    (zmin, rgb, h, w)
}

/// ground model -> terrain.bin (G grid, image orientation), then .trn / .tr3 / .tr3f / .tha. Returns the 90 m grid trn[x][y].
pub fn stage_terrain(plan: &Plan, cfg: &Value, work: &str, out: &str, name: &str) -> Vec<u16> {
    let tp = format!("{}/terrain.bin", work);
    let (w, h) = (plan.nx * 1024, plan.ny * 1024);
    if !std::path::Path::new(&tp).exists() {
        let (zmin, _, _, _) = grid_stats(plan, work);
        let valid: Vec<bool> = zmin.iter().map(|v| v.is_finite()).collect();
        if !valid.iter().any(|&v| v) { eprintln!("no raster data: run the raster stage first"); std::process::exit(1); }
        let vf: Vec<f32> = valid.iter().map(|&v| if v { 1.0 } else { 0.0 }).collect();
        let k = ((cf(cfg, "ground_filter_m", 35.0) / G).round() as usize).max(1);
        let z: Vec<f32> = zmin.iter().map(|v| if v.is_finite() { *v } else { 1e9 }).collect();
        let z = slide(&slide(&z, h, w, k, false), h, w, k, true);              // opening: removes buildings/trees narrower than 2k cells
        let z: Vec<f32> = z.iter().zip(&zmin).map(|(a, b)| if b.is_finite() { a.min(*b) } else { 0.0 }).collect();
        let (z, _) = box_blur(&z, h, w, 2, Some(&vf)); let (mut z, _) = box_blur(&z, h, w, 2, Some(&vf));
        let cy = |r: usize| plan.ny as f64 * TILE - (r as f64 + 0.5) * G; let cx = |c: usize| plan.nx as f64 * TILE - (c as f64 + 0.5) * G;
        for af in airfields(cfg, plan) {                                        // airports: extra smooth ground (no bumps under the wheels)
            let (mut r0, mut r1, mut c0, mut c1) = (usize::MAX, 0, usize::MAX, 0);
            for r in 0..h { for c in 0..w { if af.outside(cx(c), cy(r)) < 150.0 { r0 = r0.min(r); r1 = r1.max(r); c0 = c0.min(c); c1 = c1.max(c); } } }
            if r0 == usize::MAX { continue; }
            let (sh, sw) = (r1 - r0 + 1, c1 - c0 + 1);
            let mut sub = vec![0f32; sh * sw]; let mut sv = vec![0f32; sh * sw];
            for r in 0..sh { for c in 0..sw { sub[r * sw + c] = z[(r0 + r) * w + c0 + c]; sv[r * sw + c] = vf[(r0 + r) * w + c0 + c]; } }
            let (sm, _) = box_blur(&sub, sh, sw, 5, Some(&sv)); let (sm, _) = box_blur(&sm, sh, sw, 5, Some(&sv));
            for r in 0..sh { for c in 0..sw {
                let wt = ((1.0 - smooth01(af.outside(cx(c0 + c), cy(r0 + r)) / 90.0)) * sv[r * sw + c] as f64) as f32;
                z[(r0 + r) * w + c0 + c] = sub[r * sw + c] * (1.0 - wt) + sm[r * sw + c] * wt;
            } }
        }
        let mut zf = pushpull(&z, &valid, h, w, 1);
        for _ in 0..4 {                                                          // soften the filler, keep the data
            let (b, _) = box_blur(&zf, h, w, 12, None);
            for i in 0..h * w { if !valid[i] { zf[i] = b[i]; } }
        }
        for v in zf.iter_mut() { *v = v.max(1.0); }
        let (mut lo, mut hi, mut n) = (f32::MAX, f32::MIN, 0usize);
        for i in 0..h * w { if valid[i] { lo = lo.min(zf[i]); hi = hi.max(zf[i]); n += 1; } }
        write_f32(&tp, &zf);
        std::fs::write(format!("{}/valid.bin", work), valid.iter().map(|&v| v as u8).collect::<Vec<u8>>()).unwrap();
        log(&format!("terrain: ground {:.0}..{:.0} m, data covers {:.0}% of the landscape", lo, hi, n as f64 * 100.0 / (h * w) as f64));
    }
    let zg = read_f32(&tp).expect("terrain.bin");
    let sample = |x: f64, y: f64| -> f64 {                                       // bilinear in the G grid at world (x west, y north)
        let fc = ((plan.nx as f64 * TILE - x) / G - 0.5).clamp(0.0, w as f64 - 1.001); let fr = ((plan.ny as f64 * TILE - y) / G - 0.5).clamp(0.0, h as f64 - 1.001);
        let (c, r) = (fc as usize, fr as usize); let (u, v) = (fc - c as f64, fr - r as f64);
        zg[r * w + c] as f64 * (1.0 - u) * (1.0 - v) + zg[r * w + c + 1] as f64 * u * (1.0 - v) + zg[(r + 1) * w + c] as f64 * (1.0 - u) * v + zg[(r + 1) * w + c + 1] as f64 * u * v
    };
    std::fs::create_dir_all(format!("{}/HeightMaps/22.5m", out)).unwrap();
    let (tw, th) = (plan.nx * 64, plan.ny * 64);
    let mut trn = vec![0u16; tw * th]; let mut tha = String::new();
    for tx in 0..plan.nx { for ty in 0..plan.ny {
        let mut b3 = Vec::with_capacity(193 * 193 * 2);
        for ix in 0..193 { for iy in 0..193 {                                    // [x][y], y fastest
            let v = sample(tx as f64 * TILE + ix as f64 * 30.0, ty as f64 * TILE + iy as f64 * 30.0).round_ties_even() as u16;
            b3.extend_from_slice(&v.to_le_bytes());
            if ix < 192 && iy < 192 && ix % 3 == 0 && iy % 3 == 0 { trn[(tx * 64 + ix / 3) * th + ty * 64 + iy / 3] = v; }
        } }
        std::fs::write(format!("{}/HeightMaps/h{:02}{:02}.tr3", out, tx, ty), &b3).unwrap();
        let mut bf = Vec::with_capacity(257 * 257 * 4);
        for ix in 0..257 { for iy in 0..257 { bf.extend_from_slice(&(sample(tx as f64 * TILE + ix as f64 * 22.5, ty as f64 * TILE + iy as f64 * 22.5) as f32).to_le_bytes()); } }   // float posts: rendered terrain
        std::fs::write(format!("{}/HeightMaps/22.5m/h{:02}{:02}.tr3f", out, tx, ty), &bf).unwrap();
        tha.push_str(&format!("{:06} {}\r\n", tx * 1000 + ty, condor_hash(&bf, (plan.ny * tx + ty) as u32)));
    } }
    std::fs::write(format!("{}/{}.tha", out, name), tha).unwrap();
    let mut d = Vec::with_capacity(36 + trn.len() * 2);
    d.extend_from_slice(&(tw as u32).to_le_bytes()); d.extend_from_slice(&(th as u32).to_le_bytes());
    for v in [90.0f32, -90.0, 90.0, plan.east0 as f32, plan.north0 as f32] { d.extend_from_slice(&v.to_le_bytes()); }
    d.extend_from_slice(&(plan.zone as u32).to_le_bytes()); d.extend_from_slice(&(if plan.south { b'S' } else { b'N' } as u32).to_le_bytes());
    for v in &trn { d.extend_from_slice(&v.to_le_bytes()); }
    std::fs::write(format!("{}/{}.trn", out, name), d).unwrap();
    trn
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
        if (i + j) % 2 == 0 { if u >= v { a + u * (b - a) + v * (c - b) } else { a + v * (d - a) + u * (c - d) } }             // diagonal a-c
        else if u + v <= 1.0 { a + u * (b - a) + v * (d - a) } else { c + (1.0 - u) * (d - c) + (1.0 - v) * (b - c) }       // diagonal b-d
    }
}
