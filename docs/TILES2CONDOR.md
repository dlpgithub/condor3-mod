# tiles2condor: a Condor 3 landscape from a 3D Tiles export

`tools/tiles2condor.py` turns a photogrammetry export (3D Tiles 1.1, one GLB per tile, as written by the user's "earth-rea" tile library with
"3D Tiles - one glb per file") into a complete, flyable Condor 3 landscape. Formats and conventions: `docs/TERRAIN_FORMAT.md`.
First result: `Landscapes/Orly` from `/Users/lennard/Downloads/earth-3dtiles-66e4c38d` (Paris Orly, 11.3 x 6.1 km, 1680 tiles), verified in game.

```
python3 tools/tiles2condor.py <export_dir> [<export_dir> ...] <config.json> <Condor3>/Landscapes/<Name> [--work DIR] [--budget SECONDS] [--only STAGE]
```
Needs python3 with numpy and Pillow (`pip3 install numpy pillow`; the Mac's system python has neither).

- **Several exports** can be given (earth-rea caps one export at 5000 tiles). They are merged; a tile present twice is used once.
- **Any mix of detail levels** is fine: at every place the finest tile wins (see "Detail levels").
- Every stage is resumable: it skips what already exists in the work folder (default `<first export_dir>/_condor_work`) or in the output.
  `--budget N` stops cleanly after N seconds (exit code 2); run the same command again to continue. `--only STAGE` runs one stage.
- **Outputs that exist are kept.** To rebuild after changing something, build into a new landscape name or remove the old landscape folder,
  and use a fresh work folder. If the exports or the layout settings change, the tool refuses to reuse an old work folder and says so.
- **Look before the long part:** `--only plan` takes seconds, prints which levels are used and writes `plan_levels.png` into the work
  folder (a map of the detail level used everywhere, the Condor tile grid with texture sizes, and the runways).

Typical run for the Orly export:
```
cd /Users/lennard/Desktop/condor3-mod
python3 tools/tiles2condor.py /Users/lennard/Downloads/earth-3dtiles-66e4c38d examples/orly.json /Users/lennard/Condor3/Landscapes/Orly2 --only plan
open /Users/lennard/Downloads/earth-3dtiles-66e4c38d/_condor_work/plan_levels.png
python3 tools/tiles2condor.py /Users/lennard/Downloads/earth-3dtiles-66e4c38d examples/orly.json /Users/lennard/Condor3/Landscapes/Orly2
```
The last line of the airports stage prints `TPPosX / TPPosY / TPPosZ` for each airport (for the flight plan file); in the game simply pick the
landscape and an airport in the flight planner.

## Input
- `tileset.json` + `tiles/*.glb`; `location.json` (lat/lon rectangle and level of every tile) is used when present and makes planning exact.
  Without it the footprint is taken from each mesh and the level is estimated from its width.
- GLB: plain glTF 2.0, float positions relative to the tile centre, glTF Y-up (`ecef = M * (x, -z, y)`, M = accumulated tileset transforms),
  u16/u32 indices, one PNG/JPEG texture per primitive, no Draco. Heights are used as altitude directly (Orly comes out at 83-89 m, correct);
  `height_offset` in the config shifts them.

## Detail levels: the finest tile wins
- For every tile the tool looks for FINER tiles (in any export) that overlap its lat/lon rectangle. Those parts are holes in the coarse tile.
  The rectangles of such exports subdivide each other exactly, so the holes are exact (kept as a small grid per tile, up to 6 levels deep).
- Ground photo and heights: a coarse tile is not drawn into its holes (decided per pixel, so the seam is exact).
- 3D objects: a coarse tile drops the triangles whose centre lies in a hole (the seam can be off by one triangle, a few metres).
- A tile completely covered by finer ones is not read at all. The plan report lists, per level: tiles, used whole, partly hidden, fully hidden.
- Orly example: 1120 level-18 tiles used whole; of 560 level-17 tiles 247 are fully hidden, 26 partly hidden, the rest used whole.
- Known limits: colours and heights can jump at a level border (different source imagery); the terrain is smoothed across it, the photo is not.
  The source also splits tiles by height; a tall object whose top is in another tile than its base could be cut wrongly at a level border.

## Texture size per Condor tile
Each Condor tile (5.76 km) gets the texture size matching the finest detail that covers a real share of it:
source detail about 0.8 m/px -> 8192 px, 1.6 -> 4096, 3.2 -> 2048, coarser -> 1024; tiles without data get a 512 px filler.
`texture_size` caps the largest size, `min_texture_size` the smallest, `detail_min_share` (default 0.03) is the share of the tile a detail
level must cover to count. Rendering (stage raster) happens per Condor tile at that size, so coarse surroundings are fast and small.

## Config (JSON)
```json
{
  "margin_tiles": 1,            // empty Condor tiles (5.76 km) added on every side; the glider cannot fly within ~5 km of the border
  "texture_size": 8192,         // largest texture size per Condor tile
  "min_texture_size": 1024,     // smallest size for tiles that have data
  "detail_min_share": 0.03,
  "height_offset": 0.0,         // metres added to all heights
  "ground_filter_m": 35,        // things narrower than about twice this are removed from the ground model (buildings, trees)
  "utm_zone": null,             // default: the zone of the data
  "objects": { "enabled": true, "min_height": 2.5, "sink": 1.5, "min_level": 0 },
  "airports": [
    { "name": "Orly-06",
      "runway": { "from": [48.7196927, 2.3156802], "to": [48.7361067, 2.362025], "width": 45, "surface": "asphalt" },
      "frequency": 118.7,
      "ground": { "side": 100, "ends": 150 },     // optional: extent of the airport ground beside / beyond the runway [m]
      "grass": "auto" }                            // "auto": 3D grass where the photo is green; false: none
  ]
}
```
- One entry = one Condor airport = ONE runway (Condor's `.apt` knows a single runway per airport). `from` / `to` are the lat, lon of the two
  runway ends; the glider starts 200 m after `from`, heading towards `to`. Several runways of one real airport = several entries
  (`examples/orly.json` has Orly's three). Optional: `primary_reversed`, `tow_primary_left`, `tow_secondary_left`.
- `objects.min_level`: source tiles coarser than this level produce no 3D objects (their area is photo only). `"objects": false` disables 3D.
- (JSON has no comments; the `//` notes above are explanation only.)

## What the stages do
| stage | work / output files | what happens |
|---|---|---|
| plan | `plan.json`, `plan_levels.png` | UTM zone, landscape size = data extent rounded up to tiles + margin, data centred; all tiles of all exports with the share hidden by finer tiles; texture size per Condor tile |
| raster | `ras_TXTY_i_j.npz` (blocks of at most 2048 px per Condor tile) | top-down z-buffer render of all visible triangles (`raster_tris`) at the tile's own resolution: colour (orthophoto) + highest surface height per pixel |
| terrain | `grid.npz`, `terrain.npy`, `valid.npy`; `.trn`, `HeightMaps/*.tr3`, `22.5m/*.tr3f`, `.tha` | ground model on a 5.6 m grid: lowest height per cell, morphological opening (removes buildings/trees), blur; extra smoothing inside airport grounds; the area without data is filled from the edges. A float `.tr3f` is written for EVERY tile, so the rendered terrain is exact to the centimetre |
| textures | `Textures/t*.dds`, `empty.dds`, `.bmp` | orthophoto as DXT1 tile textures (data tiles at their planned size, filler tiles 512 px blurred edge colours), flight-planner map |
| base | `.ini`, `ForestMaps/*.for` (empty), `.fha`, `.tm3`, `.obj`, `.air`, `.aha`, `.oha` | forest maps are empty on purpose (trees come with the 3D data); thermal map: green 102, everything else 178 |
| airports | `.apt`, `.cup`, `Airports/<A>G.c3d`, `<A>O.c3d`, `Airports/Textures/<A>/<A>_Ground.dds` | see below |
| objects | `Autogen/oXXYY.c3d`, `Autogen/Textures/aXXYY_NNN.dds`, `.oha` | every triangle with a corner more than `min_height` above the ground model is kept; textures packed into 2048 px atlas pages (one C3D object per page); feet of walls pushed `sink` m into the ground; one file per Condor tile, MD5 in `.oha` |

### Airports
- Elevation, heading and length are computed (heading is the grid direction in the landscape's UTM zone, which is what the game uses).
- The ground mesh is cut from the 22.5 m terrain grid with the stock triangulation (cell (i, j): i+j even -> diagonal (i,j)-(i+1,j+1), else the
  other one; found in the stock Grass3D meshes), so it lies exactly on the rendered terrain, also on sloped or curved airfields.
- **Only grass goes into the G file** (`Grass3D` with a north-up crop of the orthophoto as sub-texture, alpha = blade length where green).
  Runways, taxiways and aprons are left to the plain terrain texture: drawn through Grass3D they came out bleached and grainy in game.
  If an airport has no green cell at all, one cell is written so the mandatory G file exists.
- Ground areas of several airports must not overlap; a cell belongs to the first airport that claims it.
- O file: one windsock 35 m left of the runway, 320 m after its start.

## Status of testing
The multi-level / multi-export / per-tile-size version (session 6, second half) was only run on a 21-tile sample across a level border
(all stages complete, no gaps in the orthophoto at the seam). It has NOT been run on the full Orly export or looked at in the game yet:
the user runs the builds himself. `Landscapes/Orly` on disk is from the first version (single resolution; coarse and fine copies overlap
in a ring of 26 tiles around the detailed zone).

## Verified in game with the first version (Orly, 4 x 4 tiles)
Flight planner shows the photo map and the three runways in place; flight starts on runway 06; photo terrain; buildings, hangars, terminal and
control tower stand in 3D. Game log: memory 1.6 of 4 GB (the game is a 32-bit process). Numbers: 2.6 M object triangles, 170 MB of C3D,
50 atlas pages, four 8192 px tile textures; conversion about 6 minutes in total.

## Known limits / ideas
- The landscape is padded with blurred filler to the minimum flyable size; export a larger low-detail surrounding area for something better.
- Ground-level detail is limited by the source (about 0.7 m/px); roofs and tree tops are also baked into the ground texture under the 3D objects.
- Very large buildings survive in the ground model as low mounds under their 3D mesh (raise `ground_filter_m` if they show).
- Objects use upward normals (even lighting like the baked photo), no level of detail, and are solid like all autogen. Performance with much
  larger exports is untested; `objects.min_height` or `"objects": false` reduce the load.
- No water detection (texture alpha stays opaque), no thermal classes beyond green / not green, no airspace.
- Asphalt surface objects (with tyre-mark detail) could be added for the runway given in the config.

## Rust version (fast, native binary)

`rust/tiles2condor/` is a port of `tools/tiles2condor.py` to Rust. Same command line, same stages, same config file, same output files.
A compiled Mac binary (Intel, x86_64) is at `bin/tiles2condor` (not in git; rebuild it with the commands below).

    bin/tiles2condor <export_dir> [<export_dir> ...] <config.json> <Landscapes_dir>/<Name> [--work DIR] [--budget S] [--only STAGE] [--threads N]

Differences to the Python version:

- All CPU cores are used (raster blocks, texture compression, object tiles). `--threads N` limits that.
- The work folder defaults to `<first export_dir>/_condor_work_rs` and has its own file format. Do not point it at a Python work folder
  (and the other way round).
- `plan_levels.png` has only number labels (Condor tile + texture size, level numbers in the legend); airports are drawn as white lines without names.
- The filler noise in the textures comes from a different random generator.

Build (needs the Rust toolchain from https://rustup.rs): `scripts/build_rust.sh` (macOS, Linux) or `scripts\build_rust.bat` (Windows)
build both compilers into `bin/` (`v1` / `v2` for one of them, `debug` as second argument for a debug build). By hand:

    cd rust/tiles2condor
    cargo build --release
    cp target/release/tiles2condor ../../bin/

Check against Python (21-tile + 24-tile sample around lat 48.708, lon 2.304, two detail levels, one airport): of the 23 output files 17 are
byte-identical (among them `.trn`, `.tr3`, `.apt`, `.tm3`, the airport meshes and the object atlas textures). The others differ only by rounding:
`.tr3f` below 1 mm (so `.tha` differs), object vertices below 0.1 mm (so `.oha` differs), map `.bmp` by 1 grey level, the tile texture by the filler
noise (data area: 99.3 % of the pixels identical). Run time on that sample: Python 22 s, Rust 3 s. Like the reworked Python version it has
NOT been run on a full export or looked at in the game yet.

## Rust version 2 (GPU, per-tile, 3-4x faster)

`rust/tiles2condor_v2/` is a rewrite of the Rust version: same command line (plus `--cpu`), same config, same output files; stages
plan, scan, terrain, render, fill, base, airports (`--only` takes these names). GPU raster through wgpu (Metal / DX12 / Vulkan, CPU
fallback), every GLB decoded once for photo and objects, terrain and fill colours computed per Condor tile, output written in the
background. Orly export (308 tiles): 14 s -> 4 s; Orly area (2907 tiles): 69 s -> 21-25 s, work folder 1.9 GB -> 0.15 GB.
Details, measurements and the comparison with v1: `rust/tiles2condor_v2/README.md`.
With mixed-level exports v2 also writes coarser autogen sets per level (`Autogen/oXXYY_L19.c3d`, `_L17`, list in `Autogen/lods.txt`)
for the streaming helper's distant detail levels; `"objects": { "distant_sets": false }` switches that off.

    scripts/build_rust.sh v2           # or scripts\build_rust.bat v2 on Windows; = cd rust/tiles2condor_v2 && cargo build --release
    bin/tiles2condor_v2 <export_dir> ... <config.json> <Landscapes_dir>/<Name> [--work DIR] [--budget S] [--only STAGE] [--threads N] [--cpu]
