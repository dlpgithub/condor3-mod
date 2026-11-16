//! tiles2condor - build a Condor 3 landscape from 3D Tiles exports (one GLB per tile, e.g. from the "earth-rea" tile library).
//! Rust port of tools/tiles2condor.py: same command line, same stages, same output files. Manual: docs/TILES2CONDOR.md.
mod fmt; mod geo; mod glb; mod grid; mod plan; mod raster; mod stages;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::OnceLock;
use std::time::Instant;

const USAGE: &str = "usage:  tiles2condor <export_dir> [<export_dir> ...] <config.json> <Landscapes_dir>/<Name> [--work DIR] [--budget SECONDS] [--only STAGE] [--threads N]

  export_dir   one or more folders with tileset.json, tiles/*.glb and (recommended) location.json. Several exports are merged; a tile that
               occurs twice is used once. The exports may mix any number of detail levels: at every place the FINEST tile wins.
  config.json  landscape options and the airports (see docs/TILES2CONDOR.md)
  output       the landscape folder to create (its name is the landscape name)

Stages (each skips what already exists in the work folder / output): plan, raster, terrain, textures, base, airports, objects.
--only STAGE runs just that stage. --budget stops cleanly after that many seconds (exit code 2; run the same command again to continue).
--threads limits the CPU cores used (default: all). The work folder defaults to <first export_dir>/_condor_work_rs
(it is NOT interchangeable with the work folder of the Python version).";

static T0: OnceLock<Instant> = OnceLock::new();
static BUDGET: AtomicU64 = AtomicU64::new(u64::MAX);
pub fn secs() -> f64 { T0.get_or_init(Instant::now).elapsed().as_secs_f64() }
pub fn out_of_time() -> bool { secs() > BUDGET.load(Ordering::Relaxed) as f64 }
pub fn log(s: &str) { println!("{}", s); }
/// number from the config with a default
pub fn cf(v: &serde_json::Value, k: &str, d: f64) -> f64 { v[k].as_f64().unwrap_or(d) }

fn main() {
    secs();
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut pos = vec![]; let mut opt = std::collections::HashMap::new();
    let mut i = 0;
    while i < args.len() {
        if ["--work", "--budget", "--only", "--threads"].contains(&args[i].as_str()) && i + 1 < args.len() { opt.insert(args[i].clone(), args[i + 1].clone()); i += 2; }
        else if args[i].starts_with("--") { eprintln!("unknown option {}\n\n{}", args[i], USAGE); std::process::exit(1); }
        else { pos.push(args[i].clone()); i += 1; }
    }
    if pos.len() < 3 { println!("{}", USAGE); std::process::exit(1); }
    let out = pos.pop().unwrap().trim_end_matches('/').to_string(); let cfgp = pos.pop().unwrap(); let exports = pos;
    for e in &exports { if !std::path::Path::new(&format!("{}/tileset.json", e)).exists() { eprintln!("{} is not an export folder (no tileset.json)", e); std::process::exit(1); } }
    let name = out.rsplit('/').next().unwrap().to_string();
    let cfg: serde_json::Value = match std::fs::read(&cfgp).map_err(|e| e.to_string()).and_then(|d| serde_json::from_slice(&d).map_err(|e| e.to_string())) {
        Ok(v) => v, Err(e) => { eprintln!("cannot read the config {}: {}", cfgp, e); std::process::exit(1) } };
    let work = opt.get("--work").cloned().unwrap_or(format!("{}/_condor_work_rs", exports[0].trim_end_matches('/')));
    std::fs::create_dir_all(&work).expect("cannot create the work folder");
    if let Some(b) = opt.get("--budget") { BUDGET.store(b.parse::<f64>().expect("--budget needs a number") as u64, Ordering::Relaxed); }
    if let Some(t) = opt.get("--threads") { rayon::ThreadPoolBuilder::new().num_threads(t.parse().expect("--threads needs a number")).build_global().unwrap(); }
    let stages = ["plan", "raster", "terrain", "textures", "base", "airports", "objects"];
    let only = opt.get("--only").map(|s| s.as_str());
    if let Some(o) = only { if !stages.contains(&o) { eprintln!("--only must be one of: {}", stages.join(", ")); std::process::exit(1); } }
    let run = |s: &str| only.is_none() || only == Some(s);
    let stop = || -> ! { log("time budget used up: run the same command again to continue"); std::process::exit(2) };

    let (plan, fresh) = plan::stage_plan(&exports, &cfg, &work);
    if only == Some("plan") { if !fresh { plan::report(&plan, &cfg, &work, 0); } return; }
    std::fs::create_dir_all(&out).expect("cannot create the output folder");
    if run("raster") && !raster::stage_raster(&plan, &work) { stop(); }
    let mut trn = vec![];
    if run("terrain") || only == Some("base") { trn = grid::stage_terrain(&plan, &cfg, &work, &out, &name); }
    if out_of_time() { stop(); }
    if run("textures") && !stages::stage_textures(&plan, &work, &out, &name) { stop(); }
    if run("base") { stages::stage_base(&plan, &work, &out, &name, &trn); }
    if run("airports") { stages::stage_airports(&plan, &cfg, &work, &out, &name); }
    if run("objects") && !stages::stage_objects(&plan, &cfg, &out, &name) { stop(); }
    log(&format!("done: {}  ({:.0}s)", out, secs()));
}
