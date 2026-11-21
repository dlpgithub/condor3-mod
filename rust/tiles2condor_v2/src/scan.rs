//! Stage "scan": geometry only (no texture decoding) -> lowest surface per 5.625 m cell of every data tile (zmin_TXTY.bin).
//! Rendered at the tile's texture resolution, so the ground model sees the same detail as the photo.
use crate::gpu::*;
use crate::plan::*;
use crate::src::{self, Prim, Tex};
use crate::terrain::zmin_path;
use crate::{log, out_of_time, secs};
use rayon::prelude::*;

/// source tiles decoded and drawn together
pub const CHUNK: usize = 96;

/// vertices of loaded source tiles in the pixel frame of a Condor tile; also the list of textures as (tile in chunk, primitive)
pub fn batch(plan: &Plan, f: &Frame, midx: &[(u32, u32)], chunk: &[(usize, Vec<Prim>)]) -> (Batch, Vec<(usize, usize)>) {
    // every primitive converted in parallel, then joined with index offsets
    let items: Vec<(usize, usize, &Prim)> = chunk.iter().enumerate().flat_map(|(ci, (_, prims))| prims.iter().enumerate().filter(|(_, p)| !p.tri.is_empty()).map(move |(pi, p)| (ci, pi, p))).collect();
    let parts: Vec<Vec<V>> = items.par_iter().map(|&(ci, _, pr)| {
        let m = midx[chunk[ci].0];
        pr.p.iter().enumerate().map(|(i, p)| {
            let px = ((plan.east0 - p[0]) - f.ew) / f.res; let py = (f.nn - (p[1] + plan.north0)) / f.res;
            V { p: [px as f32, py as f32, p[2] as f32], uv: pr.uv.get(i).copied().unwrap_or([0.0; 2]), hc: pr.hc.get(i).copied().unwrap_or([0.0; 2]), m: [m.0, m.1] }
        }).collect()
    }).collect();
    let nv: usize = parts.iter().map(|p| p.len()).sum(); let ni: usize = items.iter().map(|i| i.2.tri.len() * 3).sum();
    let mut b = Batch { v: Vec::with_capacity(nv), idx: Vec::with_capacity(ni), draws: vec![] }; let mut texs = vec![];
    for (&(ci, pi, pr), vs) in items.iter().zip(parts) {
        let base = b.v.len() as u32; let st = b.idx.len() as u32;
        b.v.extend_from_slice(&vs);
        for t in &pr.tri { b.idx.extend_from_slice(&[t[0] + base, t[1] + base, t[2] + base]); }
        let t = pr.tex.as_ref().map(|_| { texs.push((ci, pi)); texs.len() as u32 - 1 });
        b.draws.push((st, pr.tri.len() as u32 * 3, t));
    }
    (b, texs)
}

/// load the source tiles of a Condor tile in chunks of CHUNK and hand each chunk to `f` (chunk number, tiles, prepared batch, textures).
/// Reading, decoding and preparing (GPU upload) of the next chunk run on other threads while `f` works on the current one.
/// Only the first `draw_n` tiles of `srcs` are drawn (the rest are loaded for the distant autogen sets only).
pub fn prefetched(plan: &Plan, dev: &Dev, f: &Frame, srcs: &[usize], draw_n: usize, full: bool, mut work: impl FnMut(usize, &[(usize, Vec<Prim>)], &Prep, &[&Tex])) {
    let (_, midx) = plan.mask_bits();
    let midx = &midx;
    let chunks: Vec<&[usize]> = srcs.chunks(CHUNK).collect();
    let dec = move |c: &[usize], start: usize| {
        let loaded: Vec<(usize, Vec<Prim>)> = c.par_iter().map(|&k| (k, src::load(plan, k, full))).collect();
        let nd = draw_n.saturating_sub(start).min(loaded.len());          // the drawn tiles come first in srcs
        let (b, tl) = batch(plan, f, midx, &loaded[..nd]);
        let prep = { let texs: Vec<&Tex> = tl.iter().map(|&(i, j)| loaded[i].1[j].tex.as_ref().unwrap()).collect(); dev.prepare(b, &texs, full) };
        (loaded, tl, prep)
    };
    std::thread::scope(|sc| {
        let mut next = chunks.first().map(|&c| sc.spawn(move || dec(c, 0)));
        for ci in 0..chunks.len() {
            let (loaded, tl, prep) = crate::prof("decode.wait", || next.take().unwrap().join().expect("decoding failed"));
            if ci + 1 < chunks.len() { let c = chunks[ci + 1]; next = Some(sc.spawn(move || dec(c, (ci + 1) * CHUNK))); }
            let texs: Vec<&Tex> = tl.iter().map(|&(i, j)| loaded[i].1[j].tex.as_ref().unwrap()).collect();
            work(ci, &loaded, &prep, &texs);
        }
    });
}

pub fn stage_scan(plan: &Plan, dev: &Dev, work: &str) -> bool {
    for tx in 0..plan.nx { for ty in 0..plan.ny {
        let f = plan.frame(tx, ty);
        if f.s == 0 || crate::bg::exists(&zmin_path(work, tx, ty)) { continue; }
        if out_of_time() { return false; }
        let t0 = secs();
        let srcs = plan.sources_of(tx, ty);
        let mut cv = dev.canvas(f.s, false);
        let mut ntri = 0usize;
        prefetched(plan, dev, &f, &srcs, srcs.len(), false, |_, _, prep, texs| { ntri += prep.tris(); cv.draw(prep, texs); });
        let (st, cov) = cv.stats();
        let z: Vec<f32> = st.iter().map(|s| if s[0] >= NONE { f32::NAN } else { s[0] }).collect();
        crate::bg::write(zmin_path(work, tx, ty), bytemuck::cast_slice(&z).to_vec());
        log(&format!("scan tile {:02}{:02} ({} px): {} source tiles, {:.1} M triangles, coverage {:.0}%  ({:.1}s)", tx, ty, f.s, srcs.len(), ntri as f64 / 1e6,
            cov as f64 * 100.0 / (f.s * f.s) as f64, secs() - t0));
    } }
    true
}
