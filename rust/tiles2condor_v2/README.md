# tiles2condor_v2

Rewrite of `rust/tiles2condor` (v1). Same command line, same config file, same output files (a complete Condor 3 landscape from
3D Tiles exports). Faster, and its memory and work folder no longer grow with the size of the landscape. v1 is left unchanged.

    cd rust/tiles2condor_v2 && ~/.cargo/bin/cargo build --release
    target/release/tiles2condor_v2 <export_dir> [<export_dir> ...] <config.json> <Landscapes_dir>/<Name> [--work DIR] [--budget S] [--only STAGE] [--threads N] [--cpu]
    target/release/tiles2condor_v2 --selftest     # GPU vs CPU texture encoder: identical bytes? + timings

Work folder default: `<first export_dir>/_condor_work_v2`. Manual for the config and the outputs: `docs/TILES2CONDOR.md`.

## Stages

| stage | what | v1 equivalent |
|---|---|---|
| plan | tiles, landscape grid, "finest wins" masks, texture size per Condor tile (unchanged from v1) | plan |
| scan | geometry only (no texture decoding): top-down render of every data tile -> lowest surface per 5.6 m cell (`zmin_*.bin`) | raster (heights) |
| terrain | ground model per Condor tile (opening, blurs, airports, push-pull fill, softening), then `.trn/.tr3/.tr3f/.tha` | terrain |
| render | each data tile once: colour render, mean colour per cell, airport photo crops, 3D objects + atlas pages; every GLB is decoded once for photo and objects | raster (colour) + objects |
| fill | colours for the area without data, textures of filler tiles and of partly covered tiles, planner map `.bmp`, `empty.dds` | textures |
| base, airports | as v1 | base, airports |

## What is different inside

- **GPU raster (wgpu: Metal on the Mac, DX12/Vulkan on Windows).** Top-down orthographic render, highest surface wins (depth test), hole
  masks per pixel in the fragment shader, nearest texel like v1. Vertices are pulled from storage buffers; each triangle is grown by 1/64 px
  to close the hairline cracks that the GPU's corner snapping opens at T-junctions (without this a pixel can see the ground under a roof,
  which matters for the lowest-surface statistics). `--cpu` uses a CPU raster with v1's edge rule instead; results are practically equal.
- **Texture compression (DXT1 + mips).** GPU compute shader and CPU encoder use the same integer arithmetic and give identical bytes
  (`--selftest`). Tiles rendered on the GPU are compressed there (8192 px in 0.07 s); pictures that live on the CPU (atlas pages, partly
  covered tiles) are compressed on idle CPU cores in the background (uploading costs as much as encoding).
- **Each GLB decoded once.** v1 decoded every GLB for each 2048 px raster block it touched and again for the objects. v2 reads geometry
  only for the scan, then decodes once for colour and objects together (tiles crossing a Condor tile border: once per tile).
  The next chunk is read, decoded and uploaded on other threads while the current one is drawn.
- **Projection.** Instead of ECEF -> lat/lon -> UTM for every vertex, a quadratic fit per source tile (checked against the exact
  formulas on 32 points, tolerance 0.5 mm; exact formulas if the check fails).
- **Per-tile terrain and fill colours.** The landscape-wide filters of v1 run per Condor tile on a window with enough halo from the
  neighbours that the tile comes out as in a landscape-wide computation; push-pull uses a landscape-wide pyramid only from 32 x 32 cells
  per tile up. Memory: a few tiles at a time. The box blur runs its columns in parallel and the three colour channels in one pass
  (same summation order as v1).
- **Background writing.** Output files go to a writer thread; later stages read files that are still queued from memory, so the disk
  writes while the next tiles are computed. The program waits for the disk only at the end ("writing" in the summary).
- **Less in the work folder.** Lowest surface (f32) and mean colour (u16, 1/256 grey level) per cell, airport photo crops, markers;
  pictures of partly covered tiles stay in memory (up to 2 GB, else lz4 in the work folder). Orly-sized test: 155 MB instead of v1's 1.9 GB.
- **Resumable** like v1: `--budget` stops between tiles (exit code 2), the next run continues (tested: identical bytes).

## Numbers (this Mac: i5-7500 4 cores, RX 5700 XT, 16 GB; its disk writes only 30-120 MB/s, which limits the big case)

| export | v1 | v2 |
|---|---|---|
| Orly `earth-3dtiles-e15386dd` (308 tiles L17, 4 x 3 Condor tiles, 2 x 4096 px) | 14.3 s, 1.0 GB RAM | 3.9-4.5 s, 0.55 GB |
| Orly area `earth-3dtiles-26296f6a` (2907 tiles L17, 6 x 5, 12 x 4096 px) | 69 s, 2.4 GB RAM, 1.9 GB work | 21-25 s (38 s when the disk is slow), 1.8 GB, 0.15 GB work |
| Eiffel `earth-3dtiles-e9dc3adf` (1267 tiles L17/19/20, 3 x 3, 1 x 8192 px) | 14 s | 6.8 s |

`T2C_PROF=1` prints where the time went. On this machine the rest is mostly PNG decoding, copying textures to the GPU and the disk.

## Checked against v1 (`tools/compare.py A B`, viewer `tools/c3view.py`)

- Heights: mean difference 1-8 mm (v1 rounded the raster heights to 0.1 m; v2 keeps floats). Single cells at the edge of the data can differ
  by a few metres: the ground model takes minima, and whether one pixel hits a steep wall there decides a local dip (v1 has the same
  sensitivity; with the GPU's 1/64 px growth v2 is closest to v1).
- Tile textures: 96-99 % of the pixels identical (differences at triangle edges and in the fill colours), objects: triangle counts within
  0.05 %, object atlas pages byte-identical with v1 on Orly, airports: same cells and records.
- Viewer screenshots v1 vs v2 (Orly area: terminal, overview, low over runway 07, data edge): no visible difference.
- Not yet flown in the game with a v2 landscape.

## Files

`main.rs` stages and options, `plan.rs` (from v1), `src.rs` GLB + projection fit, `gpu.rs` + `shaders.wgsl` raster / statistics / DXT1,
`scan.rs`, `terrain.rs`, `render.rs` (photo + objects), `finish.rs` (fill, base, airports), `grid.rs` tiled grids and filters,
`fmt.rs` file formats, `bg.rs` background writer, `geo.rs` (from v1). `tools/compare.py` compares two landscapes file by file.
