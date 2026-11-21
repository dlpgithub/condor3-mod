//! tiles2condor_v2 - build a Condor 3 landscape from 3D Tiles exports (one GLB per tile, e.g. from the "earth-rea" tile library).
//! Rewrite of rust/tiles2condor (v1): same command line, config and output files; GPU raster (wgpu), each GLB decoded once for photo and
//! 3D objects, terrain and fill colours computed per Condor tile (memory independent of the landscape size). Manual: README.md here.
mod bg; mod finish; mod fmt; mod geo; mod gpu; mod grid; mod plan; mod render; mod runway; mod scan; mod src; mod terrain;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::OnceLock;
use std::time::Instant;

const USAGE: &str = "usage:  tiles2condor_v2 <export_dir> [<export_dir> ...] <config.json> <Landscapes_dir>/<Name> [--work DIR] [--budget SECONDS] [--only STAGE] [--threads N] [--cpu]

  export_dir   one or more folders with tileset.json, tiles/*.glb and (recommended) location.json. Several exports are merged; a tile that
               occurs twice is used once. The exports may mix any number of detail levels: at every place the FINEST tile wins.
  config.json  landscape options and the airports (same file as for v1, see docs/TILES2CONDOR.md)
  output       the landscape folder to create (its name is the landscape name)

Stages (each skips what already exists in the work folder / output): plan, scan, terrain, render, fill, base, airports.
--only STAGE runs just that stage. --budget stops cleanly after that many seconds (exit code 2; run the same command again to continue).
--threads limits the CPU cores used (default: all). --cpu renders on the CPU instead of the GPU (practically the same results;
also used automatically when no GPU is found). --selftest checks the GPU texture encoder against the CPU one.
The work folder defaults to <first export_dir>/_condor_work_v2 (not interchangeable with the v1 or Python work folders).
T2C_PROF=1 in the environment prints where the time went.";

static T0: OnceLock<Instant> = OnceLock::new();
static BUDGET: AtomicU64 = AtomicU64::new(u64::MAX);
pub fn secs() -> f64 { T0.get_or_init(Instant::now).elapsed().as_secs_f64() }
pub fn out_of_time() -> bool { secs() > BUDGET.load(Ordering::Relaxed) as f64 }
pub fn log(s: &str) { println!("{}", s); }
pub static PROF: std::sync::Mutex<std::collections::BTreeMap<&'static str, f64>> = std::sync::Mutex::new(std::collections::BTreeMap::new());
/// time a step (summed per label; printed at the end when T2C_PROF is set)
pub fn prof<T>(k: &'static str, f: impl FnOnce() -> T) -> T {
    let t = Instant::now(); let r = f();
    *PROF.lock().unwrap().entry(k).or_default() += t.elapsed().as_secs_f64();
    r
}
/// number from the config with a default
pub fn cf(v: &serde_json::Value, k: &str, d: f64) -> f64 { v[k].as_f64().unwrap_or(d) }

/// tiles2condor_v2 --selftest: GPU and CPU must give identical DXT1 bytes; prints timings
fn selftest() {
    let mut s = 12345u64;
    let mut rnd = || { s = s.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407); (s >> 33) as u8 };
    let dev = gpu::Dev::new(false, &[0]);
    log(&format!("device: {}", dev.name()));
    for &(n, w) in &[(1usize, 512usize), (8, 2048), (1, 4096), (1, 8192)] {
        // smooth-ish picture with noise, like a photo
        let pics: Vec<Vec<u8>> = (0..n).map(|k| (0..w * w).flat_map(|i| { let (x, y) = (i % w, i / w); let b = ((x / 7 + y / 5 + k * 13) % 200) as u8; [b.wrapping_add(rnd() % 24), b / 2 + rnd() % 16, 255 - b, 255] }).collect()).collect();
        let refs: Vec<&[u8]> = pics.iter().map(|p| p.as_slice()).collect();
        let t = Instant::now(); let g = dev.dxt1_many(&refs, w, w); let tg = t.elapsed().as_secs_f64();
        let t = Instant::now(); let c: Vec<Vec<u8>> = refs.iter().map(|p| fmt::dxt_cpu(p, w, w, false)).collect(); let tc = t.elapsed().as_secs_f64();
        log(&format!("{} x {} px: GPU {:.3}s  CPU {:.3}s  identical: {}", n, w, tg, tc, g == c));
        log(&format!("   {}", std::mem::take(&mut *PROF.lock().unwrap()).iter().map(|(k, v)| format!("{} {:.3}", k, v)).collect::<Vec<_>>().join(", ")));
        if let gpu::Dev::Gpu(g) = &dev { log(&format!("   encoder alone (texture already on the GPU): {:.3}s", g.bench_encode(&pics[0], w))); }
        PROF.lock().unwrap().clear();
    }
}

fn main() {
    secs();
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.first().map(|s| s.as_str()) == Some("--selftest") { selftest(); return; }
    let mut pos = vec![]; let mut opt = std::collections::HashMap::new(); let mut force_cpu = false;
    let mut i = 0;
    while i < args.len() {
        if ["--work", "--budget", "--only", "--threads"].contains(&args[i].as_str()) && i + 1 < args.len() { opt.insert(args[i].clone(), args[i + 1].clone()); i += 2; }
        else if args[i] == "--cpu" { force_cpu = true; i += 1; }
        else if args[i].starts_with("--") { eprintln!("unknown option {}\n\n{}", args[i], USAGE); std::process::exit(1); }
        else { pos.push(args[i].clone()); i += 1; }
    }
    if pos.len() < 3 { println!("{}", USAGE); std::process::exit(1); }
    let out = pos.pop().unwrap().trim_end_matches('/').to_string(); let cfgp = pos.pop().unwrap(); let exports = pos;
    for e in &exports { if !std::path::Path::new(&format!("{}/tileset.json", e)).exists() { eprintln!("{} is not an export folder (no tileset.json)", e); std::process::exit(1); } }
    let name = out.rsplit('/').next().unwrap().to_string();
    let cfg: serde_json::Value = match std::fs::read(&cfgp).map_err(|e| e.to_string()).and_then(|d| serde_json::from_slice(&d).map_err(|e| e.to_string())) {
        Ok(v) => v, Err(e) => { eprintln!("cannot read the config {}: {}", cfgp, e); std::process::exit(1) } };
    let work = opt.get("--work").cloned().unwrap_or(format!("{}/_condor_work_v2", exports[0].trim_end_matches('/')));
    std::fs::create_dir_all(&work).expect("cannot create the work folder");
    if let Some(b) = opt.get("--budget") { BUDGET.store(b.parse::<f64>().expect("--budget needs a number") as u64, Ordering::Relaxed); }
    if let Some(t) = opt.get("--threads") { rayon::ThreadPoolBuilder::new().num_threads(t.parse().expect("--threads needs a number")).build_global().unwrap(); }
    let stages = ["plan", "scan", "terrain", "render", "fill", "base", "airports"];
    let only = opt.get("--only").map(|s| s.as_str());
    if let Some(o) = only { if !stages.contains(&o) { eprintln!("--only must be one of: {}", stages.join(", ")); std::process::exit(1); } }
    let run = |s: &str| only.is_none() || only == Some(s);
    let stop = || -> ! { render::parts_flush(&work); bg::flush(); log("time budget used up: run the same command again to continue"); std::process::exit(2) };
    let mut times: Vec<(&str, f64)> = vec![];
    let mut lap = secs();
    let mut tick = |s: &'static str, times: &mut Vec<(&str, f64)>| { let t = secs(); times.push((s, t - lap)); lap = t; };

    let (plan, fresh) = plan::stage_plan(&exports, &cfg, &work);
    if only == Some("plan") { if !fresh { plan::report(&plan, &cfg, &work, 0); } return; }
    tick("plan", &mut times);
    std::fs::create_dir_all(&out).expect("cannot create the output folder");
    let needs_dev = run("scan") || run("render") || run("fill");
    let dev = if needs_dev { let (bits, _) = plan.mask_bits(); let d = gpu::Dev::new(force_cpu, &bits); log(&format!("raster on {}", d.name())); Some(d) } else { None };
    tick("device", &mut times);
    if run("scan") && !scan::stage_scan(&plan, dev.as_ref().unwrap(), &work) { stop(); }
    tick("scan", &mut times);
    let mut trn = vec![];
    if only == Some("terrain") || (run("terrain") && !bg::exists(&terrain::done_path(&work))) { trn = terrain::stage_terrain(&plan, &cfg, &work, &out, &name); }
    tick("terrain", &mut times);
    if out_of_time() { stop(); }
    if run("render") && !render::stage_render(&plan, &cfg, dev.as_ref().unwrap(), &work, &out, &name) { stop(); }
    if only == Some("render") { render::parts_flush(&work); }
    tick("render", &mut times);
    if run("fill") && !finish::stage_fill(&plan, dev.as_ref().unwrap(), &work, &out, &name) { stop(); }
    tick("fill", &mut times);
    if run("base") {
        if trn.is_empty() {
            let d = std::fs::read(format!("{}/{}.trn", out, name)).unwrap_or_else(|_| { eprintln!("{}.trn is missing: run the terrain stage first", name); std::process::exit(1) });
            trn = d[36..].chunks_exact(2).map(|c| u16::from_le_bytes([c[0], c[1]])).collect();
        }
        finish::stage_base(&plan, &work, &out, &name, &trn);
    }
    tick("base", &mut times);
    if run("airports") { finish::stage_airports(&plan, &cfg, &work, &out, &name); }
    tick("airports", &mut times);
    bg::flush();
    tick("writing", &mut times);
    log(&format!("done: {}  ({:.1}s: {})", out, secs(), times.iter().filter(|t| t.1 >= 0.05).map(|(s, t)| format!("{} {:.1}", s, t)).collect::<Vec<_>>().join(", ")));
    if std::env::var("T2C_PROF").is_ok() { log(&format!("profile: {}", PROF.lock().unwrap().iter().map(|(k, v)| format!("{} {:.2}", k, v)).collect::<Vec<_>>().join(", "))); }
}
