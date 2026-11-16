# tiles2condor: a Condor 3 landscape from a 3D Tiles export

`tools/tiles2condor.py` turns a photogrammetry export (3D Tiles 1.1, one GLB per tile, as written by the user's "earth-rea" tile library with
"3D Tiles - one glb per file") into a complete, flyable Condor 3 landscape. Formats and conventions: `docs/TERRAIN_FORMAT.md`.
First result: `Landscapes/Orly` from `/Users/lennard/Downloads/earth-3dtiles-66e4c38d` (Paris Orly, 11.3 x 6.1 km, 1680 tiles), verified in game.

```
python3 tools/tiles2condor.py <export_dir> <config.json> <Condor3>/Landscapes/<Name> [--work DIR] [--budget SECONDS] [--only STAGE]
```
Needs python3 with numpy and Pillow (`pip3 install numpy pillow`; the Mac's system python has neither). Every stage is resumable: it skips
what already exists in the work folder (default `<export_dir>/_condor_work`) or in the output. `--budget N` stops cleanly after N seconds
(exit code 2) so it can be run in slices; rerun the same command to continue. To redo a stage delete its outputs (see table).

## Input
- `tileset.json` + `tiles/*.glb`; optional `location.json` (lat/lon bounds per tile, speeds up planning).
- GLB: plain glTF 2.0, float positions relative to the tile centre, glTF Y-up (`ecef = M * (x, -z, y)`, M = accumulated tileset transforms),
  u16/u32 indices, one PNG/JPEG texture per primitive, no Draco. Heights are used as altitude directly (Orly comes out at 83-89 m, correct);
  `height_offset` in the config shifts them.
- Only the finest tiles are used: a tileset node that has children is replaced by them.

## Config (JSON)
```json
{
  "margin_tiles": 1,            // empty Condor tiles (5.76 km) added on every side; the glider cannot fly within ~5 km of the border
  "texture_size": 8192,         // pixels per Condor tile for tiles with data (8192 = 0.70 m/px, 4096 = 1.4 m/px)
  "height_offset": 0.0,         // metres added to all heights
  "ground_filter_m": 35,        // things narrower than about twice this are removed from the ground model (buildings, trees)
  "utm_zone": null,             // default: the zone of the data
  "objects": { "enabled": true, "min_height": 2.5, "sink": 1.5 },
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
- (JSON has no comments; the `//` notes above are explanation only.)

## What the stages do
| stage | work / output files | what happens |
|---|---|---|
| plan | `plan.json` | UTM zone, landscape size = data extent rounded up to tiles + margin, data centred; list of leaf tiles |
| raster | `ras_BX_BY.npz` (2048 px blocks) | top-down z-buffer render of all triangles (`raster_tris`): colour (orthophoto) + highest surface height per pixel |
| terrain | `grid.npz`, `terrain.npy`, `valid.npy`; `.trn`, `HeightMaps/*.tr3`, `22.5m/*.tr3f`, `.tha` | ground model on a 5.6 m grid: lowest height per cell, morphological opening (removes buildings/trees), blur; extra smoothing inside airport grounds; the area without data is filled from the edges. A float `.tr3f` is written for EVERY tile, so the rendered terrain is exact to the centimetre |
| textures | `Textures/t*.dds`, `empty.dds`, `.bmp` | orthophoto as DXT1 tile textures (data tiles at `texture_size`, filler tiles 512 px blurred edge colours), flight-planner map |
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

## Verified in game (Orly, 4 x 4 tiles)
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
