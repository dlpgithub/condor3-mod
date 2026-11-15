# Condor 3 landscape format (reverse engineered from Condor.exe via REA, verified on Slovenia3)

Landscape dir: `Landscapes/<Name>/`. Slovenia3: 60 x 48 tiles, tile = 64 cells x 90 m = 5760 m.

## Files
| file | content |
|---|---|
| `<N>.ini` | `[General] Version=1.02`, `RealtimeShading=1` |
| `<N>.trn` | 36 B header + 3840x3072 uint16 heights (m), **[x][y], y fastest**. Header: u32 w, u32 h, f32 90.0 (cell), f32 -90, f32 90, f32 UTM easting origin 645120, f32 northing origin 5022720, u32 UTM zone 33, u32 'N'(78). Outer 32 cells are faded to sea level by the loader. Coarse height map. |
| `HeightMaps/hXXYY.tr3` | 193x193 uint16 (m), no header, [x][y] (y fastest), 30 m posts, neighbours share edge posts. Name `h%03d%03d` tried first, else `h%02d%02d` (x,y). |
| `HeightMaps/22.5m/hXXYY.tr3f` | 257x257 float32, 22.5 m posts, tried first when present. Engine-native resolution (patch array 257x257); .tr3 is bilinearly resampled. |
| `ForestMaps/XXYY.for` | 512x512 uint8 (0 none, 1/2 tree type), name `%03d%03d` else `%02d%02d`. Loaded for 4x4 tiles around camera. |
| `Textures/tXXYY.dds` | per-tile DXT1/DXT5 with mips, 2048/4096/8192 px; `Textures\empty.dds` fallback. |
| `.tha .fha .oha .aha` | hash tables (see below) |
| `.tm3` | 3840x3072 RGB, rows [y][x] (smooth image data; far-terrain colour, consumer TBD) |
| `.bmp` | 3840x3072 32bpp map for flight planner |
| `.apt .cup .air .obj`, `Airports/`, `Autogen/`, `World/` | airports, waypoints, airspace, objects |

Missing .tr3 -> heightmaps flagged invalid and not loaded. Tiles outside range -> zeros. Border tiles (x==0/max, y==0/max): outer half tile faded by (192-i)/192*2.

## Hash tables (.tha heights, .fha forest) -- SOLVED
Lines `"%03d%03d" CRLF value` where the key is `tx*1000+ty` (decimal text). Check at load (FUN_004b46c8):

```
s    = sum(byte+1 for byte in file) + 0   # == sum(bytes)+len, uint32
seed = (idx + 0x14A68F2) & 0xFFFFFFFF
seed = (seed*0x41C64E6D + 1) & 0xFFFFFFFF   # one Delphi LCG step
hash = round(s * seed / 2**32)               # x87 float, nearest
idx  = 48*tx + ty                            # tileCountY*tx+ty
```
Verified: .fha 2871/2880 exact, rest off by 1; .tha same (tr3f used when present). Loader accepts |diff|<=1; mismatch only clears a "valid" flag (L+0x38 heights, L+0x40 forest).
Custom landscape: just generate the tables with this formula (tools/terrain_hash.py).

## Textures (FUN_0076c9d0, verified on all 2814 Slovenia3 tiles)
- Path: `<landscape dir>\Textures\t` + `%03d%03d.dds` else `%02d%02d.dds` (tile x, y), else `Textures\empty.dds` (256x256 DXT3). Slovenia3 has 2814 of 2880 tile textures; missing tiles use empty.dds.
- Plain DDS: magic `DDS `, 124-byte header, then ALL mips down to 1x1 (levels = log2(size)+1). Width must equal height and be non-zero.
- Pixel format must be fourcc `DXT1` (BC1, 8 B/block) or `DXT3` (BC2, 16 B/block). **DXT5 is rejected.**
- File size must equal exactly `128 + sum over mips of max(1,(s+3)/4)^2 * blockbytes` (s halves each level), else the tile is rejected.
- Seen sizes: 2048 (DXT1/DXT3), 4096, 8192 (14 mips). Loader creates an immutable D3D11 texture skipping the top `lod + 2 - TextureQuality` mips, so different tile distances use smaller mips.
- Residency (FUN_0076d094): tiles with Chebyshev distance to camera tile <4 load LOD 0, <7 LOD 1, <11 LOD 2, otherwise unloaded.

## Open items
- Geo "calibration" check ("Landscape not correctly calibrated.", code at 0x6aeeb0, not a Ghidra function): compares fabs() of floats at [[0x792a68]+0x4750/+0x4754] with 80.0 and 100.0 and another with 6000.0. Meaning not yet decoded.
- `.tm3` consumer, `LandscapeOrigin` is only an FX parameter name (shader), not a file field.
- Landscape enumeration (`Landscapes\*.*`, "Checking landscapes", xref 0x6a8747), registry key `Software\Condor3\Landscapes\` (license key), "not registered -> demo" message at 0x830272.

## Landscape discovery (flight planner init, FUN_006a849c)
Enumerates `Landscapes\*.*` directories (skips `.` and `..`), requires `Landscapes\<N>\<N>.ini` to exist, reads `[General] Version` as an **integer** (default 0). Version==0 (absent or non-integer such as `1.02`) -> landscape is added to the list; a valid integer value -> an error message is shown instead. So a custom landscape just needs a folder + ini copied from an existing one (`Version=1.02`). No registry key is needed to be listed (registry `Software\Condor3\Landscapes\` is only the license/registration -> otherwise "Running demo version").

## .apt airport records (Slovenia3.apt, 39 records x 72 bytes, no header)
`[0]` name length, `[1..35]` name padded with spaces (36 B), `+0x24` f32 latitude (deg N), `+0x28` f32 longitude (deg E), `+0x2c` f32 elevation m, `+0x30` u32 (unknown), `+0x34` u16 (700-1060, probably runway length m), `+0x38` u32 (25..100, probably width/other), `+0x40` f32 (~118-123, radio frequency MHz), `+0x44` 4 bytes flags. Lat/lon here are geographic degrees (Aigen 47.536, 14.141) -> the landscape is georeferenced via the .trn UTM origin (zone 33N, E 645120, N 5022720).
Per airport: `Airports/<Name>.grs` (~21 MB), `<Name>G.c3d` (ground 3D, 'C3D' magic + ascii chunks like "Grass3D"), `<Name>O.c3d` (objects).

## Georeference (verified with all 39 airports, median error 2 m)
`.trn` header: E0 = 645120 (UTM zone 33N easting of the **east** edge), N0 = 5022720 (northing of the **south** edge).
Heights/tiles grid: `x = (E0 - E)/90` (x counts WEST from the east edge), `y = (N - N0)/90` (y counts NORTH from the south edge). Tile (tx,ty) = (x//64, y//64).
.tm3 is [y][x] RGB rows (same indexing as the height grid, no flips). .bmp is a normal north-up, west-left 32bpp image (bottom-up BMP, row 0 = south), 90 m per pixel.
`tools/utm.py` has forward/inverse UTM.

## End-to-end proof: cropped landscape (tools/crop_landscape.py)
`crop_landscape.py Slovenia3 Crop8 32 38 8 8` builds an 8x8-tile landscape: new .trn with shifted E0/N0, renamed tr3/tr3f/for/dds tiles (new tile index), regenerated .tha/.fha (idx = nTilesY*tx+ty with the NEW nTilesY) and .oha (md5), cropped .tm3/.bmp, airports inside the crop. Result in game (CrossOver): appears in the flight planner list as `Crop8 [1.02]`, map and airports at correct lat/lon, flight starts at Aigen, terrain/textures/autogen/grass correct.
Notes learned: `Airports/Textures` and `Autogen/Textures` are referenced BY NAME from the .c3d files and must be copied whole (without them airfield ground renders white). Files outside tile range are not needed.
.oha = MD5 hex of `Autogen/oXXYY.c3d`, key tx*1000+ty (empty entry if no autogen file). Neither .oha nor .aha strings occur in Condor.exe (probably only the validator tools).

## Orientation of tile textures and forest maps (verified)
All per-tile rasters use the same convention: with tile origin (x0,y0) = (tx*5760, ty*5760) (the tile's SOUTH-EAST corner, x grows WEST, y grows NORTH):
- `.tr3`/`.tr3f`/`.for` arrays: `a[i][j]`, i (slow) -> x = x0 + i*step, j (fast) -> y = y0 + j*step.
- `tXXYY.dds` is an ordinary north-up map image: column c -> x = x0 + T*(1-(c+.5)/S) (column 0 = WEST edge), row r -> y = y0 + T*(1-(r+.5)/S) (row 0 = NORTH edge).
Evidence: correlation of real Slovenia3 textures with heights (5 tiles) and with forest maps (5 tiles, r = -0.7..-0.86, all the same transform), plus an in-game test: a synthetic texture coloured by world quadrant around the airport shows green NE / blue SE exactly where expected from the cockpit (heading 065).
World coordinates seen by the game: x west, y north, z up (glider position doubles at obj+0x6d8/0x6e0/0x6e8, e.g. `move 0 0 0` in the mod prints them); lat/lon computed from (E0-x, N0+y) matches the in-game HUD to 4 decimals.
Airport files are position independent: G.c3d/O.c3d/.grs of Aigen work when the airport is moved elsewhere (only lat/lon/elevation in the .apt record change); elevation must match the terrain.
Synthetic landscape: `tools/make_landscape.py` (own height function, own textures/forest, new airport position) loads and plays in the game (Synth8).

## Verified optionality / robustness (in-game tests on Synth8)
- `<name>.tm3` is REQUIRED: without it the game logs `Cannot open file ...tm3. File not found.` right after "Initializing Thermals." and shuts down. It is the per-landscape thermal map (786432 bytes = 512x512x3 for an 8x8-tile landscape: 3 bytes per 90 m cell, [y][x]); a synthetic one (green->tan by height) is accepted.
- Wrong values in `.tha`/`.fha` (+5000 on every hash) load without any log message or visible effect in the test flight (hash only gates the cached-validity flags; see above).
- Texture orientation confirmed in game (world-quadrant tint test, heading 065: NE ahead-left, SE ahead-right): north-up, column 0 = west, row 0 = north.
- Tool pitfall: `make_landscape.py OUT NAME` takes the landscape directory itself (`.../Landscapes/Synth8`), not the Landscapes folder.

## Facts from the official guides (Landscape Guide rev2 = V2 text, AirportsInV3.pdf; toolkit in ~/Downloads/landscape-toolkit)
- Hierarchy: patch = 5760 m (192 x 30 m), tile = 4x4 patches = 23040 m (768 x 30 m). Official tile/patch names are CCRR, origin 0000 = bottom-right (SE) corner, counted west and north: same as our tx,ty.
- Landscape size must be a whole multiple of 5760 m. Glider cannot fly closer than ~5 km (one patch) to the border.
- Patch texture standard 2048 px (2.8125 m/px), may be larger for airport patches; alpha channel of the texture = water mask.
- Forest maps: two classes, coniferous and deciduous (our .for values 1/2), up to 2048 px per TILE = 512 px per patch (our 512x512); black = no trees.
- Thermal map: grayscale, 90 m/px (256 px per tile); black = no thermals, white = strongest; used together with "flats activity". Reference values: water 0, deciduous 64, forest/grass 102, fields 178. Our .tm3 is stored RGB (786432 B = 512*512*3 for 2x2 tiles).
- THA/FHA: mandatory; mismatch is treated as cheating (free flight and multiplayer). `LandscapeEditor.exe -hash <Landscape>` regenerates both. (Our test with wrong hashes showed no log message, but do not rely on that: always write correct hashes.)
- INI: only `Version` and `RealtimeShading` (0 disables real-time terrain shading).
- Waypoints: `<Name>.cup` (SeeYou format); Condor drops waypoints outside the landscape at flight start.
- Objects: `World/Objects/*.c3d` + `World/Objects/Textures/`; airports: `Airports/<Name>G.c3d` (ground; Grass, Asphalt, Grasspaint, Asphaltpaint, V3: Grass3D, AsphaltPaintYellow, Gravel) + `<Name>O.c3d` (crashable objects, up to 3 windsocks as proxy triangles Windsack1..3). V3: G file mandatory (missing -> blue screen); airport runway axis is local +X; 22.5 m `HeightMaps/22.5m/hXXYY.tr3f` for the airport patch; G-file surfaces are cut from that terrain with Z=0 at runway centre; sub-texture dds (8.8.8.8 ARGB, alpha = grass length) mapped on Grass3D/Asphalt.
- Official pipeline: OBJ (Y forward, Z up, triangulated) -> ObjectEditor -> C3D; LandscapeEditor imports BMP tile textures (8192 px/tile), exports tr3/for/tm3/dds/hash.

## Solved from the official LandscapeEditor.exe (disassembled; Delphi, strings sit right after their function)
### .aha
`<name>.aha` = decimal number written as a text line: the same hash function as .tha/.fha applied to the bytes of `<name>.air` (airspace file) with idx = 0:
`round((sum(byte+1)) * LCG(0+0x14A68F2) / 2^32)` (function 0x49ec30, LCG `x = x*0x41C64E6D+1`, see terrain_hash.py). Empty .air -> `0`. Missing .air -> editor error "*.air file not found".
### .tm3 (thermal/terrain map, 90 m cells) -- tools/make_tm3.py reproduces Slovenia3.tm3 exactly (<=1 LSB in <0.5% of cells)
Layout `tm3[y][x][3]`, y=0 south edge, x=0 east edge (pairs with trn[x][y]); size = W*H*3 (W,H of the .trn).
- byte0: thermal map value from `Working/ThermalMap/ThermalMap.bmp` (grayscale; 0 none ... 255 max; bitmap is north-up/west-left)
- byte1: clamp(stddev of heights in a disk * 0.01, 0, 1)*255 (terrain roughness)
- byte2: clamp(mean((h0-h)/dist) * 0.005, -0.5, 0.5)*254 + 127 (mean slope toward the neighbourhood; ~127 = flat)
- disk = offsets (i,j), i^2+j^2<=64, sampled at (x+2i, y+2j) (cells outside the map skipped); dist = sqrt(i^2+j^2).
The game requires the file (missing -> shutdown right after "Initializing Thermals").

### .for values (LandscapeEditor 'Export forest maps', @0x5ed4c0)
Per patch a 512x512 byte array is built from two 512x512 paletted bitmaps in `Working/ForestMaps/`: `s<CCRR>.bmp` (coniferous) -> value 1, `b<CCRR>.bmp` (deciduous) -> += 2, so values are 0 none, 1 conifer, 2 deciduous, 3 both. Array `[i][j]` = bitmap pixel (x = 511-i, y = 511-j): i (slow) runs from the EAST edge towards west, j (fast) from the SOUTH edge towards north (matches the correlation result). Written raw (262144 bytes) to `ForestMaps/XXYY.for`.

### .apt record (tools/apt.py, byte-exact round trip of all 39 Slovenia3 records)
72 bytes: ShortString[35] name (len byte + chars, bytes after the string are uninitialised garbage), f32 lat, f32 lon, f32 elevation, i32 direction code `(deg+1000)*100+hundredths` (106493 = 64.93 deg), i32 runway length, i32 width, u8 asphalt(1)/grass(0) +3 pad, f32 frequency MHz, u8 primary-dir-reversed, u8 tow-primary-left-side, u8 tow-secondary-left-side, u8 0.
(Names in the game also select `Airports/<Name>G.c3d` / `<Name>O.c3d`.)
