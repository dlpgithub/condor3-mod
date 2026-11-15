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
| `Textures/tXXYY.dds` | per-tile DXT1/DXT3 (not DXT5) with full mip chain, 2048/4096/8192 px, alpha = water; `Textures\empty.dds` fallback. |
| `.tha .fha .oha .aha` | hash tables (see below) |
| `.tm3` | 3 bytes per 90 m cell, rows [y][x]: thermal value, roughness, slope (REQUIRED, see '.tm3' below) |
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
- `LandscapeOrigin` is only an FX parameter name (shader), not a file field.
- Landscape enumeration (`Landscapes\*.*`, "Checking landscapes", xref 0x6a8747), registry key `Software\Condor3\Landscapes\` (license key), "not registered -> demo" message at 0x830272.

## Landscape discovery (flight planner init, FUN_006a849c)
Enumerates `Landscapes\*.*` directories (skips `.` and `..`), requires `Landscapes\<N>\<N>.ini` to exist, reads `[General] Version` as an **integer** (default 0). Version==0 (absent or non-integer such as `1.02`) -> landscape is added to the list; a valid integer value -> an error message is shown instead. So a custom landscape just needs a folder + ini copied from an existing one (`Version=1.02`). No registry key is needed to be listed (registry `Software\Condor3\Landscapes\` is only the license/registration -> otherwise "Running demo version").

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

## C3D mesh format (solved, tools/c3d.py round-trips all 2880 Slovenia3 files byte-exact)

Little endian; ShortString = u8 len + chars.
- `'C3D'` (3 bytes, no version), u32 ngroups + ngroups x (u32 first_obj, u32 count) (editor writes 1 group (0,nobj)), u32 nobj
- per object: ShortString name, u32 first_vertex, u32 vertex_count, u32 first_index, u32 index_count, ShortString texture path, 4 x f32 RGBA, 3 x f32 params (O: 0.5,2.5,0; G: 0,0,0; autogen 0.01,250,0; meaning unverified)
- u32 nverts + nverts x 32 B (pos xyz, normal xyz, uv; f32; Z up, metres), u32 nidx + nidx x u32 absolute indices (CCW)
- Editor save may add an obfuscation offset to indices; not used in normal files.

### Airports (verified in game with tools/make_test_airport.py)
- `Airports/<Name>G.c3d` (ground) is mandatory (missing -> blue screen); `<Name>O.c3d` = objects. `.grs` is NOT required.
- Local frame: +Y = runway heading, +X = left (see 'C3D local frame of airports').
- A custom single-record .apt + custom G/O files load; airport is listed in the flight planner and boxes are visible in the sim.

### Airport G/O findings (session 3)
- **Our G meshes DO render** (white = Asphalt object with a copied Muck.dds, yellow = Grass3D quad with a copied Aigen_Grass.dds). Earlier "invisible" tests were wrong/inconclusive.
- `Grass3D` replaces the satellite terrain texture around the airport (texture vanishes when approaching, returns with distance). Stock `<Airport>_Grass.dds` is an UNCOMPRESSED 32-bit aerial photo (Aigen: 1840x1236, no mips); Grass3D UVs are a planar projection of the ground area onto that photo (Aigen: u 0.146-0.962, v 0.013-0.982 over ~610x1110 m).
- Stock `Asphalt` object texture is the per-airport `*Muck.dds` (DXT1 1024^2, mips); its UVs are also a planar projection over the asphalt area. Base asphalt/paint/gravel come from `Airports/Textures/{Asphalt,AsphaltPaint,Gravel,...}.dds` (landscape-relative).
- Object names are semantic: Asphalt, AsphaltPaint, AsphaltPaintYellow, Grass, GrassPaint, Gravel, Grass3D (found by name in Condor.exe).
- `Airports/<Name>.grs` (Testfield.grs, 843 KB) was created by the game itself at runtime -> it is a generated cache (size scales with ground area), not required input.
- Paused game: `move` changes the HUD altitude but the frame is not redrawn; unpausing is needed (P toggles; check the PAUSE text first).

### World axes for `move` / remote control (measured)
Glider world position (double x,y,z at object+0x6d8): **+x = WEST, +y = NORTH, z up** (moving x by +224 changed the HUD longitude by -0.003 deg, y by +113.6 changed latitude by +0.001). 1 unit = 1 m. This matches the terrain georeference (x west, y north). Quick-toggle technique: `move ...`, `kkey P`, `kkey P` in one cmd.txt redraws the paused frame.
Glider/pole collision ("collided with ground object") at the airport origin confirms the .apt lat/lon places the O objects correctly.

### C3D local frame of airports -- SOLVED (session 3, verified in game with tools/make_airport.py)
- **+Y = along the runway heading** (clockwise from north, `.apt` direction), **+X = LEFT of the heading**, +Z up, origin = `.apt` lat/lon, Z=0 = TERRAIN height at the origin (not the `.apt` elevation, see session 4).
  World (x west, y north): `world = origin + x*(cosH, sinH) + y*(-sinH, cosH)`.
- Evidence: in all stock airports the Grass3D/GrassPaint/Asphalt UVs are an exact affine function of (x,y) (max error 0.0000); the photo is a north-up aerial image: u grows to the east, v to the south, ~0.7 m/px
  (Aigen: 1840x1236 px photo for a 1295x869 m area). Expressing east/south in local coordinates gives (-cosH, sinH) / (-sinH, -cosH), i.e. x = left, y = forward.
- In-game check: markers at (+-37, +-450) appear at the runway ends, left markers on the left side of the heading; glider at local (10.5, -251) is placed correctly by `move`.
- **The F2 "external view" camera looks BACK at the glider from the front** (it faces the opposite direction of the heading) -- this fooled several earlier screenshots (left/right seemed mirrored).
- Glider start position (Synth8/Testfield): about 73 m to the right of the runway centre line (local x=-73, y=+10), i.e. beside the runway, not on it.
- Load distance of G/O: airport is loaded while the camera is within 24000 m and (view range - 1000 m); unloaded beyond 25000 m (Condor.exe 0x6e0d04..0x6e1535). Not a visibility cut at hundreds of metres.
- `tools/make_airport.py <landscape_dir> <Name> <lat> <lon> <heading> [length] [width]` builds a terrain-following Grass3D photo ground (photo cropped from the landscape tiles and rotated heading-up, uncompressed BGRA DDS like the stock one), an Asphalt strip with the stock-style Muck texture, four coloured corner markers, and the .apt record (elevation = terrain height at the origin).
- Open: the Asphalt strip looks faint/washed out (base asphalt texture not applied?), no paint objects (AsphaltPaint/GrassPaint) yet.

### Working custom airport (verified in game, tools/make_airport.py, commit after a626140)
Result: terrain-following Grass3D ground with our own photo (runway, edge lines and threshold bars painted into the photo), corner markers in O, custom .apt -> airport selectable, glider spawns on the centre line 250 m before the origin (local x=0, y=-250).
Lessons that cost most of the time:
1. **Triangle winding must be CCW in local (x,y)** (stock: 3130/3130 triangles positive area). Clockwise triangles are back-face culled -> invisible ground.
2. The Grass3D mesh must not lie BELOW the rendered terrain surface (at z = -0.3 it was hidden). Coplanar is fine and is what the stock files do (session 4); the +0.25 m used by make_airport.py is only needed because it samples the coarse 90 m .trn instead of the rendered tr3/tr3f surface.
3. Stock **`Asphalt` is not the runway surface**: it is a dirt/rubber "muck" overlay (`<Airport>Muck.dds` is ~white, mean 252). The runway itself is part of the aerial photo on `Grass3D`. We paint our own runway into the photo (1 m/px). `GrassPaint`/`AsphaltPaint` objects add paint lines on top (not needed).
4. Stock Asphalt (muck) pasted next to our own Grass3D rendered solid red; not investigated (probably a missing/unsupported detail texture binding).
5. The game applies a tiled detail texture over Grass3D near the camera (the "grid of tiles" seen from the cockpit); the photo itself needs to be rendered from above (e.g. 90+ m up) to judge it.
6. Spawn: glider starts at local (0, -250); G/O are loaded at flight start (restart needed after changing them), textures too.
7. Testing recipe: `kkey P` (pause), `move dW dN dz` (+x west, +y north), then `kkey P` twice quickly to redraw; F2 camera looks backwards at the glider.

## Session 4: airport/terrain relation solved, from-scratch demo landscape (tools/make_demo.py)

### How stock airports sit on the terrain (all 39 Slovenia3 airports analysed, then verified in game)
- **The G mesh IS the terrain surface.** For every stock airport, `z_mesh(x,y) - terrain(x,y)` is constant over all vertices of all G objects
  (std 0.00-0.11 m), where terrain = bilinear `22.5m/*.tr3f` if the tile has one, else `.tr3`. Paint/Asphalt objects are exactly coplanar (0.00).
  The editor "cuts" the meshes out of the height map, as the official guide says; nothing is lifted.
- **That constant equals `apt elevation - terrain height at the airport origin`** (e.g. Kapfenberg -14.0, Novo-Mesto +8.6, Aigen 0.0): the game puts
  local Z=0 at the terrain height under the origin and ignores the `.apt` elevation for placement. In-game proof: Talfeld with `.apt` elevation
  530 on terrain at 500 m -> glider spawns at z=500.8, ground mesh and hangar sit on the terrain exactly as with elevation 500.
  (`.apt` elevation is only informational: flight planner / task height.)
- A coplanar Grass3D (z = 0 on terrain that is an exact plane) renders without z-fighting, so the game draws the airport ground with a depth
  bias or replaces the terrain there. A mesh *below* the terrain is hidden.
- 63 of 2880 Slovenia3 tiles have a `.tr3f`; 4 airports (Fuerstenfeld, Klagenfurt, Noetsch, Lucko) have none and are simply flat in the `.tr3`.
  Where both exist they can differ a lot (Sostanj origin: tr3 380 m, tr3f 393.9 m) -> tr3f wins, also for the `.tha` hash (checked: tile 3642).
- Recipe for a custom airport: make the terrain under it the surface you want (in `.tr3`, and `.tr3f` if present), then build the G mesh from
  the same surface minus the height at the origin.

### What a landscape really needs (ProofValley: nothing copied from a stock landscape)
`tools/make_demo.py <Landscapes>/ProofValley` writes a 6x5-tile landscape (non-square, proves `idx = nTilesY*tx+ty`) that loads and flies:
`.ini .trn .tm3 .bmp .tha .fha .oha .aha .air(empty) .obj(empty) .cup .apt`, `HeightMaps/*.tr3` (+ one `22.5m/*.tr3f`), `ForestMaps/*.for`,
`Textures/t*.dds` + `empty.dds`, `Airports/<A>G.c3d`, `<A>O.c3d`, `Airports/Textures/<A>/`. Verified in game:
- **Not needed:** `World/`, `Images/`, `Autogen/` (and its textures), shared `Airports/Textures/*.dds` (Asphalt, GrassDetail, ...), `.grs` (the game
  writes `Airports/<A>.grs` itself on first flight).
- DDS headers can be written from scratch (flags 0xA1007, pitch = top mip size, caps 0x401008; identical to stock headers). Mixed tile sizes work
  (airport tile 4096, others 2048).
- **Water = texture alpha**: DXT3 tiles, land alpha 255 (nibble 15), water alpha ~51 (stock water pixels use nibbles 3..14, never 0-2). Water pixels
  render as a reflective water surface on the (flat) terrain. Tiles without water can stay DXT1.
- Forest map classes 1/2/3 produce 3D trees where set; the ground colour under them comes only from the tile texture.
- O file: untextured coloured objects and an object with its own DXT1 texture (`Textures/<A>/Hangar.dds`, path relative to `Airports/`) render.
  Windsock = an object named `Windsack1` (..3) consisting of one tiny triangle at the pole top (stock: 7.9 m, colour 0.8, params 0.8/5/0); the game
  draws the animated sock there.
- The flight planner shows the `.bmp` and the `.apt` airport; default flight plan = `Documents/Condor3/Pilots/<pilot>/Flightplan.fpl`
  (`Landscape=`, `TPName/TPPosX/TPPosY/TPPosZ` with X = metres west, Y = metres north; pilot from registry `LastPlayer`), NOT `Condor3/Pilots/`.
- Spawn again at local (0, -250) on the runway.
Screenshots: `docs/img/proofvalley_*.jpg`.


## Session 5: objects, airport ground layers, scenery, airspace, thermals (everything below verified in game on ProofValley unless marked RE)

Method notes: (1) the game ships its shader SOURCES in `Condor3/Effects/fx/*.fx` (EffectDX11File = objects/airport surfaces, EffectLand = terrain,
EffectGrass = 3D grass, EffectAutoObjects = trees) - they name every material/constant. (2) Delphi forms can be dumped from the exe resources
(RCDATA `TPF0`), which gives menu captions and event handler names; handler addresses are in the class method tables (u16 size, u32 address,
ShortString name). (3) `rea` pseudo-code times out on these binaries; a small annotated objdump wrapper (resolves Delphi string literals and
float constants) was enough. The official PDFs were read completely; their claims are folded in below.

### C3D, final picture
- Header groups = **LOD levels** (planes have 2-3 groups, e.g. `(0,104),(104,28),(132,1)`; landscape files always one group).
- Material record after the texture name: `R, G, B, A` then **`Spec, Shiny, Env`** (column titles of ObjectEditor's grid; shader constants
  `SpecularStrength`, `SpecularShininess`, `EnvironmentalStrength`). Defaults: O files 0.5 / 2.5 / 0, G files 0 / 0 / 0.
- **For landscape content the three numbers do nothing**: airport O objects, world objects and autogen are drawn with technique
  `Render_GroundObject` = `LightColor*N.L + Ambient` x MaterialColor x Texture, no specular, no environment map (test spheres with Spec 0 / Spec 1
  Shiny 50 / Env 1 look identical). They matter only for aircraft.
- Material colour multiplies the texture (white = unchanged). Material alpha < 1 blends (works without the stock `z...` naming and regardless of
  order in the file in a simple test; stock files still name transparent objects `z...` and sort them last = ObjectEditor "Sort objects by render order").
  Texture alpha is alpha-TESTED (`clip(a - AlphaRef)`): DXT3 textures with holes give cut-outs (fences, chain link).
- Texture paths are relative to the folder of the c3d (`Airports/`, `World/Objects/`, `Autogen/`); `/` and `\` both work; DXT1 with mips,
  DXT3, and uncompressed BGRA all load.
- Coordinate convention everywhere (airport local, world object, autogen): x and y as stored, z up, **triangles counter-clockwise when plotted
  with x to the right and y up, seen from the side the normal points to** (naive cross product agrees with the stored normals in 99.97 % of stock triangles).

### OBJ -> C3D (ObjectEditor importer, RE of ObjectEditor.exe 0x51eabc; reimplemented in tools/obj2c3d.py and verified with a tower object)
- "Open OBJ (Blender)" (Blender export Forward = Y, Up = Z, triangulated): `C3D (x, y, z) = OBJ (y, x, z)`; normals the same; `uv = (u, 1 - v)`.
  So Blender +X = C3D +Y (runway heading / north) and Blender +Y = C3D +X (left / west). That is why the official guide says "runway along X".
- "Open OBJ (Wings)" (Y up): `C3D (x, y, z) = OBJ (z, -x, y)`.
- Both mappings are reflections, so the triangle order is reversed to keep outward faces.
- Lines understood: `mtllib, o, # object, usemtl, v, vt, vn, f`; MTL: `newmtl, Kd` (colour), `d` (alpha), `Ks` (Spec), `Ns` (Shiny x 100), `Ke` (Env),
  `map_Kd` (texture). One material per object. Other ObjectEditor formats: X/CX, GAU (gauges), C3Dx (index-obfuscated C3D).

### Airport G file: the surface layers (shader `Render_AirportSurface`, loader Condor.exe 0x6e0d43..)
- Objects are found by name: `Asphalt, AsphaltPaint, AsphaltPaintYellow, Grass, GrassPaint, Gravel, Grass3D`. Everything is drawn coplanar on
  the terrain; surfaces must not overlap each other (cut holes), paint lies on top of its base surface.
- Colour = Light x **MaterialColor x object texture x (0.5 + detail) x (0.5 + big detail)**. The object texture is the per-airport "sub-texture"
  (aerial photo) or a "muck" texture; the detail textures are the tiling asphalt / paint / grass / gravel textures.
- Detail textures: the game's own `Condor3/World/Textures/{Asphalt,AsphaltPaint,AsphaltPaintYellow,Grass,GrassPaint,GrassDetail,Gravel}.dds`
  (so a landscape needs none), overridden per airport by `Landscapes/<L>/Airports/Textures/<AirportName>/{Asphalt,AsphaltPaint,Grass,GrassDetail,GrassPaint,Gravel}.dds`
  (folder name = airport name) when present.
- **Detail tiling comes from the UVs**: for every surface object the game fits `u` against one horizontal axis and `v` against the other over all
  vertices (least squares, 0x6e18d8) and tiles the detail every 2 m / 20 m using those slopes. For Asphalt/AsphaltPaint/AsphaltPaintYellow the
  axes are rotated by 90 deg: **u must vary along the runway (local y) and v across it** (stock muck textures: grad u = +y, grad v = -x).
  If u depends only on x and v only on y (slopes 0) the detail texture is lost and the surface is a flat untextured colour.
  Grass-type objects use the north-up sub-texture mapping (u east, v south), like stock.
- Official colour conventions (guide): Asphalt 0.5-0.8 grey (white looks bleached), AsphaltPaint 0.8-1, AsphaltPaintYellow (0.6, 0.6, 0), GrassPaint 0.8.
- **Grass3D** = ground mesh + sub-texture; additionally 3D grass blades (EffectGrass) are instanced on a 0.4 m grid over it. Blade colour comes
  from the sub-texture, blade size from the sub-texture's **alpha** (255 long, stock uses ~64-95 for mown strips; **alpha 0 = no blades**, e.g. on
  asphalt painted into the photo). Use an uncompressed 32-bit BGRA dds (stock) so the alpha is exact.
- `Airports/<A>.grs` = cache of the grass grid written by the game when missing: blocks of `u32 count + count x (u16 ix, u16 iy, f32 altitude)`,
  cells of 0.4 m on a north-up grid over the airport bounding box, bucketed 30 x 30 cells; 40 MB for a 600 x 1400 m airport. Delete it after
  changing the G file or the sub-texture.
- **Spawn point** = on the centre line, 200 m behind the START of the runway as given by the `.apt` length (local y = -length/2 + 200):
  Talfeld 900 m -> y = -250, Hangfeld 600 m -> y = -100.
- A sloped airport works: Hangfeld lies on a plane tilted 2 % along and 1 % across the runway, written as float heights in a `.tr3f`;
  G surfaces and O objects built on the same plane (z relative to the origin) sit exactly on the terrain, the glider stands on it.

### Airport O file
- All objects are solid (collision). Windsock: object named `Windsack1`..`Windsack3` = one equilateral triangle; centre = attachment point,
  **sock length = 100 x circumradius** (0.045 -> 4.5 m like stock, 0.10 -> 10 m: confirmed). The pole is ordinary geometry.

### World objects: `<L>.obj` + `World/Objects/` (loader Condor.exe 0x6de460)
- `<L>.obj` is a list of 152-byte records (file may be empty): `f32 x (west), f32 y (north), f32 z, f32 scale, f32 rotation, ShortString[131] name`.
  z is ignored (the game samples the terrain), scale is uniform, rotation is in radians and is the **heading of the model's +Y axis** (0 = north,
  pi/2 = east); at rotation 0 model +X points west. The name may carry `.c3d` or not.
- The model is looked up in `Landscapes/<L>/World/Objects/` first, then in the game's `World/Objects/` (e.g. `Winch`, `Pylon` exist there).
  Textures relative to that folder (`Textures/...`).

### Autogen: `Autogen/oXXYY.c3d` (+ `Autogen/Textures/`)
- One ordinary C3D per tile (name like the height tile). Vertex coordinates: **x west / y north relative to the tile CENTRE, z = absolute altitude**
  (checked against the terrain for three stock tiles, and with our own houses). Stock content: OSM buildings, pylons; params mostly 0/0/0.
- `<L>.oha` (MD5 per tile) is not read by the game (only the editor/validator); a tile without autogen simply has no file.

### Terrain details
- `.trn` header (36 bytes): `u32 width, u32 height, f32 90 (cell), f32 dx = -90, f32 dy = +90, f32 E0, f32 N0, u32 UTM zone, u32 'N'`.
  The "Landscape not correctly calibrated" check (0x6aee47) only requires `80 <= |dx| <= 100` and `80 <= |dy| <= 100`.
- **The UTM zone of the header is honoured**: the same landscape written as zone 31 (airport and airspace lat/lon recomputed) shows airports and
  airspace at the right map position and flies normally. Southern hemisphere not tested.
- Water: a terrain texel is water when the tile texture's **alpha < 0.8** (EffectLand); use DXT3 (DXT1's 1-bit alpha gives jagged shores, guide).
- Tile textures get a tiled detail texture multiplied in (x30 per tile); `RealtimeShading=0` in the ini lights the terrain as if flat.
- Files that define a landscape for the game's file list (0x6956bd): `.air .trn .bmp .tm3 .obj .apt .tha .fha .aha .ini`; the `.bmp` must have
  exactly the `.trn` dimensions ("Map bitmap dimensions differ from TRN header dimensions!").

### Airspace: `<L>.air` + `<L>.aha`
- `.air` is OpenAir text. Parser (0x666b3c) understands `AC, AN, AY, AL, AH` (GND / SFC / FL / FT / M, MSL / AGL), `V X=`, `V D=+/-`, `DP`, `DC` (NM), `DA`, `DB`.
- **`.aha` is mandatory and checked**: decimal hash of the `.air` bytes (same hash as tha/fha, idx 0). Mismatch or missing file raises
  "<L>: modified airspace file detected or airspace hash file (.aha) missing!". Empty `.air` -> `0`.
- Verified: a `DC 2` circle around Talfeld is drawn in the flight planner, listed on the PDA, and "entered airspace" is announced.

### Thermal map `.tm3` (RE of the thermal generator 0x75ad94 and updater 0x75a3f9)
- Number of thermals ~ landscape area x length of the day. For every candidate position the 3 bytes of the cell are read:
  - byte1 (terrain roughness) decides the mode: **> 0.5 (stddev > 50 m) = mountain mode**, where the sun direction against the terrain slope
    drives the thermal; **otherwise flat-land mode**, where probability and strength are proportional to **byte0 (the painted thermal map)**
    together with the weather's flats activity (guide: black = no thermals; water 0, deciduous 64, forest/grass 102, fields 178).
  - during a thermal's life byte1 is sampled again (bilinear): above 0.5 the strength is scaled by `1 - 3 (v - 0.5)`.
  - byte2 (mean slope): consumer not found.

### Flight plan and test automation
- `scripts/flytest.sh [views]` starts the game, enters the flight, pauses and visits camera positions given as ABSOLUTE world coordinates
  (`tools/views.py name:airport:x:y[:height] ...` writes them from airport-local coordinates); the glider drifts whenever the sim runs, so the
  script reads the position back before every move. Ground-level cameras stay level; after several airborne moves the glider pitches up.

### Still open
- Southern hemisphere / very different latitudes; landscapes larger than the tested 6x5 with other tile counts are covered by stock (60x48).
- What exactly `.tha`/`.fha` mismatches trigger (official text: treated as cheating); consumer of tm3 byte2; the red stock Asphalt seen in session 3
  (a degenerate-UV Asphalt renders flat grey, not red).
- Rotation angle used for the detail fit of grass-type surfaces (field +0x5c of the airport object); north-up sub-texture mapping works.
- C3Dx obfuscation, GAU, plane-specific C3D features (animation morph targets), `Images/` (optional splash picture, not needed).
