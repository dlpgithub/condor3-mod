//! Raster back ends. "Canvas" = one Condor tile texture being drawn: top-down, highest surface wins, hole masks applied per pixel,
//! nearest texel. GPU (wgpu: Metal / DX12 / Vulkan) and CPU implementations give the same pictures; the DXT1 blocks are bit-identical
//! for identical pixels (same integer arithmetic in fmt.rs and shaders.wgsl).
use crate::fmt;
use crate::src::Tex;
use rayon::prelude::*;
use wgpu::util::DeviceExt;

pub const CELLS: usize = 1024;            // statistics cells per Condor tile side (5.625 m)
const ZLO: f32 = -1000.0;                 // height range mapped to the depth buffer
const ZHI: f32 = 9000.0;
pub const NONE: f32 = 1e30;               // "no data" in the statistics
/// GPU triangles are grown by this many pixels to close the hairline cracks the GPU's corner snapping opens (see shaders.wgsl).
/// 1/64 gave the closest ground model to the CPU raster / v1 on Orly (1/256 .. 1/16 tested).
const GROW: f32 = 1.0 / 64.0;

/// vertex in the pixel frame of a tile: p = (x px from the west edge, y px from the north edge, height m)
#[repr(C)]
#[derive(Clone, Copy, bytemuck::Pod, bytemuck::Zeroable)]
pub struct V { pub p: [f32; 3], pub uv: [f32; 2], pub hc: [f32; 2], pub m: [u32; 2] }

/// geometry of a chunk of source tiles; draws = (first index, index count, number of its texture in the chunk's texture list)
pub struct Batch { pub v: Vec<V>, pub idx: Vec<u32>, pub draws: Vec<(u32, u32, Option<u32>)> }
/// a batch made ready for drawing (on the GPU: buffers and textures already uploaded, which can happen on another thread)
pub enum Prep { Cpu(Batch), Gpu(GpuPrep), }
pub struct GpuPrep { vb: wgpu::Buffer, ib: wgpu::Buffer, n: u32, draws: Vec<(u32, u32)>, bgs: Vec<wgpu::BindGroup> }
impl Prep { pub fn tris(&self) -> usize { match self { Prep::Cpu(b) => b.idx.len() / 3, Prep::Gpu(g) => g.n as usize / 3 } } }

pub trait Canvas {
    /// draw a prepared batch; texs = the chunk's textures (used by the CPU raster)
    fn draw(&mut self, p: &Prep, texs: &[&Tex]);
    /// per statistics cell (row 0 = north): (lowest height or NONE, mean r, g, b); and the number of pixels with data
    fn stats(&mut self) -> (Vec<[f32; 4]>, u64);
    /// the picture, rgba with alpha 255 where something was drawn
    fn rgba(&mut self) -> Vec<u8>;
    /// rgba of a pixel rectangle
    fn crop(&mut self, x0: usize, y0: usize, w: usize, h: usize) -> Vec<u8>;
    /// DXT1 blocks of the picture with all mips
    fn dxt(&mut self) -> Vec<u8>;
}

pub enum Dev { Gpu(Box<Gpu>), Cpu(Vec<u32>) }
impl Dev {
    pub fn new(force_cpu: bool, mask: &[u32]) -> Dev {
        if !force_cpu { match Gpu::new(mask) { Ok(g) => return Dev::Gpu(Box::new(g)), Err(e) => crate::log(&format!("no usable GPU ({}): using the CPU", e)) } }
        Dev::Cpu(mask.to_vec())
    }
    /// upload a batch (GPU) or keep it (CPU); thread-safe
    pub fn prepare(&self, b: Batch, texs: &[&Tex], color: bool) -> Prep {
        match self { Dev::Cpu(_) => Prep::Cpu(b), Dev::Gpu(g) => Prep::Gpu(g.prepare(b, texs, color)) }
    }
    pub fn name(&self) -> String { match self { Dev::Gpu(g) => format!("GPU {} ({:?})", g.info.name, g.info.backend), Dev::Cpu(_) => "CPU".into() } }
    pub fn canvas(&self, s: usize, color: bool) -> Box<dyn Canvas + '_> {
        match self { Dev::Gpu(g) => Box::new(g.canvas(s, color)), Dev::Cpu(m) => Box::new(CpuCanvas { s, color, z: vec![-1e9; s * s], c: if color { vec![0; s * s * 4] } else { vec![] }, mask: m }) }
    }
    /// DXT1 blocks with mips of pictures of the same size (power-of-two sides), on the GPU in one round trip.
    /// Only worth it for pictures that are already on the GPU (see Canvas::dxt): uploading costs as much as the CPU encoder.
    pub fn dxt1_many(&self, pics: &[&[u8]], w: usize, h: usize) -> Vec<Vec<u8>> {
        match self { Dev::Gpu(g) if w >= 4 && h >= 4 => g.dxt1_many(pics, w, h), _ => pics.iter().map(|p| fmt::dxt_cpu(p, w, h, false)).collect() }
    }
}

fn hidden(mask: &[u32], hu: f64, hv: f64, m: [u32; 2]) -> bool {
    if m[0] == u32::MAX { return false; }
    let k = m[1] as i64;
    let r = ((hv * k as f64).floor() as i64).clamp(0, k - 1); let c = ((hu * k as f64).floor() as i64).clamp(0, k - 1);
    let b = m[0] as usize + (r * k + c) as usize;
    (mask[b >> 5] >> (b & 31)) & 1 == 1
}

// ================================================================== CPU
struct CpuCanvas<'a> { s: usize, color: bool, z: Vec<f32>, c: Vec<u8>, mask: &'a [u32] }
const BAND: usize = 64;

impl<'a> Canvas for CpuCanvas<'a> {
    fn draw(&mut self, p: &Prep, texs: &[&Tex]) {
        let Prep::Cpu(b) = p else { unreachable!() };
        let s = self.s; let nb = s.div_ceil(BAND);
        let mut bins: Vec<Vec<(u32, u32)>> = vec![vec![]; nb];   // (draw, first index of the triangle)
        for (di, &(st, n, _)) in b.draws.iter().enumerate() {
            for t in (st..st + n).step_by(3) {
                let ys = [b.v[b.idx[t as usize] as usize].p[1], b.v[b.idx[t as usize + 1] as usize].p[1], b.v[b.idx[t as usize + 2] as usize].p[1]];
                let y0 = (ys[0].min(ys[1]).min(ys[2]).floor() as i64).max(0); let y1 = (ys[0].max(ys[1]).max(ys[2]).ceil() as i64).min(s as i64 - 1);
                if y0 > y1 { continue; }
                for bi in y0 as usize / BAND..=y1 as usize / BAND { bins[bi].push((di as u32, t)); }
            }
        }
        let grey = Tex { w: 2, h: 2, d: vec![128, 128, 128, 255].repeat(4) };
        let color = self.color; let mask = self.mask;
        let zc: Vec<(&mut [f32], &mut [u8])> = if color { self.z.chunks_mut(BAND * s).zip(self.c.chunks_mut(BAND * s * 4)).collect() }
            else { self.z.chunks_mut(BAND * s).map(|z| (z, &mut [][..])).collect() };
        zc.into_par_iter().zip(bins.par_iter()).enumerate().for_each(|(bi, ((zb, cb), list))| {
            let r0 = bi * BAND; let r1 = (r0 + BAND).min(s) - 1;
            for &(di, t) in list {
                let tex = b.draws[di as usize].2.map(|t| texs[t as usize]).unwrap_or(&grey);
                let (ia, ib, ic) = (b.idx[t as usize] as usize, b.idx[t as usize + 1] as usize, b.idx[t as usize + 2] as usize);
                let (va, vb, vc) = (&b.v[ia], &b.v[ib], &b.v[ic]);
                let (xa, xb, xc, ya, yb, yc) = (va.p[0] as f64, vb.p[0] as f64, vc.p[0] as f64, va.p[1] as f64, vb.p[1] as f64, vc.p[1] as f64);
                let den = (yb - yc) * (xa - xc) + (xc - xb) * (ya - yc);
                if !(den.abs() > 1e-9) { continue; }
                let x0 = (xa.min(xb).min(xc).floor() as i64).max(0); let x1 = (xa.max(xb).max(xc).ceil() as i64).min(s as i64 - 1);
                let y0 = (ya.min(yb).min(yc).floor() as i64).max(r0 as i64); let y1 = (ya.max(yb).max(yc).ceil() as i64).min(r1 as i64);
                if x0 > x1 || y0 > y1 { continue; }
                for iy in y0..=y1 {
                    let gy = iy as f64 + 0.5;
                    for ix in x0..=x1 {
                        let gx = ix as f64 + 0.5;
                        let l0 = ((yb - yc) * (gx - xc) + (xc - xb) * (gy - yc)) / den;
                        let l1 = ((yc - ya) * (gx - xc) + (xa - xc) * (gy - yc)) / den;
                        let l2 = 1.0 - l0 - l1;
                        if l0 < -1e-4 || l1 < -1e-4 || l2 < -1e-4 { continue; }
                        let p = (iy as usize - r0) * s + ix as usize;
                        let zz = (l0 * va.p[2] as f64 + l1 * vb.p[2] as f64 + l2 * vc.p[2] as f64) as f32;
                        if !(zz > zb[p]) { continue; }
                        if va.m[0] != u32::MAX && hidden(mask, l0 * va.hc[0] as f64 + l1 * vb.hc[0] as f64 + l2 * vc.hc[0] as f64, l0 * va.hc[1] as f64 + l1 * vb.hc[1] as f64 + l2 * vc.hc[1] as f64, va.m) { continue; }
                        zb[p] = zz;
                        if color {
                            let uu = l0 * va.uv[0] as f64 + l1 * vb.uv[0] as f64 + l2 * vc.uv[0] as f64;
                            let vv = l0 * va.uv[1] as f64 + l1 * vb.uv[1] as f64 + l2 * vc.uv[1] as f64;
                            let tr = ((vv * tex.h as f64).floor() as i64).clamp(0, tex.h as i64 - 1) as usize;
                            let tc = ((uu * tex.w as f64).floor() as i64).clamp(0, tex.w as i64 - 1) as usize;
                            let so = (tr * tex.w + tc) * 4;
                            cb[p * 4..p * 4 + 3].copy_from_slice(&tex.d[so..so + 3]); cb[p * 4 + 3] = 255;
                        }
                    }
                }
            }
        });
    }
    fn stats(&mut self) -> (Vec<[f32; 4]>, u64) {
        let (s, f) = (self.s, self.s / CELLS);
        let cov = self.z.par_iter().filter(|&&z| z > -1e8).count() as u64;
        (( 0..CELLS * CELLS).into_par_iter().map(|i| {
            let (r, cc) = (i / CELLS, i % CELLS);
            let (mut m, mut n, mut sum) = (NONE, 0u32, [0u32; 3]);
            for y in r * f..(r + 1) * f { for x in cc * f..(cc + 1) * f {
                let z = self.z[y * s + x];
                if z > -1e8 { n += 1; m = m.min(z); if self.color { for k in 0..3 { sum[k] += self.c[(y * s + x) * 4 + k] as u32; } } }
            } }
            if n > 0 { [m, sum[0] as f32 / n as f32, sum[1] as f32 / n as f32, sum[2] as f32 / n as f32] } else { [NONE, 0.0, 0.0, 0.0] }
        }).collect(), cov)
    }
    fn rgba(&mut self) -> Vec<u8> { self.c.clone() }
    fn crop(&mut self, x0: usize, y0: usize, w: usize, h: usize) -> Vec<u8> {
        let mut o = Vec::with_capacity(w * h * 4);
        for y in y0..y0 + h { o.extend_from_slice(&self.c[(y * self.s + x0) * 4..(y * self.s + x0 + w) * 4]); }
        o
    }
    fn dxt(&mut self) -> Vec<u8> { fmt::dxt_cpu(&self.c, self.s, self.s, false) }
}

// ================================================================== GPU
pub struct Gpu {
    pub info: wgpu::AdapterInfo, dev: wgpu::Device, q: wgpu::Queue,
    bgl0: wgpu::BindGroupLayout, bgl1: wgpu::BindGroupLayout, pipe_color: wgpu::RenderPipeline, pipe_height: wgpu::RenderPipeline,
    st_bgl: wgpu::BindGroupLayout, st_pipe: wgpu::ComputePipeline, mp_bgl: wgpu::BindGroupLayout, mp_pipe: wgpu::ComputePipeline,
    bc_bgl: wgpu::BindGroupLayout, bc_pipe: wgpu::ComputePipeline, mask: wgpu::Buffer, grey: wgpu::TextureView, dummy: wgpu::TextureView,
}

fn e_tex(b: u32, vis: wgpu::ShaderStages) -> wgpu::BindGroupLayoutEntry {
    wgpu::BindGroupLayoutEntry { binding: b, visibility: vis, count: None,
        ty: wgpu::BindingType::Texture { sample_type: wgpu::TextureSampleType::Float { filterable: false }, view_dimension: wgpu::TextureViewDimension::D2, multisampled: false } }
}
fn e_buf(b: u32, vis: wgpu::ShaderStages, ty: wgpu::BufferBindingType) -> wgpu::BindGroupLayoutEntry {
    wgpu::BindGroupLayoutEntry { binding: b, visibility: vis, count: None, ty: wgpu::BindingType::Buffer { ty, has_dynamic_offset: false, min_binding_size: None } }
}
const CS: wgpu::ShaderStages = wgpu::ShaderStages::COMPUTE;
const STOR: wgpu::BufferBindingType = wgpu::BufferBindingType::Storage { read_only: false };
const UNI: wgpu::BufferBindingType = wgpu::BufferBindingType::Uniform;

impl Gpu {
    fn new(mask: &[u32]) -> Result<Gpu, String> {
        let inst = wgpu::Instance::new(&wgpu::InstanceDescriptor { backends: wgpu::Backends::PRIMARY, ..Default::default() });
        let ad = pollster::block_on(inst.request_adapter(&wgpu::RequestAdapterOptions { power_preference: wgpu::PowerPreference::HighPerformance, force_fallback_adapter: false, compatible_surface: None })).map_err(|e| e.to_string())?;
        let info = ad.get_info();
        if info.device_type == wgpu::DeviceType::Cpu { return Err("only a software adapter".into()); }
        let lim = ad.limits();
        if lim.max_texture_dimension_2d < 8192 { return Err("textures of 8192 px are not supported".into()); }
        let (dev, q) = pollster::block_on(ad.request_device(&wgpu::DeviceDescriptor { label: None, required_features: wgpu::Features::empty(), required_limits: lim, memory_hints: wgpu::MemoryHints::Performance, trace: wgpu::Trace::Off })).map_err(|e| e.to_string())?;
        let src = include_str!("shaders.wgsl");
        let parts: Vec<&str> = src.split("// ----------------------------------------------------------------").collect();
        assert_eq!(parts.len(), 5);
        let md = |s: &str| dev.create_shader_module(wgpu::ShaderModuleDescriptor { label: None, source: wgpu::ShaderSource::Wgsl(format!("//{}", s).into()) });
        let (m_ras, m_st, m_mp, m_bc) = (md(parts[1]), md(parts[2]), md(parts[3]), md(parts[4]));
        let fs = wgpu::ShaderStages::FRAGMENT;
        let bgl = |e: &[wgpu::BindGroupLayoutEntry]| dev.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor { label: None, entries: e });
        let ro = wgpu::BufferBindingType::Storage { read_only: true }; let vs = wgpu::ShaderStages::VERTEX;
        let bgl0 = bgl(&[e_buf(0, vs | fs, UNI), e_buf(1, fs, ro), e_buf(2, vs, ro), e_buf(3, vs, ro)]);
        let bgl1 = bgl(&[e_tex(0, fs)]);
        let pl = |g: &[&wgpu::BindGroupLayout]| dev.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor { label: None, bind_group_layouts: g, push_constant_ranges: &[] });
        let tgt = |f: wgpu::TextureFormat| Some(wgpu::ColorTargetState { format: f, blend: None, write_mask: wgpu::ColorWrites::ALL });
        let rp = |layout: &wgpu::PipelineLayout, fe: &str, targets: &[Option<wgpu::ColorTargetState>]| dev.create_render_pipeline(&wgpu::RenderPipelineDescriptor {
            label: None, layout: Some(layout),
            vertex: wgpu::VertexState { module: &m_ras, entry_point: Some("vs"), compilation_options: Default::default(), buffers: &[] },
            fragment: Some(wgpu::FragmentState { module: &m_ras, entry_point: Some(fe), compilation_options: Default::default(), targets }),
            primitive: wgpu::PrimitiveState { topology: wgpu::PrimitiveTopology::TriangleList, cull_mode: None, ..Default::default() },
            depth_stencil: Some(wgpu::DepthStencilState { format: wgpu::TextureFormat::Depth32Float, depth_write_enabled: true, depth_compare: wgpu::CompareFunction::Greater, stencil: Default::default(), bias: Default::default() }),
            multisample: Default::default(), multiview: None, cache: None });
        let pipe_color = rp(&pl(&[&bgl0, &bgl1]), "fs_color", &[tgt(wgpu::TextureFormat::Rgba8Unorm), tgt(wgpu::TextureFormat::R32Float)]);
        let pipe_height = rp(&pl(&[&bgl0]), "fs_height", &[tgt(wgpu::TextureFormat::R32Float)]);
        let cp = |layout: &wgpu::BindGroupLayout, m: &wgpu::ShaderModule, e: &str| dev.create_compute_pipeline(&wgpu::ComputePipelineDescriptor {
            label: None, layout: Some(&pl(&[layout])), module: m, entry_point: Some(e), compilation_options: Default::default(), cache: None });
        let st_bgl = bgl(&[e_tex(0, CS), e_tex(1, CS), e_buf(2, CS, STOR), e_buf(3, CS, UNI), e_buf(4, CS, STOR)]);
        let mp_bgl = bgl(&[e_tex(0, CS), wgpu::BindGroupLayoutEntry { binding: 1, visibility: CS, count: None,
            ty: wgpu::BindingType::StorageTexture { access: wgpu::StorageTextureAccess::WriteOnly, format: wgpu::TextureFormat::Rgba8Unorm, view_dimension: wgpu::TextureViewDimension::D2 } }]);
        let bc_bgl = bgl(&[e_tex(0, CS), e_buf(1, CS, STOR), e_buf(2, CS, UNI)]);
        let st_pipe = cp(&st_bgl, &m_st, "stats"); let mp_pipe = cp(&mp_bgl, &m_mp, "mip"); let bc_pipe = cp(&bc_bgl, &m_bc, "bc1");
        let mut mb = mask.to_vec(); if mb.is_empty() { mb.push(0); }
        let mask = dev.create_buffer_init(&wgpu::util::BufferInitDescriptor { label: None, contents: bytemuck::cast_slice(&mb), usage: wgpu::BufferUsages::STORAGE });
        let small = |c: [u8; 4], f: wgpu::TextureFormat, data: &[u8]| dev.create_texture_with_data(&q, &wgpu::TextureDescriptor { label: None, size: wgpu::Extent3d { width: 2, height: 2, depth_or_array_layers: 1 },
            mip_level_count: 1, sample_count: 1, dimension: wgpu::TextureDimension::D2, format: f, usage: wgpu::TextureUsages::TEXTURE_BINDING, view_formats: &[] },
            wgpu::util::TextureDataOrder::LayerMajor, &if data.is_empty() { c.repeat(4) } else { data.to_vec() }).create_view(&Default::default());
        let grey = small([128, 128, 128, 255], wgpu::TextureFormat::Rgba8Unorm, &[]);
        let dummy = small([0, 0, 0, 0], wgpu::TextureFormat::Rgba8Unorm, &[]);
        Ok(Gpu { info, dev, q, bgl0, bgl1, pipe_color, pipe_height, st_bgl, st_pipe, mp_bgl, mp_pipe, bc_bgl, bc_pipe, mask, grey, dummy })
    }

    fn tex(&self, w: usize, h: usize, f: wgpu::TextureFormat, mips: u32, usage: wgpu::TextureUsages) -> wgpu::Texture {
        self.dev.create_texture(&wgpu::TextureDescriptor { label: None, size: wgpu::Extent3d { width: w as u32, height: h as u32, depth_or_array_layers: 1 },
            mip_level_count: mips, sample_count: 1, dimension: wgpu::TextureDimension::D2, format: f, usage, view_formats: &[] })
    }
    fn level(t: &wgpu::Texture, l: u32) -> wgpu::TextureView { t.create_view(&wgpu::TextureViewDescriptor { base_mip_level: l, mip_level_count: Some(1), ..Default::default() }) }
    fn uniform(&self, d: &[u8]) -> wgpu::Buffer { self.dev.create_buffer_init(&wgpu::util::BufferInitDescriptor { label: None, contents: d, usage: wgpu::BufferUsages::UNIFORM }) }
    fn bg(&self, l: &wgpu::BindGroupLayout, r: &[wgpu::BindingResource]) -> wgpu::BindGroup {
        let e: Vec<wgpu::BindGroupEntry> = r.iter().enumerate().map(|(i, x)| wgpu::BindGroupEntry { binding: i as u32, resource: x.clone() }).collect();
        self.dev.create_bind_group(&wgpu::BindGroupDescriptor { label: None, layout: l, entries: &e })
    }
    /// run the encoder, then copy `size` bytes out of `src`
    fn read(&self, mut enc: wgpu::CommandEncoder, src: &wgpu::Buffer, size: u64) -> Vec<u8> {
        let rb = self.dev.create_buffer(&wgpu::BufferDescriptor { label: None, size, usage: wgpu::BufferUsages::MAP_READ | wgpu::BufferUsages::COPY_DST, mapped_at_creation: false });
        enc.copy_buffer_to_buffer(src, 0, &rb, 0, size);
        crate::prof("read.submit", || self.q.submit([enc.finish()]));
        let sl = rb.slice(..);
        sl.map_async(wgpu::MapMode::Read, |r| r.expect("GPU read-back failed"));
        crate::prof("read.wait", || self.dev.poll(wgpu::PollType::Wait).expect("GPU poll failed"));
        let v = crate::prof("read.copy", || sl.get_mapped_range().to_vec());
        rb.unmap();
        v
    }
    fn enc(&self) -> wgpu::CommandEncoder { self.dev.create_command_encoder(&Default::default()) }

    fn canvas(&self, s: usize, color: bool) -> GpuCanvas<'_> {
        use wgpu::TextureUsages as TU;
        let mips = fmt::mip_count(s, s);
        let ctex = if color { Some(self.tex(s, s, wgpu::TextureFormat::Rgba8Unorm, mips, TU::RENDER_ATTACHMENT | TU::TEXTURE_BINDING | TU::STORAGE_BINDING | TU::COPY_SRC | TU::COPY_DST)) } else { None };
        let htex = self.tex(s, s, wgpu::TextureFormat::R32Float, 1, TU::RENDER_ATTACHMENT | TU::TEXTURE_BINDING | TU::COPY_SRC);
        let dtex = self.tex(s, s, wgpu::TextureFormat::Depth32Float, 1, TU::RENDER_ATTACHMENT);
        let mut ub = vec![]; for v in [s as f32, ZLO, 1.0 / (ZHI - ZLO), GROW] { ub.extend_from_slice(&v.to_le_bytes()); }
        let uni = self.uniform(&ub);
        let c = GpuCanvas { g: self, s, cview: ctex.as_ref().map(|t| Gpu::level(t, 0)), ctex, hview: htex.create_view(&Default::default()), dview: dtex.create_view(&Default::default()), _ht: htex, _dt: dtex, uni };
        let mut enc = self.enc();
        c.pass(&mut enc, true);
        self.q.submit([enc.finish()]);
        c
    }

    /// mips + BC1 of textures (same size) whose level 0 is filled; one read-back for all
    fn encode(&self, ts: &[&wgpu::Texture], w: usize, h: usize, mut enc: wgpu::CommandEncoder) -> Vec<Vec<u8>> {
        let mips = fmt::mip_count(w, h);
        let mut offs = vec![]; let mut words = 0usize;
        for l in 0..mips { let (lw, lh) = ((w >> l).max(1), (h >> l).max(1)); let (bw, bh) = (lw.div_ceil(4), lh.div_ceil(4)); offs.push((words, bw, bh, lw, lh)); words += bw * bh * 2; }
        let all = words * ts.len();
        let out = self.dev.create_buffer(&wgpu::BufferDescriptor { label: None, size: (all * 4) as u64, usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_SRC, mapped_at_creation: false });
        let mut keep = vec![];
        {
            let mut p = enc.begin_compute_pass(&Default::default());
            for (n, t) in ts.iter().enumerate() {
                let views: Vec<wgpu::TextureView> = (0..mips).map(|l| Gpu::level(t, l)).collect();
                let mbg: Vec<wgpu::BindGroup> = (1..mips as usize).map(|l| self.bg(&self.mp_bgl, &[wgpu::BindingResource::TextureView(&views[l - 1]), wgpu::BindingResource::TextureView(&views[l])])).collect();
                let unis: Vec<wgpu::Buffer> = offs.iter().map(|o| self.uniform(bytemuck::cast_slice(&[(n * words + o.0) as u32, o.1 as u32, o.2 as u32, 0]))).collect();
                let bbg: Vec<wgpu::BindGroup> = (0..mips as usize).map(|l| self.bg(&self.bc_bgl, &[wgpu::BindingResource::TextureView(&views[l]), out.as_entire_binding(), unis[l].as_entire_binding()])).collect();
                p.set_pipeline(&self.mp_pipe);
                for l in 1..mips as usize { p.set_bind_group(0, &mbg[l - 1], &[]); p.dispatch_workgroups((offs[l].3 as u32).div_ceil(8), (offs[l].4 as u32).div_ceil(8), 1); }
                p.set_pipeline(&self.bc_pipe);
                for l in 0..mips as usize { p.set_bind_group(0, &bbg[l], &[]); p.dispatch_workgroups((offs[l].1 as u32).div_ceil(8), (offs[l].2 as u32).div_ceil(8), 1); }
                keep.push((views, mbg, unis, bbg));
            }
        }
        let d = crate::prof("dxt.run+read", || self.read(enc, &out, (all * 4) as u64));
        d.chunks_exact(words * 4).map(|c| c.to_vec()).collect()
    }

    /// upload the vertices / indices (storage buffers, the vertex shader pulls them) and the textures of a batch
    fn prepare(&self, b: Batch, texs: &[&Tex], color: bool) -> GpuPrep {
        let buf = |d: &[u8]| self.dev.create_buffer_init(&wgpu::util::BufferInitDescriptor { label: None, contents: if d.is_empty() { &[0u8; 4] } else { d }, usage: wgpu::BufferUsages::STORAGE });
        let (vb, ib) = crate::prof("gpu.upload", || (buf(bytemuck::cast_slice(&b.v)), buf(bytemuck::cast_slice(&b.idx))));
        let mut bgs = vec![];
        if color {
            bgs = crate::prof("gpu.textures", || b.draws.par_iter().map(|&(_, _, t)| {
                let view = t.map(|t| { let t = texs[t as usize]; self.dev.create_texture_with_data(&self.q, &wgpu::TextureDescriptor { label: None, size: wgpu::Extent3d { width: t.w as u32, height: t.h as u32, depth_or_array_layers: 1 },
                    mip_level_count: 1, sample_count: 1, dimension: wgpu::TextureDimension::D2, format: wgpu::TextureFormat::Rgba8Unorm, usage: wgpu::TextureUsages::TEXTURE_BINDING, view_formats: &[] },
                    wgpu::util::TextureDataOrder::LayerMajor, &t.d).create_view(&Default::default()) });
                self.bg(&self.bgl1, &[wgpu::BindingResource::TextureView(view.as_ref().unwrap_or(&self.grey))])
            }).collect());
        }
        GpuPrep { vb, ib, n: b.idx.len() as u32, draws: b.draws.iter().map(|d| (d.0, d.1)).collect(), bgs }
    }
    /// timing of the encoder alone on a texture that is already on the GPU (for --selftest)
    pub fn bench_encode(&self, rgba: &[u8], w: usize) -> f64 {
        use wgpu::TextureUsages as TU;
        let t = self.tex(w, w, wgpu::TextureFormat::Rgba8Unorm, fmt::mip_count(w, w), TU::TEXTURE_BINDING | TU::STORAGE_BINDING | TU::COPY_DST);
        self.q.write_texture(wgpu::TexelCopyTextureInfo { texture: &t, mip_level: 0, origin: wgpu::Origin3d::ZERO, aspect: wgpu::TextureAspect::All }, rgba,
            wgpu::TexelCopyBufferLayout { offset: 0, bytes_per_row: Some(w as u32 * 4), rows_per_image: Some(w as u32) }, wgpu::Extent3d { width: w as u32, height: w as u32, depth_or_array_layers: 1 });
        self.encode(&[&t], w, w, self.enc());
        let t0 = std::time::Instant::now();
        for _ in 0..3 { self.encode(&[&t], w, w, self.enc()); }
        t0.elapsed().as_secs_f64() / 3.0
    }
    fn dxt1_many(&self, pics: &[&[u8]], w: usize, h: usize) -> Vec<Vec<u8>> {
        use wgpu::TextureUsages as TU;
        let ts: Vec<wgpu::Texture> = crate::prof("dxt.upload", || pics.iter().map(|rgba| {
            let t = self.tex(w, h, wgpu::TextureFormat::Rgba8Unorm, fmt::mip_count(w, h), TU::TEXTURE_BINDING | TU::STORAGE_BINDING | TU::COPY_DST);
            self.q.write_texture(wgpu::TexelCopyTextureInfo { texture: &t, mip_level: 0, origin: wgpu::Origin3d::ZERO, aspect: wgpu::TextureAspect::All }, rgba,
                wgpu::TexelCopyBufferLayout { offset: 0, bytes_per_row: Some(w as u32 * 4), rows_per_image: Some(h as u32) }, wgpu::Extent3d { width: w as u32, height: h as u32, depth_or_array_layers: 1 });
            t
        }).collect());
        self.encode(&ts.iter().collect::<Vec<_>>(), w, h, self.enc())
    }
}

pub struct GpuCanvas<'a> {
    g: &'a Gpu, s: usize, ctex: Option<wgpu::Texture>, cview: Option<wgpu::TextureView>, hview: wgpu::TextureView, dview: wgpu::TextureView,
    _ht: wgpu::Texture, _dt: wgpu::Texture, uni: wgpu::Buffer,
}
impl<'a> GpuCanvas<'a> {
    fn pass<'e>(&self, enc: &'e mut wgpu::CommandEncoder, clear: bool) -> wgpu::RenderPass<'e> {
        let op = |c: wgpu::Color| wgpu::Operations { load: if clear { wgpu::LoadOp::Clear(c) } else { wgpu::LoadOp::Load }, store: wgpu::StoreOp::Store };
        let mut ca = vec![];
        if let Some(v) = &self.cview { ca.push(Some(wgpu::RenderPassColorAttachment { view: v, resolve_target: None, ops: op(wgpu::Color::TRANSPARENT) })); }
        ca.push(Some(wgpu::RenderPassColorAttachment { view: &self.hview, resolve_target: None, ops: op(wgpu::Color { r: -1e9, g: 0.0, b: 0.0, a: 0.0 }) }));
        enc.begin_render_pass(&wgpu::RenderPassDescriptor { label: None, color_attachments: &ca,
            depth_stencil_attachment: Some(wgpu::RenderPassDepthStencilAttachment { view: &self.dview, depth_ops: Some(if clear { wgpu::Operations { load: wgpu::LoadOp::Clear(0.0), store: wgpu::StoreOp::Store } } else { wgpu::Operations { load: wgpu::LoadOp::Load, store: wgpu::StoreOp::Store } }), stencil_ops: None }),
            timestamp_writes: None, occlusion_query_set: None })
    }
    fn copy_out(&self, t: &wgpu::Texture, x0: usize, y0: usize, w: usize, h: usize) -> Vec<u8> {
        let g = self.g; let bpr = (w * 4).div_ceil(256) * 256;
        let buf = g.dev.create_buffer(&wgpu::BufferDescriptor { label: None, size: (bpr * h) as u64, usage: wgpu::BufferUsages::COPY_SRC | wgpu::BufferUsages::COPY_DST, mapped_at_creation: false });
        let mut enc = g.enc();
        enc.copy_texture_to_buffer(wgpu::TexelCopyTextureInfo { texture: t, mip_level: 0, origin: wgpu::Origin3d { x: x0 as u32, y: y0 as u32, z: 0 }, aspect: wgpu::TextureAspect::All },
            wgpu::TexelCopyBufferInfo { buffer: &buf, layout: wgpu::TexelCopyBufferLayout { offset: 0, bytes_per_row: Some(bpr as u32), rows_per_image: Some(h as u32) } },
            wgpu::Extent3d { width: w as u32, height: h as u32, depth_or_array_layers: 1 });
        let d = g.read(enc, &buf, (bpr * h) as u64);
        if bpr == w * 4 { return d; }
        let mut o = Vec::with_capacity(w * h * 4);
        for r in 0..h { o.extend_from_slice(&d[r * bpr..r * bpr + w * 4]); }
        o
    }
}
impl<'a> Canvas for GpuCanvas<'a> {
    fn draw(&mut self, p: &Prep, _texs: &[&Tex]) {
        let Prep::Gpu(pp) = p else { unreachable!() };
        if pp.n == 0 { return; }
        let g = self.g;
        let bg0 = g.bg(&g.bgl0, &[self.uni.as_entire_binding(), g.mask.as_entire_binding(), pp.vb.as_entire_binding(), pp.ib.as_entire_binding()]);
        let mut enc = g.enc();
        {
            let mut rp = self.pass(&mut enc, false);
            rp.set_bind_group(0, &bg0, &[]);
            if self.cview.is_some() {
                rp.set_pipeline(&g.pipe_color);
                for (i, &(st, n)) in pp.draws.iter().enumerate() { rp.set_bind_group(1, &pp.bgs[i], &[]); rp.draw(st..st + n, 0..1); }
            } else {
                rp.set_pipeline(&g.pipe_height);
                rp.draw(0..pp.n, 0..1);
            }
        }
        g.q.submit([enc.finish()]);
    }
    fn stats(&mut self) -> (Vec<[f32; 4]>, u64) {
        let g = self.g; let n = CELLS;
        let out = g.dev.create_buffer(&wgpu::BufferDescriptor { label: None, size: (n * n * 16) as u64, usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_SRC, mapped_at_creation: false });
        let cnt = g.dev.create_buffer(&wgpu::BufferDescriptor { label: None, size: 16, usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_SRC, mapped_at_creation: false });
        let uni = g.uniform(bytemuck::cast_slice(&[(self.s / n) as u32, n as u32, self.cview.is_some() as u32, 0]));
        let bg = g.bg(&g.st_bgl, &[wgpu::BindingResource::TextureView(self.cview.as_ref().unwrap_or(&g.dummy)), wgpu::BindingResource::TextureView(&self.hview), out.as_entire_binding(), uni.as_entire_binding(), cnt.as_entire_binding()]);
        let mut enc = g.enc();
        { let mut p = enc.begin_compute_pass(&Default::default()); p.set_pipeline(&g.st_pipe); p.set_bind_group(0, &bg, &[]); p.dispatch_workgroups((n / 8) as u32, (n / 8) as u32, 1); }
        let d = g.read(enc, &out, (n * n * 16) as u64);
        let c = g.read(g.enc(), &cnt, 16);
        (bytemuck::cast_slice::<u8, [f32; 4]>(&d).to_vec(), u32::from_le_bytes([c[0], c[1], c[2], c[3]]) as u64)
    }
    fn rgba(&mut self) -> Vec<u8> { let s = self.s; self.copy_out(self.ctex.as_ref().expect("colour canvas"), 0, 0, s, s) }
    fn crop(&mut self, x0: usize, y0: usize, w: usize, h: usize) -> Vec<u8> { self.copy_out(self.ctex.as_ref().expect("colour canvas"), x0, y0, w, h) }
    fn dxt(&mut self) -> Vec<u8> { let s = self.s; self.g.encode(&[self.ctex.as_ref().expect("colour canvas")], s, s, self.g.enc()).pop().unwrap() }
}
