//! Condor file formats: DDS (DXT1/DXT3 with mips, BGRA), C3D meshes, .apt records, tile hash, thermal map. See docs/TERRAIN_FORMAT.md.
use rayon::prelude::*;

/// Condor 3 tile hash (.tha / .fha)
pub fn condor_hash(data: &[u8], idx: u32) -> u64 {
    let s = (data.iter().map(|&b| b as u64).sum::<u64>() + data.len() as u64) & 0xFFFF_FFFF;
    let seed = idx.wrapping_add(0x14A68F2).wrapping_mul(0x41C64E6D).wrapping_add(1);
    (s as f64 * (seed as f64 / 4294967296.0)).round_ties_even() as u64
}

fn dds_header(w: usize, h: usize, fourcc: &[u8; 4], mips: u32) -> Vec<u8> {
    let bs = if fourcc == b"DXT1" { 8 } else { 16 };
    let mut d = b"DDS ".to_vec();
    for v in [124u32, 0xA1007, h as u32, w as u32, ((w / 4).max(1) * (h / 4).max(1) * bs) as u32, 0, mips] { d.extend_from_slice(&v.to_le_bytes()); }
    d.extend_from_slice(&[0u8; 44]);
    d.extend_from_slice(&32u32.to_le_bytes()); d.extend_from_slice(&4u32.to_le_bytes()); d.extend_from_slice(fourcc); d.extend_from_slice(&[0u8; 20]);
    for v in [0x401008u32, 0, 0, 0, 0] { d.extend_from_slice(&v.to_le_bytes()); }
    d
}

fn to565(c: [i32; 3]) -> u32 { ((((c[0] * 31 + 127) / 255) << 11) | (((c[1] * 63 + 127) / 255) << 5) | ((c[2] * 31 + 127) / 255)) as u32 }
fn from565(v: u32) -> [i32; 3] { [(((v >> 11) & 31) * 255 / 31) as i32, (((v >> 5) & 63) * 255 / 63) as i32, ((v & 31) * 255 / 31) as i32] }

/// one BC1 block from 16 RGB pixels: bounding-box endpoints, pixels projected on the axis (same arithmetic as tools/dds.py)
fn bc1_block(px: &[[i32; 3]; 16]) -> [u8; 8] {
    let (mut mx, mut mn) = ([0i32; 3], [255i32; 3]);
    for p in px { for k in 0..3 { mx[k] = mx[k].max(p[k]); mn[k] = mn[k].min(p[k]); } }
    let (a, b) = (to565(mx), to565(mn));
    let (c0, c1) = (a.max(b), a.min(b));
    let (p0, p1) = (from565(c0), from565(c1));
    let ax = [p0[0] - p1[0], p0[1] - p1[1], p0[2] - p1[2]];
    let den = (ax[0] * ax[0] + ax[1] * ax[1] + ax[2] * ax[2]).max(1);
    let mut bits = 0u32;
    if c0 != c1 {
        for (i, p) in px.iter().enumerate() {
            let dot = (p[0] - p1[0]) * ax[0] + (p[1] - p1[1]) * ax[1] + (p[2] - p1[2]) * ax[2];
            let t = (dot * 6 + den).div_euclid(2 * den).clamp(0, 3);
            bits |= [1u32, 3, 2, 0][t as usize] << (2 * i);
        }
    }
    [c0 as u8, (c0 >> 8) as u8, c1 as u8, (c1 >> 8) as u8, bits as u8, (bits >> 8) as u8, (bits >> 16) as u8, (bits >> 24) as u8]
}

/// one mip level -> block bytes (DXT3 when alpha is given), edge pixels repeated for sizes below 4
fn level_blocks(rgb: &[u8], alpha: Option<&[u8]>, w: usize, h: usize) -> Vec<u8> {
    let (bw, bh) = ((w + 3) / 4, (h + 3) / 4); let bs = if alpha.is_some() { 16 } else { 8 };
    let mut out = vec![0u8; bw.max(1) * bh.max(1) * bs];
    out.par_chunks_mut(bw.max(1) * bs).enumerate().for_each(|(by, row)| {
        for bx in 0..bw.max(1) {
            let mut px = [[0i32; 3]; 16]; let mut al = [0u16; 16];
            for k in 0..16 {
                let y = (by * 4 + k / 4).min(h - 1); let x = (bx * 4 + k % 4).min(w - 1); let o = (y * w + x) * 3;
                px[k] = [rgb[o] as i32, rgb[o + 1] as i32, rgb[o + 2] as i32];
                if let Some(a) = alpha { al[k] = (a[y * w + x] as u16 + 8) / 17; }
            }
            let o = &mut row[bx * bs..(bx + 1) * bs];
            if alpha.is_some() { for k in 0..8 { o[k] = (al[2 * k] | (al[2 * k + 1] << 4)) as u8; } o[8..].copy_from_slice(&bc1_block(&px)); }
            else { o.copy_from_slice(&bc1_block(&px)); }
        }
    });
    out
}

/// rgb (h*w*3) with power-of-two sides; alpha -> DXT3, else DXT1. Full mip chain, exact file size (the game checks it).
pub fn write_dxt(path: &str, rgb: &[u8], alpha: Option<&[u8]>, w: usize, h: usize) {
    let mips = (w.max(h) as f64).log2() as u32 + 1;
    let mut d = dds_header(w, h, if alpha.is_some() { b"DXT3" } else { b"DXT1" }, mips);
    let (mut cur, mut ca, mut cw, mut chh) = (rgb.to_vec(), alpha.map(|a| a.to_vec()), w, h);
    for m in 0..mips {
        d.extend_from_slice(&level_blocks(&cur, ca.as_deref(), cw, chh));
        if m == mips - 1 { break; }
        let (hh, ww) = ((chh / 2).max(1), (cw / 2).max(1)); let (fy, fx) = (chh / hh, cw / ww);
        let mut nx = vec![0u8; hh * ww * 3];
        nx.par_chunks_mut(ww * 3).enumerate().for_each(|(y, row)| {
            for x in 0..ww { for k in 0..3 {
                let mut s = 0u32;
                for yy in 0..fy { for xx in 0..fx { s += cur[((y * fy + yy) * cw + x * fx + xx) * 3 + k] as u32; } }
                row[x * 3 + k] = (s / (fy * fx) as u32) as u8;
            } }
        });
        if let Some(a) = &ca {
            let mut na = vec![0u8; hh * ww];
            for y in 0..hh { for x in 0..ww {
                let mut s = 0u32;
                for yy in 0..fy { for xx in 0..fx { s += a[(y * fy + yy) * cw + x * fx + xx] as u32; } }
                na[y * ww + x] = (s / (fy * fx) as u32) as u8;
            } }
            ca = Some(na);
        }
        cur = nx; cw = ww; chh = hh;
    }
    std::fs::write(format!("{}.tmp", path), &d).and_then(|_| std::fs::rename(format!("{}.tmp", path), path)).expect("cannot write texture");
}

/// uncompressed 32-bit, no mips: the format of the stock airport sub-textures (alpha = 3D grass length)
pub fn write_bgra(path: &str, rgb: &[u8], alpha: &[u8], w: usize, h: usize) {
    let mut d = b"DDS ".to_vec();
    let mut hd = vec![124u32, 0xA1007, h as u32, w as u32, (w * h * 4) as u32, 0, 1];
    hd.extend_from_slice(&[0; 11]);
    hd.extend_from_slice(&[32, 65, 0, 32, 0xFF0000, 0xFF00, 0xFF, 0xFF000000, 0x401008, 0, 0, 0, 0]);
    for v in hd { d.extend_from_slice(&v.to_le_bytes()); }
    d.reserve(w * h * 4);
    for i in 0..w * h { d.extend_from_slice(&[rgb[i * 3 + 2], rgb[i * 3 + 1], rgb[i * 3], alpha[i]]); }
    std::fs::write(path, d).expect("cannot write texture");
}

/// one scene object of a C3D file: vertices = x,y,z, nx,ny,nz, u,v; indices local (0-based), triangle list
pub struct Obj { pub name: String, pub texture: String, pub color: [f32; 4], pub params: [f32; 3], pub verts: Vec<[f32; 8]>, pub idx: Vec<u32> }

pub fn c3d_bytes(objs: &[Obj]) -> Vec<u8> {
    fn ps(o: &mut Vec<u8>, s: &str) { let b: Vec<u8> = s.chars().map(|c| if (c as u32) < 256 { c as u32 as u8 } else { b'_' }).take(255).collect(); o.push(b.len() as u8); o.extend_from_slice(&b); }
    let u = |o: &mut Vec<u8>, v: usize| o.extend_from_slice(&(v as u32).to_le_bytes());
    let mut o = b"C3D".to_vec();
    u(&mut o, 1); u(&mut o, 0); u(&mut o, objs.len()); u(&mut o, objs.len());
    let (mut fv, mut fi) = (0usize, 0usize);
    for ob in objs {
        ps(&mut o, &ob.name);
        u(&mut o, fv); u(&mut o, ob.verts.len()); u(&mut o, fi); u(&mut o, ob.idx.len());
        ps(&mut o, &ob.texture);
        for v in ob.color.iter().chain(ob.params.iter()) { o.extend_from_slice(&v.to_le_bytes()); }
        fv += ob.verts.len(); fi += ob.idx.len();
    }
    u(&mut o, fv);
    o.reserve(fv * 32 + fi * 4 + 4);
    for ob in objs { for v in &ob.verts { for f in v { o.extend_from_slice(&f.to_le_bytes()); } } }
    u(&mut o, fi);
    let mut base = 0u32;
    for ob in objs { for i in &ob.idx { o.extend_from_slice(&(i + base).to_le_bytes()); } base += ob.verts.len() as u32; }
    o
}

/// one 72-byte record of <Landscape>.apt
#[allow(clippy::too_many_arguments)]
pub fn apt_record(name: &str, lat: f64, lon: f64, elev: f64, direction: f64, length: i32, width: i32, asphalt: bool, freq: f64, flags: [bool; 3]) -> Vec<u8> {
    let nb: Vec<u8> = name.chars().map(|c| if (c as u32) < 256 { c as u32 as u8 } else { b'_' }).take(35).collect();
    let mut d = vec![0u8; 36];
    d[0] = nb.len() as u8; d[1..1 + nb.len()].copy_from_slice(&nb);
    for v in [lat as f32, lon as f32, elev as f32] { d.extend_from_slice(&v.to_le_bytes()); }
    for v in [((direction + 1000.0) * 100.0).round() as i32, length, width] { d.extend_from_slice(&v.to_le_bytes()); }
    d.extend_from_slice(&[asphalt as u8, 0, 0, 0]);
    d.extend_from_slice(&(freq as f32).to_le_bytes());
    d.extend_from_slice(&[flags[0] as u8, flags[1] as u8, flags[2] as u8, 0]);
    assert_eq!(d.len(), 72);
    d
}

/// thermal / terrain map from the 90 m heights hgt[x][y] (w columns from the east, hh rows from the south) and thermal[y][x] -> hh*w*3 bytes
pub fn make_tm3(hgt: &[u16], w: usize, hh: usize, thermal: &[u8]) -> Vec<u8> {
    let mut offs = vec![];
    for i in -8i64..=8 { for j in -8i64..=8 { if i * i + j * j <= 64 { offs.push((i, j)); } } }
    let mut out = vec![0u8; hh * w * 3];
    out.par_chunks_mut(w * 3).enumerate().for_each(|(y, row)| {
        for x in 0..w {
            let h0 = hgt[x * hh + y] as f64;
            let (mut n, mut s1, mut s2, mut sl, mut ns) = (0f64, 0f64, 0f64, 0f64, 0f64);
            for &(i, j) in &offs {
                let (sx, sy) = (x as i64 + 2 * i, y as i64 + 2 * j);
                if sx < 0 || sy < 0 || sx >= w as i64 || sy >= hh as i64 { continue; }
                let v = hgt[sx as usize * hh + sy as usize] as f64;
                n += 1.0; s1 += v; s2 += v * v;
                if i != 0 || j != 0 { sl += (h0 - v) / ((i * i + j * j) as f64).sqrt(); ns += 1.0; }
            }
            let sd = ((s2 - s1 * s1 / n) / n).max(0.0).sqrt();
            row[x * 3] = thermal[y * w + x];
            row[x * 3 + 1] = ((sd * 0.01).clamp(0.0, 1.0) * 255.0).round_ties_even() as u8;
            row[x * 3 + 2] = ((sl / ns.max(1.0) * 0.005).clamp(-0.5, 0.5) * 254.0 + 127.0).round_ties_even() as u8;
        }
    });
    out
}
