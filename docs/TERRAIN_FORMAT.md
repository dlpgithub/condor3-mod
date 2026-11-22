# Condor 3 landscapes: complete technical reference

Everything this project has worked out about how Condor 3 stores, loads and renders landscapes (terrain, textures, forests, airports,
objects, airspace, thermals), written so that a new session can continue without the earlier chats.
State: 2026-11-16, repo branch `terrain`. Companion file: `HANDOVER_TERRAIN.md` (how to work with the user, machines, recipes).

**Goal of the project:** understand the formats completely so that landscapes can be built with OUR OWN tools. The proof is
`tools/make_demo.py`, which writes a complete flyable landscape (`Landscapes/ProofValley`) without copying a single stock file.

**Evidence tags used below**
- `[G]` verified in the running game (screenshot or log)
- `[D]` verified against stock data (Slovenia3, usually all files of a kind)
- `[RE]` read from disassembly (address given); behaviour not provoked in game unless also tagged `[G]`
- `[DOC]` stated by the official guides (toolkit PDFs)

---------------------------------------------------------------------------------------------------------------------------------

## 1. Orientation for a new session

| thing | where |
|---|---|
| Game (CrossOver bottle `Condor3`) | Mac `/Users/lennard/Condor3`, in device bash `$HOME/mnt/Condor3` |
| Repo | Mac `/Users/lennard/Desktop/condor3-mod`, in device bash `$HOME/mnt/condor3-mod` |
| Official toolkit | `/Users/lennard/Downloads/landscape-toolkit` (`CLT3/LandscapeEditor.exe`, `ObjectEditor.exe`, `scenerycheck3.exe`, two PDFs, OSM exporter) |
| Stock landscape (ground truth, never modify) | `Condor3/Landscapes/Slovenia3` (60 x 48 tiles, 39 airports) |
| Our landscapes | `Crop8` (crop of Slovenia3), `Synth8` (old debug-coloured test bed), **`ProofValley`** (the demo, built by `make_demo.py`) |
| Game shader SOURCES | `Condor3/Effects/fx/*.fx` (contain the user's own mod edits marked `[MOD]`; the original logic is intact) |
| Pilot data / default flight plan | Mac `~/Documents/Condor3/Pilots/Bar_Foo/Flightplan.fpl` |

Rebuild and test the demo:
```
cd tools
python3 make_demo.py ~/mnt/Condor3/Landscapes/ProofValley      # KEEP=1 resumes textures, ONLY=airport skips tile textures
python3 views.py t_runway:t:0:-430:1.0 h_shed:h:45:-45:1.5      # camera list in airport-local coordinates -> Condor3/postfx/views.txt
# on the Mac (desktop-commander start_process):  bash scripts/flytest.sh /Users/lennard/Condor3/postfx/views.txt
# screenshots land in Condor3/postfx/shots/<name>.png (planner.png and start.png always)
```
A full device-bash call is limited to 180 s and background processes die when the call returns, so long builds must be resumable.

---------------------------------------------------------------------------------------------------------------------------------

## 2. Coordinates, units, conventions

### 2.1 World frame `[G]`
- Metres. **x = WEST of the landscape's east edge, y = NORTH of its south edge, z = altitude.**
  `x = E0 - Easting`, `y = Northing - N0` with E0/N0/zone/hemisphere from the `.trn` header (UTM, WGS84). `tools/utm.py`.
- The glider position (doubles at aircraft object +0x6d8/+0x6e0/+0x6e8) is in this frame; flight-plan `TPPosX/Y` too.
- Seen on a normal map (east to the right) the frame is mirrored. All files use it consistently, see 2.5.

### 2.2 Grid hierarchy `[D][DOC]`
- Height cell 90 m (`.trn`, `.tm3`, `.bmp`); fine posts 30 m (`.tr3`) and 22.5 m (`.tr3f`).
- **Tile** (the guides call it "patch") = 64 cells = 192 x 30 m = **5760 m**. Tile index `(tx, ty) = (x // 5760, y // 5760)`, so tile 0000 is the
  SOUTH-EAST corner, tx counts west, ty counts north. File names use `%02d%02d` (tx, ty); the game tries `%03d%03d` first.
- The guides' "tile" = 4 x 4 of ours = 23040 m (only relevant inside LandscapeEditor's `Working/` folder).
- Landscape size = whole number of tiles; non-square works (`ProofValley` 6 x 5 `[G]`, Slovenia3 60 x 48).
- The glider cannot fly closer than about one tile (5 km) to the border `[DOC]`.

### 2.3 Orientation of every raster (tile origin `(x0, y0) = (tx*5760, ty*5760)` = SE corner)
| data | indexing | evidence |
|---|---|---|
| `.trn` heights | `a[ix][iy]`, iy fastest; x = ix*90, y = iy*90 | `[G]` |
| `.tr3` (193 x 193), `.tr3f` (257 x 257) | `a[i][j]`, j fastest; x = x0 + i*step, y = y0 + j*step; edge posts shared with neighbours | `[D][G]` |
| `.for` (512 x 512) | `a[i][j]`, j fastest; cell centres x = x0 + (i+.5)*11.25, y = y0 + (j+.5)*11.25 | `[RE][G]` |
| `tXXYY.dds` | ordinary north-up image: column 0 = WEST edge (x max), row 0 = NORTH edge (y max) | `[D][G]` |
| `.tm3` | `a[iy][ix][3]`, row 0 = south edge, column 0 = east edge (pairs with `trn[ix][iy]`) | `[D]` |
| `.bmp` | normal map picture, north up, west left; stored bottom-up, so file row 0 = south; pixel column c <-> ix = W-1-c | `[G]` |

### 2.4 Airport local frame (G and O files) `[D][G]`
- Origin = the `.apt` lat/lon. **+Y = runway heading** (the `.apt` direction, clockwise from north), **+X = LEFT of the heading**, +Z up.
- `world = origin + x*(cos H, sin H) + y*(-sin H, cos H)`; inverse `x = dW*cos H + dN*sin H`, `y = -dW*sin H + dN*cos H`
  (`Airport.to_world / to_local` in `make_demo.py`).
- **Z = 0 is the terrain height under the origin, NOT the `.apt` elevation** (section 6.2).

### 2.5 Mesh conventions (all C3D files) `[D][G]`
- Coordinates are used as stored (airport: local frame; world object: x west / y north at rotation 0; autogen: tile-centred world axes).
- **Triangles are counter-clockwise when plotted with x to the right and y up, looking against the normal**; equivalently the naive cross product
  `(p1-p0) x (p2-p0)` of the stored coordinates points along the stored normals (99.97 % of stock autogen triangles, 3130/3130 ground triangles).
  Clockwise ground triangles are culled and invisible.
- UV origin is the top-left of the image (v grows downwards).

---------------------------------------------------------------------------------------------------------------------------------

## 3. The landscape folder

`Condor3/Landscapes/<L>/`. Listing in the flight planner (`[RE]` 0x6a849c, `[G]`): a folder with `<L>.ini` whose `[General] Version` is NOT a plain
integer (stock `1.02`) is listed as `<L> [1.02]`; no registry entry is needed.

| file | needed? | content | writer in our tools |
|---|---|---|---|
| `<L>.ini` | yes | `[General]` `Version=1.02`, `RealtimeShading=1` (0 = terrain lit as if flat) | `make_demo.main` |
| `<L>.trn` | yes | header + 90 m heights (4.1) | `make_demo.main` |
| `HeightMaps/hXXYY.tr3` | yes | 30 m heights per tile (4.2) | `make_demo.main` |
| `HeightMaps/22.5m/hXXYY.tr3f` | per tile, optional | float heights, wins over `.tr3` (4.2) | `make_demo.main` |
| `<L>.tha` | yes | hash per height tile (4.7) | `terrain_hash.condor_hash` |
| `ForestMaps/XXYY.for` | optional per tile | tree classes (4.3) | `make_demo.forest_class` |
| `<L>.fha` | yes | hash per forest tile (4.7) | `terrain_hash.condor_hash` |
| `Textures/tXXYY.dds`, `Textures/empty.dds` | yes (missing tile -> empty.dds) | ground texture, alpha = water (4.4) | `make_demo.write_dds`, `tile_texture` |
| `<L>.tm3` | **yes** (missing -> game quits after "Initializing Thermals.") | thermal map (4.5, 8) | `make_tm3.make_tm3` |
| `<L>.bmp` | yes | flight-planner map, exactly the `.trn` dimensions (4.6) | `make_demo.main` |
| `<L>.apt` | yes (may list 1..n airports) | airport records (4.8) | `apt.pack_one` |
| `Airports/<A>G.c3d` | **yes per airport** (missing -> blue screen `[DOC][G]`) | ground surfaces (6) | `make_demo.build_airport` |
| `Airports/<A>O.c3d` | per airport | solid objects, windsocks (6.6) | `make_demo.build_airport` |
| `Airports/Textures/<A>/...` | as referenced | sub-texture, muck, object textures, detail overrides | `make_demo.build_airport` |
| `Airports/<A>.grs` | no | grass cache, written by the game (6.5) | - |
| `<L>.air` + `<L>.aha` | **both yes**, `.air` may be empty with `.aha` = `0` | airspace + its hash (4.9) | `make_demo.build_scenery` |
| `<L>.obj` | yes, may be empty | world object placement list (7.1) | `make_demo.build_scenery` |
| `World/Objects/*.c3d` (+`Textures/`) | optional | models for `.obj` (7.1) | `make_demo.build_scenery`, `obj2c3d.py` |
| `Autogen/oXXYY.c3d` (+`Textures/`) + `<L>.oha` | optional | per-tile scenery mesh + index/MD5 list (7.2) | `make_demo.build_scenery` |
| `<L>.cup` | optional | SeeYou waypoints (4.10) | `make_demo.main` |
| `World/Textures`, `Images/` | no | stock landscapes carry them; not needed `[G]` | - |

The game's own "landscape file list" (`[RE]` 0x6956bd): `.air .trn .bmp .tm3 .obj .apt .tha .fha .aha .ini`.
`ProofValley` proves the "needed" column: it contains exactly the rows marked yes/optional above and nothing from Slovenia3 `[G]`.

---------------------------------------------------------------------------------------------------------------------------------

## 4. File formats

All binary values little endian. "ShortString" = Pascal string: u8 length + characters.

### 4.1 `<L>.trn` - coarse height map `[D][G][RE]`
```
u32 width (cells, = tilesX*64)      u32 height (= tilesY*64)
f32 90.0   cell size
f32 dx = -90.0, f32 dy = +90.0      "calibration": the game refuses unless 80 <= |dx| <= 100 and 80 <= |dy| <= 100
                                    ("Landscape not correctly calibrated.", check at 0x6aee47; header is read to [0x792a68]+0x4744)
f32 E0  UTM easting of the EAST edge        f32 N0  UTM northing of the SOUTH edge (southern hemisphere: with the 10 000 km false northing)
u32 UTM zone                        u32 hemisphere character 'N' (78) or 'S'
u16 heights[width][height]          metres, [ix][iy], iy fastest
```
- Slovenia3: 3840 x 3072, E0 645120, N0 5022720, zone 33 N. ProofValley: 384 x 320, E0 460800, N0 5184000.
- Zone and hemisphere are honoured: ProofValley rebuilt as zone 31 and as hemisphere S shows airports/airspace at the right map place and flies;
  in the south the sun is in the north `[G]` (`UTMZONE=31` / `HEMI=S` in `make_demo.py`).
- `[RE]` (not checked in game): the loader fades the outer 32 cells to sea level; border tiles fade their outer half.
- E0/N0 are f32: keep them exactly representable (multiples of small powers of two below 2^24).

### 4.2 `HeightMaps/hXXYY.tr3` and `HeightMaps/22.5m/hXXYY.tr3f` `[D][G]`
- `.tr3`: 193 x 193 u16 metres, no header, 30 m posts (74498 bytes). Missing file -> heights flagged invalid `[RE]`.
- `.tr3f`: 257 x 257 f32 metres, 22.5 m posts (264196 bytes). This is the engine's native patch resolution; a `.tr3` is resampled bilinearly to it.
- **If a `.tr3f` exists for a tile it is used instead of the `.tr3`** - for rendering, for the airport ground (6.2) and for the `.tha` hash
  (checked: Slovenia3 tile 3642 hash matches the tr3f bytes; Sostanj origin tr3 = 380 m but tr3f = 393.9 m and the airport sits at 393.9).
  63 of 2880 Slovenia3 tiles have one: the airport tiles. Purpose `[DOC]`: sub-metre precision so airports can keep their real profile.
- In ProofValley both airport tiles have a `.tr3f` holding the exact analytic plane (`make_demo.ground`), while their `.tr3` is the rounded master.

### 4.3 `ForestMaps/XXYY.for` `[RE][G]`
- 512 x 512 u8 (262144 bytes, 11.25 m cells): 0 none, 1 coniferous, 2 deciduous, 3 both (`[RE]` LandscapeEditor export 0x5ed4c0: built from
  `Working/ForestMaps/s<CCRR>.bmp` -> +1 and `b<CCRR>.bmp` -> +2, array[i][j] = bitmap pixel (511-i, 511-j)).
- The game instances 3D trees where the value is non-zero (EffectAutoObjects, loaded for about 4 x 4 tiles around the camera `[RE]`).
  The ground colour under the trees comes only from the tile texture `[G]`.

### 4.4 `Textures/tXXYY.dds` `[D][RE][G]`
- Loader `[RE]` 0x76c9d0: path `Textures\t%03d%03d.dds`, else `t%02d%02d.dds`, else `Textures\empty.dds` (stock: 256 x 256 DXT3).
- Square, power of two; seen 2048 (standard, 2.8125 m/px), 4096, 8192; mixed sizes in one landscape work `[G]`.
- **FourCC must be `DXT1` or `DXT3`; DXT5 is rejected.** ALL mips down to 1 x 1 must be present and the **file size must be exactly**
  `128 + sum over mips of max(1,(s+3)/4)^2 * (8 | 16)`.
- Header we write (`make_demo.dds_header`, byte-identical to stock): size 124, flags 0xA1007, height, width, pitch = bytes of mip 0, depth 0,
  mip count = log2(S)+1, pixel format {32, flags 4, fourcc}, caps 0x401008.
- **Water**: a texel is water when **alpha < 0.8** (`EffectLand.fx`: animated bump map, specular, flat reflective surface) `[G]`. Use DXT3 with
  land alpha 255 and water about 51 (stock water texels have 4-bit alpha 3..14, never 0-2). DXT1 has only 1-bit alpha -> jagged shores `[DOC]`;
  tiles without water can be DXT1.
- The shader multiplies a tiled detail texture into the ground (30 repeats per tile).
- Texture residency `[RE]` 0x76d094: Chebyshev tile distance to the camera < 4 -> LOD 0, < 7 -> LOD 1, < 11 -> LOD 2, else unloaded;
  the top `lod + 2 - TextureQuality` mips are skipped.
- Own codec: `make_demo.bc1_blocks`, `alpha_blocks`, `write_dds` (encode); `make_airport.decode_dxt` (decode DXT1/DXT3).

### 4.5 `<L>.tm3` - thermal / terrain-shape map `[D][RE]`
- `width*height*3` bytes (same width/height as `.trn`), `a[iy][ix][3]`:
  - byte0 = painted thermal map (0 = no thermals ... 255 strongest). Guide values: water 0, deciduous trees 64, forest and grass 102, fields 178.
  - byte1 = `clamp(stddev(heights in disk) * 0.01, 0, 1) * 255` (terrain roughness)
  - byte2 = `clamp(mean((h0 - h)/dist) * 0.005, -0.5, 0.5) * 254 + 127` (mean slope to the neighbourhood, 127 = flat)
  - disk = offsets (i, j) with i^2 + j^2 <= 64, sampled at cells (x+2i, y+2j), `dist = sqrt(i^2+j^2)`.
- `[RE]` LandscapeEditor "Export thermal map" 0x5ecdf0; `tools/make_tm3.py` reproduces Slovenia3.tm3 to <= 1 LSB.
- How the game uses it: section 8.

### 4.6 `<L>.bmp` - flight-planner map `[G][RE]`
- Windows BMP, 32 bpp, uncompressed, bottom-up, 54-byte header, **width x height must equal the `.trn` dimensions** (else "Map bitmap
  dimensions differ from TRN header dimensions!", `[RE]` 0x6af164). 90 m per pixel, north up, west left.

### 4.7 `<L>.tha`, `<L>.fha` - integrity hashes of height and forest tiles `[D][RE]`
- Text, CRLF lines `"%06d %d"`: key `tx*1000 + ty`, then the hash of the tile's file bytes (`.tr3f` when present, else `.tr3`; `.for`).
```
s    = (sum(bytes) + len(bytes)) mod 2^32
seed = ((idx + 0x14A68F2) * 0x41C64E6D + 1) mod 2^32        idx = tilesY * tx + ty
hash = round(s * seed / 2^32)
```
- `[RE]` hash 0x4b46c8; `tools/terrain_hash.py`. Reproduces 2871/2880 stock values exactly, the rest are off by 1; the game tolerates |diff| <= 1.
- What a mismatch does: section 9.2. LandscapeEditor regenerates them with `-hash <Landscape>` `[DOC]`.

### 4.8 `<L>.apt` - airport list `[D][G]`
72-byte records, no header (`tools/apt.py`, byte-exact round trip of the 39 stock records; `[RE]` LandscapeEditor dialog 0x5e3340, loader 0x5ee2a0):
```
0x00 ShortString[35] name      also the file name stem: Airports/<name>G.c3d, <name>O.c3d, Airports/Textures/<name>/
0x24 f32 latitude   0x28 f32 longitude (degrees, negative = south/west)
0x2c f32 elevation  INFORMATIONAL (planner/task); the 3D airport is placed on the terrain regardless (6.2)
0x30 i32 direction code = (whole_degrees + 1000) * 100 + hundredths      e.g. 106493 = 64.93 deg, clockwise from north
0x34 i32 runway length [m]     also defines the spawn point (6.7)
0x38 i32 runway width [m]
0x3c u8 asphalt (1) / grass (0), 3 pad bytes
0x40 f32 radio frequency [MHz]
0x44 u8 primary direction reversed   0x45 u8 tow primary left side   0x46 u8 tow secondary left side   0x47 u8 0
```
Georeference check: all 39 stock airports land within 2 m (median) of their mesh position.

### 4.9 `<L>.air` + `<L>.aha` - airspace `[RE][G]`
- `.air` is **OpenAir** text. Parser `[RE]` 0x666b3c understands `AC, AN, AY, AL, AH` (altitudes `GND`, `SFC`, `FL n`, `n FT`, `n M`, with `MSL`/`AGL`),
  `V X=dd:mm:ss N ddd:mm:ss E`, `V D=+` / `V D=-`, `DP` (polygon point), `DC r` (circle, NM), `DA`, `DB` (arcs). `*` lines are comments.
- `.aha` = one text line with the decimal hash of the `.air` bytes: same function as 4.7 with idx 0 (`[RE]` LandscapeEditor 0x49ec30).
  Empty `.air` -> `0`.
- **The game checks it at landscape load** (`[RE]` 0x6af28b): mismatch or missing file raises
  "<L>: modified airspace file detected or airspace hash file (.aha) missing!".
- `[G]` ProofValley: `AC D / AN TALFELD CTR / AL GND / AH FL 65 / V X=... / DC 2` is drawn in the planner, shown on the PDA and announced in flight.

### 4.10 `<L>.cup` - waypoints `[DOC]`
SeeYou CUP text: header line `name,code,country,lat,lon,elev,style,rwdir,rwlen,freq,desc`, then e.g.
`"Talfeld",Talfeld,,4655.932N,01413.338E,500.0m,5,068,900.0m,123.500,`. The game drops waypoints outside the landscape at flight start.

---------------------------------------------------------------------------------------------------------------------------------

## 5. C3D mesh files

Used for airports (G, O), world objects, autogen, and aircraft. `tools/c3d.py` (`parse`, `write`, `build`) round-trips all 2880 stock autogen
files and all airport files byte-exact. Layout from ObjectEditor "Save to C3D" (`[RE]` 0x5218bc):
```
'C3D'                                 3 bytes, no version
u32 ngroups, ngroups x (u32 first_object, u32 object_count)     groups = LOD levels; landscape files always 1 group (0, nobj);
                                                                aircraft have 2-3, e.g. (0,104),(104,28),(132,1) [D]
u32 nobj
nobj x { ShortString name
         u32 first_vertex, u32 vertex_count, u32 first_index, u32 index_count      index values are ABSOLUTE vertex numbers
         ShortString texture path ('' = none)
         f32 R, G, B, A                material colour
         f32 Spec, Shiny, Env }        = shader SpecularStrength, SpecularShininess, EnvironmentalStrength
u32 nverts, nverts x { f32 x, y, z, f32 nx, ny, nz, f32 u, v }  (32 bytes)
u32 nidx,   nidx x u32                triangle list
```
ObjectEditor's grid columns are `Name, Vertex, LOD, Texture, Spec, Shiny, Env, Red, Green, Blue, Alpha` (`[RE]` 0x52f9ba), which names the fields.
"Save to C3Dx" writes an index-obfuscated variant (not used by landscapes).

### 5.1 How ground objects are shaded `[G]` (technique `Render_GroundObject` in `EffectDX11File.fx`)
`colour = (LightColor * max(N.L, 0) + Ambient) * cloudShadow * MaterialColor * Texture`, alpha = Texture.a * MaterialColor.a, fogged.
- **Spec / Shiny / Env have no effect** on airport O objects, world objects and autogen (test spheres with 0/0/0, 1/50/0 and 0/0/1 look identical).
  They matter for aircraft only. Stock values: O files 0.5 / 2.5 / 0, G files 0 / 0 / 0, some autogen 0.01 / 250 / 0.
- Material colour multiplies the texture: use white for textured objects, the colour itself for untextured ones.
- Material alpha < 1 blends. In our test this worked for an object named `T4_Alpha` in the middle of the file as well as for `zT5_Alpha` at the
  end; stock files name transparent objects `z...` and put them last (ObjectEditor "Sort objects by render order"), follow that.
- Texture alpha is alpha-TESTED (`clip(a - AlphaRef)`): a DXT3 texture with alpha 0 areas gives cut-outs (fences).
- Texture paths are relative to the folder of the c3d (`Airports/`, `World/Objects/`, `Autogen/`); `/` and `\` both work. DXT1 with mips, DXT3 and
  uncompressed 32-bit BGRA load.

### 5.2 OBJ -> C3D, as ObjectEditor imports it `[RE][G]` (`tools/obj2c3d.py`)
`[RE]` ObjectEditor.exe: menu handlers `Open1Click` 0x52d09c (Blender) and `OpeClick` 0x52d070 (Wings) call 0x52ced8 -> line parser 0x51eabc.
- **Blender** (export Forward = Y, Up = Z, triangulated): `C3D (x, y, z) = OBJ (y, x, z)`, normals alike, `uv = (u, 1 - v)`.
  So Blender +X -> C3D +Y (runway heading / north) and Blender +Y -> C3D +X (left / west): this is why the official guide puts the runway on X.
- **Wings** (Y up): `C3D (x, y, z) = OBJ (z, -x, y)`.
- Both maps are reflections, so the triangle order must be reversed (this follows from 2.5; the tower test object is lit and oriented correctly).
- Lines read: `mtllib, o, "# object", usemtl, v, vt, vn, f`. MTL: `newmtl, Kd` -> colour, `d` -> alpha, `Ks` -> Spec, `Ns` -> Shiny*100,
  `Ke` -> Env, `map_Kd` -> texture (also `Ka, illum, map_Ka/Ks/Ke` are parsed). One material per object (no multi-materials `[DOC]`).
- ObjectEditor also opens X/CX and GAU files and can batch-convert C3D to OBJ.

---------------------------------------------------------------------------------------------------------------------------------

## 6. Airports

### 6.1 Files and loading
- `.apt` record (4.8) + `Airports/<A>G.c3d` (mandatory) + `Airports/<A>O.c3d` + textures. Loader `[RE]` Condor.exe 0x6e0c00-0x6e1600.
- G and O are loaded while the camera is within 24000 m (and within view range - 1000 m) and unloaded beyond 25000 m `[RE]` 0x6e0d43/0x6e1540.
- G/O files and textures are read at flight start: restart the game after changing them `[G]`.
- The files are position independent: Aigen's files work at another place if only the `.apt` lat/lon changes `[G]`.

### 6.2 Relation to the terrain - the key fact `[D][G]`
- **The G mesh is the terrain surface itself.** For all 39 stock airports, `z_mesh - terrain` is constant over every vertex of every G object
  (std 0.00-0.11 m), terrain = bilinear `.tr3f` if the tile has one, else `.tr3`. Paint and asphalt are exactly coplanar (0.00). The official
  workflow `[DOC]` exports the 22.5 m patch as OBJ, smooths it in Blender, re-imports it as `.tr3f`, and CUTS the surfaces out of that mesh.
- **Local Z = 0 is the terrain height under the airport origin.** The constant above always equals `apt elevation - terrain(origin)`
  (Kapfenberg -14.0 m, Novo-Mesto +8.6 m, Aigen 0.0). `[G]`: Talfeld with `.apt` elevation 530 on terrain at 500 m sits on the ground unchanged
  (`APT_ELEV_OFFSET=30` in `make_demo.py`).
- A coplanar ground mesh renders without z-fighting `[G]`; a mesh BELOW the terrain is hidden (-0.3 m was invisible); a mesh above floats.
- **Triangulation of the rendered terrain** `[D]`: stock Grass3D vertices lie on the world-aligned 22.5 m post grid, and each grid cell is split
  by an alternating diagonal: cell (i, j) (post indices, i west, j north, counted from the landscape origin) with i+j even uses the diagonal
  (i,j)-(i+1,j+1), odd uses (i+1,j)-(i,j+1) (Aigen, Bovec, Celje: 100 % of grid triangles). A ground mesh built from the same posts and
  diagonals is the terrain surface exactly, whatever its shape (`Terrain` class and `stage_airports` in `tools/tiles2condor.py`).
- Pavement drawn through `Grass3D` (photo x grass detail) looks bleached and grainy `[G]` (Orly): put only grass into Grass3D and leave paved
  areas to the terrain texture or to Asphalt objects.
- 4 stock airports have no `.tr3f` and are simply flat in the `.tr3`. Integer `.tr3` heights cannot represent a slope exactly, so a sloped
  airport needs a `.tr3f`.
- **Recipe**: first make the terrain under the airport the surface you want (ProofValley: an exact plane, blended into the surroundings over
  350 m, `make_demo.build_master`; written to the `.tr3f` by `make_demo.ground`), then build every G vertex as `z = surface(x, y) - surface(origin)`.
  Hangfeld proves the sloped case: plane tilted 2 % along and 1 % across the runway; ground, shed and glider sit on it `[G]`.
- Analysis scripts: `tools/re/uvfit.py`; the mesh-vs-terrain comparison is described here and easy to redo with `c3d.py` + `apt.py` + `utm.py`.

### 6.3 G file: surface objects `[RE][G]`
Objects are identified by NAME (case-insensitive in stock): `Grass3D`, `Grass`, `GrassPaint`, `Asphalt`, `AsphaltPaint`, `AsphaltPaintYellow`, `Gravel`.
- Rendering (technique `Render_AirportSurface`): `Light * MaterialColor * ObjectTexture * (0.5 + Detail) * (0.5 + BigDetail)`, normal fixed to +Z.
  - ObjectTexture = the texture named in the object: the airport "sub-texture" (6.4) or a "muck" texture (stock `<A>Muck.dds`: almost white DXT1
    1024^2 with tyre marks). Empty = white.
  - Detail / BigDetail = tiling surface textures: the game's own `Condor3/World/Textures/{Asphalt,AsphaltPaint,AsphaltPaintYellow,Grass,
    GrassPaint,GrassDetail,Gravel}.dds` (so a landscape needs none), overridden per airport by
    `Landscapes/<L>/Airports/Textures/<AirportName>/{Asphalt,AsphaltPaint,Grass,GrassDetail,GrassPaint,Gravel}.dds` when that file exists.
- **Detail tiling is derived from the UVs** (`[RE]` 0x6e18d8): per object a least-squares fit of u against one horizontal axis and v against the
  other gives `ScaleX = du/dX`, `ScaleY = dv/dY`; the shader tiles the detail every 2 m and 20 m with them. For `Asphalt`, `AsphaltPaint`,
  `AsphaltPaintYellow` the axes are rotated by 90 deg: **u must vary along the runway (local y) and v across (local x)**, like the stock muck
  mapping (`u = (y + L/2)/L`, `v = 0.5 - x/L`). If u depends only on x and v only on y the fit is zero and the surface is a flat untextured
  colour `[G]`. Grass-type objects use the north-up sub-texture mapping.
- Surfaces must not overlap each other (cut holes: Grass3D has holes where Asphalt and Gravel are); paint objects lie on top of their base
  surface, coplanar `[DOC][G]`. "To use AsphaltPaint you must have Asphalt, to use GrassPaint you must have Grass" `[DOC]`.
- Colours `[DOC][D]`: Asphalt 0.5-0.8 grey (1.0 looks bleached; stock with muck often 1.0), AsphaltPaint 0.8-1.0,
  AsphaltPaintYellow (0.6, 0.6, 0), GrassPaint 0.8. Params 0/0/0.
- ProofValley/Talfeld contains all six types (`make_demo.build_airport`, helper `plane_mesh`, `cells`).
- History note: session 3 saw a stock Asphalt object render solid red next to our own Grass3D. Not reproduced: a degenerate-UV Asphalt is flat
  grey. Cause unknown.

### 6.4 The sub-texture (aerial photo) `[D][G]`
- One texture per airport, mapped on `Grass3D` (and usually GrassPaint, Gravel, sometimes Asphalt). Stock: **uncompressed 32-bit BGRA dds, no
  mips** (Aigen: 1840 x 1236 px for 1295 x 869 m, about 0.7 m/px), **north-up**: u grows east, v grows south. In all stock airports the UVs are an
  exact affine function of local (x, y).
- Near the airport this photo replaces the tile texture (drawn on the Grass3D mesh); from far away the tile texture shows, so paint the same
  airport into both (`make_demo.color` is used for both).
- **Alpha = 3D grass length** (6.5).
- Writer: `make_demo.write_bgra` (header: flags 0xA1007, pixel format flags 0x41, 32 bits, masks FF0000/FF00/FF/FF000000, caps 0x401008).

### 6.5 3D grass and the `.grs` cache `[RE][G]`
- `EffectGrass.fx`: grass blades are instanced on a **0.4 m grid** over `Grass3D`. Blade colour = sub-texture colour, blade size = sub-texture
  alpha: 255 long, stock mown areas about 64-95, **0 = no blades** (use it where the photo shows asphalt) `[G]`.
- `Airports/<A>.grs` is a cache the game writes when it is missing: blocks of `u32 count + count x (u16 ix, u16 iy, f32 altitude)`, cells of
  0.4 m on a north-up grid over the airport's bounding box, bucketed in 30 x 30 cells. 40 MB for Talfeld (600 x 1400 m). It is stale after any
  change of the G mesh or sub-texture: remove it (deleting is not permitted in this setup: move it to `Condor3/_to_delete/`).

### 6.6 O file `[DOC][G]`
- Ordinary objects (5.1). Everything in the O file is solid (collision); the G file never collides.
- **Windsocks**: up to three objects named `Windsack1`, `Windsack2`, `Windsack3`, each ONE equilateral triangle. Its centre is where the sock is
  attached, and **sock length = 100 x the triangle's circumradius** (stock 0.045 -> 4.5 m at 7.9 m height; our 0.10 -> a visibly 10 m sock).
  The pole is separate geometry. Stock proxy: untextured, colour 0.8, params 0.8/5/0.

### 6.7 Spawn point `[G]`
The glider starts on the centre line **200 m after the start of the runway**: local `(0, -length/2 + 200)` with the `.apt` length
(Talfeld 900 m -> y = -250, Hangfeld 600 m -> y = -100), standing on the terrain.

### 6.8 Official modelling rules worth keeping `[DOC]`
Metres; one material/texture per object; no pre-baked shading (objects are lit dynamically); airports have no underlying grass polygon other
than Grass/Grass3D; multiplayer parks 16 gliders in a 30 x 200 m line starting 170 m from the runway end, so flatten that too.

---------------------------------------------------------------------------------------------------------------------------------

## 7. Scenery objects

### 7.1 World objects: `<L>.obj` + `World/Objects/` `[RE][G]`
- `<L>.obj` = list of **152-byte records** (loader `[RE]` Condor.exe 0x6de460; record size 0x98):
```
f32 x (west)   f32 y (north)   f32 z (ignored: the game samples the terrain height)
f32 scale (uniform)            f32 rotation [radians]
ShortString[131] model file name ("Tower.c3d" or "Tower")
```
- Rotation is the **compass heading of the model's +Y axis** (0 = +Y north and +X west; pi/2 = +Y east) `[G]` with four test houses.
- The model is searched in `Landscapes/<L>/World/Objects/` first, then in the game's own `World/Objects/` (which has `Winch`, `Pylon`,
  `PylonDouble`, `WindsackA`, ...) `[G]`. Textures relative to that folder.
- Objects stand on the terrain at their origin; flatten the terrain under large ones `[DOC]`.

### 7.2 Autogen: `Autogen/oXXYY.c3d` + `<L>.oha` `[D][RE][G]`
- One C3D per tile holding all generated scenery of that tile (stock: OSM buildings and power lines from the toolkit's Blender exporter).
- Vertex frame: **x west / y north relative to the tile CENTRE, z = absolute altitude** (base heights of three stock tiles match the terrain
  only in this frame; our six houses stand where intended).
- Textures in `Autogen/Textures/`, referenced as `Textures\name.dds`.
- **`<L>.oha` is the index and integrity list** (`[RE]` 0x6defe8): CRLF lines keyed like 4.7; a tile's autogen file is only loaded when its line
  carries a value, and that value must be the **MD5 hex of the file** (`000302 d41d...`). A key without value = no autogen for the tile.
  A wrong MD5 or missing file clears an integrity flag (9.2).

---------------------------------------------------------------------------------------------------------------------------------

## 8. Thermals and the `.tm3` `[RE]`

Thermal object: loader 0x75c3dd (reads the file into rows at +0x8c), generator 0x75ad94, per-thermal update 0x75a3f9, sampler 0x75c11c.
- Sunrise and sunset are found by scanning the sun elevation over the day; the number of thermals is about
  `6e-5 * landscape width * height [m] * (sunset - sunrise) [h]`.
- For each candidate position the cell's three bytes are read:
  - **byte1 > 0.5** (height stddev above 50 m): mountain mode - the sun direction against the local terrain normal drives the thermal.
  - otherwise flat-land mode - probability and strength are proportional to **byte0** (the painted map) and the weather's flats activity.
    Guide: with flats activity "none" the map is ignored and only relief and sun exposure produce thermals `[DOC]`.
  - **byte2 is stored but never read** by the generator.
- While a thermal lives, byte1 is sampled again (bilinear); above 0.5 its strength is scaled by `1 - 3*(v - 0.5)`.
None of this was measured in flight; the file format itself is `[D]` (4.5).

---------------------------------------------------------------------------------------------------------------------------------

## 9. Game behaviour around landscapes

### 9.1 Flight plan `[G]`
`Documents/Condor3/Pilots/<pilot>/Flightplan.fpl` (pilot = registry `Software\Condor3` `LastPlayer`; NOT `Condor3/Pilots/`) is what "Free flight"
opens. INI text: `[Task] Landscape=<L>`, `Count`, `TPName<n>`, `TPPosX<n>` (metres west), `TPPosY<n>` (metres north), `TPPosZ<n>`, `TPAirport<n>`,
sector fields, `DisabledAirspaces=`; `[Weather]`, `[Plane]`, `[GameOptions]` (`StartType`, `StartHeight`, ...). Editing it selects landscape and
airport without touching the UI. The previous plan is saved beside it as `Flightplan.fpl.before_proofvalley`.

### 9.2 Integrity flags and the silent penalty `[RE]`
- Three byte flags in the landscape object: +0x38 (cleared when a `.tha` value differs by more than 1, 0x76a2e2), +0x40 (`.fha`, 0x76ad6e),
  +0x48 (autogen MD5 vs `.oha`, 0x6df337).
- Nothing is logged or displayed. But in every simulation frame with any flag cleared (0x6d64d7) the game calls 0x6f58f0, which writes -1 into
  one input axis of the player's aircraft controls (controls object +0x618; skipped in some states). A sibling routine 0x6f588c (logs "CD")
  does the same to two axes. So a landscape with wrong hashes loads and looks fine but the aircraft is sabotaged in flight.
- Not provoked in game; which axis it is is unknown. An early test with all hashes +5000 showed "no effect", but only on the ground.
  **Always write correct `.tha`, `.fha`, `.oha`.**
- `.aha` is different: it is checked at load and raises an error (4.9).

### 9.3 Misc `[RE]`
- Landscape object: `[0x792fd0]+0x44`; size in metres at +0x1b0/+0x1b4, 1/90 at +0x1bc, tile counts at +0x1a4/+0x1a6.
- `Landscapes\<L>\...` paths are built with the landscape name at `[0x792a68]+4`.
- Registry `Software\Condor3\Landscapes\` only concerns licensing ("Running demo version").

---------------------------------------------------------------------------------------------------------------------------------

## 10. Shaders (read these before disassembling render code)

`Condor3/Effects/fx/` holds HLSL sources (compiled `.fxc` beside them). The user's photorealism mod inserted helper functions and lines marked
`[MOD]`; constant buffers, techniques and the original formulas are intact.
| file | what | facts taken from it |
|---|---|---|
| `EffectDX11File.fx` | all C3D rendering: aircraft (`Render_Solid*`, `Render_Canopy`), `Render_GroundObject`, `Render_AirportSurface` | 5.1, 6.3; `cbPerObject`: MaterialColor, SpecularStrength, SpecularShininess, EnvironmentalStrength, WrapScale, ScaleX (dU/dX), ScaleY |
| `EffectLand.fx` | terrain | water = alpha < 0.8, detail x30, RealtimeShading, MirrorX/MirrorY at borders |
| `EffectGrass.fx` | 3D grass | ground colour and alpha (blade size) sampled from the sub-texture via OffsetX + x*ScaleX |
| `EffectAutoObjects.fx` | forest trees (billboards from the forest map) | alpha clip 0.1 |
| `EffectCloud*.fx`, `EffectSky.fx`, ... | sky | not studied |

---------------------------------------------------------------------------------------------------------------------------------

## 11. Tools in this repo

| file | purpose | status |
|---|---|---|
| `tools/make_demo.py` | builds the complete ProofValley landscape; the executable form of this document. Key functions: `build_master` (heights, airport planes), `ground`, `forest_class`, `color` (ground colour, water mask, thermal class for any world point), `dds_header`/`bc1_blocks`/`alpha_blocks`/`write_dds`, `tile_texture`, `Airport` class, `plane_mesh`, `build_airport`, `build_scenery` (world objects, `.obj`, autogen, `.oha`, `.air`/`.aha`), `main` | current |
| `tools/tiles2condor.py`, `tools/dds.py` | landscape from a 3D Tiles export (photogrammetry): rasteriser, ground model, textures, airports from JSON, 3D objects as autogen. Manual: `docs/TILES2CONDOR.md` | current, verified with Paris Orly |
| `tools/c3d.py` | C3D parse / write / build | current |
| `tools/obj2c3d.py` | OBJ(+MTL) -> C3D like ObjectEditor (`--wings`, `--texdir`) | current, verified with one object |
| `tools/apt.py` | `.apt` unpack / pack_one | current |
| `tools/terrain_hash.py` | `condor_hash(bytes, idx)` for tha/fha/aha | current |
| `tools/make_tm3.py` | `.tm3` from heights + thermal values | current |
| `tools/utm.py` | WGS84 UTM forward/inverse (northern form; south = subtract 10 000 000 from N, see `make_demo.LL`) | current |
| `tools/views.py` | camera list in airport-local coordinates -> `postfx/views.txt` | current |
| `scripts/flytest.sh` | start game, enter flight, pause, visit absolute positions, screenshot | current |
| `tools/crop_landscape.py` | cut N x M tiles out of a stock landscape (first end-to-end proof, `Crop8`) | works, historical |
| `tools/make_airport.py` | single airport into an existing landscape, photo cropped from tile textures (heading-up photo, lifts mesh 0.25 m) | superseded by `make_demo.build_airport`; still imported for `box`; also has a DXT1/DXT3 decoder (`decode_dxt`) |
| `tools/make_landscape.py`, `tools/make_test_airport.py` | first synthetic test bed (`Synth8`) | obsolete; `make_landscape.py` has an undefined variable in its `.bmp` part |
| `tools/re/dasm.py` | `dasm.py exe VA [nbytes|f]`: objdump with Delphi string and float-constant annotation | RE helper |
| `tools/re/strref.py` | find strings and the code that references them (case-insensitive: use specific text) | RE helper |
| `tools/re/callers.py` | direct callers of an address | RE helper |
| `tools/re/dfm.py` | dump Delphi forms (captions, event handlers) from exe resources | RE helper |
| `tools/re/uvfit.py` | affine UV fit of every G object of all stock airports | analysis |

### 11.1 Reverse-engineering method that works here
- The exes are 32-bit Delphi PE files, image base 0x400000; `.text` file offset = VA - 0x401000 + 0x400 (Condor.exe and the editors alike).
- String literals sit right after the function that uses them; code loads them with `mov edx, imm32`, which `dasm.py` annotates.
- Event handlers: `dfm.py` gives `OnClick = 'Open1Click'`; the address is the u32 right before the ShortString method name in the class method
  table (preceded by a u16 entry size = len + 7).
- The editors contain the game's own loaders and writers and are much smaller: look there first for file formats.
- The `rea` MCP (Ghidra) can open the binaries, but pseudo-code requests timed out and its error dumps flood the context; do not rely on it.
- Do not name a helper `dis.py` (shadows Python's stdlib module and breaks numpy).

### 11.2 Running and observing the game
- The user's d3dx hook (other project) provides remote control through `<game>/postfx/cmd.txt` (consumed within about 0.2 s): `kkey <KEY>`,
  `mclick x y`, `move dW dN dZ` (RELATIVE; prints the new absolute position as `move: ... pos now X Y Z` in `postfx/postfx.log`), `state`,
  `peek/poke/scan/watch`, `shot`, `wait ms`. `move 0 0 0` just prints the position.
- `scripts/flytest.sh` wraps the whole cycle (about 75 s to the first screenshot): kill, start under CrossOver, `mclick 137 212` (Free flight),
  `mclick 991 668` (Start flight), wait for `ENTERED SIMULATION` in `Logs/Logfile.txt`, `kkey P`, then per view: read position, move, toggle
  pause twice to redraw (a paused game does not redraw after `move`), `screencapture`.
- The camera is the cockpit; it always looks along the glider's heading (the runway heading of the start airport). Plan views accordingly.
  The F2 external camera looks BACK at the glider and has misled earlier analysis.
- The glider drifts and pitches whenever the sim runs: ground-level positions stay level; after several airborne moves the nose points up,
  so take airborne views first. On the sloped airport it rolls downhill between moves (hence absolute positioning).
- To look at screenshots: stage `Condor3/postfx/shots/*.png` and read them (contact sheets of several views save context).

---------------------------------------------------------------------------------------------------------------------------------

## 12. Address index

**Condor.exe**
| address | what |
|---|---|
| 0x4b46c8 | tile/airspace hash function (callers 0x6af396 `.aha`, 0x76a2e2 `.tha`, 0x76ad6e `.fha`) |
| 0x666b3c | OpenAir parser |
| 0x6956bd | list of landscape files (.air .trn .bmp .tm3 .obj .apt .tha .fha .aha .ini) |
| 0x6a849c | landscape discovery for the flight planner |
| 0x6aed62-0x6aeecd | `.trn` header read and calibration check |
| 0x6af0b0-0x6af3bd | `.bmp` size check, `.air` load, `.aha` check |
| 0x6d51e5 / 0x6d52ef | "Initializing Thermals" / "Caching autogen" in the load sequence |
| 0x6d64d7 | per-frame integrity flag test -> 0x6f58f0 (penalty), 0x6f588c ("CD") |
| 0x6de02c-0x6de798 | world objects: paths, `.obj` reader 0x6de460, model cache 0x6de2c4 |
| 0x6defe8-0x6df34f | autogen tile loading and MD5 check against `.oha` |
| 0x6dfd48-0x6e0500 | airport grass: `.grs` cache, EffectGrass, GrassObject.dds |
| 0x6e0c00-0x6e1600 | airport load/update: distances, G object names, detail texture overrides, O file, WindSack |
| 0x6e18d8 | least-squares UV fit per surface object (detail tiling) |
| 0x6e1bc1-0x6e1f2a | airports init: `.apt`, default detail textures `world/textures/*.dds`, AcroBox, WindSackA |
| 0x75a3f9 / 0x75ad94 / 0x75c11c / 0x75c3dd | thermal update / generator / tm3 sampler / tm3 loader |
| 0x75eea8.. | strings `.tha .fha .oha` (hash table loaders nearby) |
| 0x76a073 | `.tr3f` / `.tr3` loading |
| 0x76c9d0 / 0x76d094 | tile texture loader / residency |
| 0x7749e5.. | technique names (`Render_AirportSurface`, `Render_GroundObject`, ...) |

**LandscapeEditor.exe**: `.for` export 0x5ed4c0, thermal map export 0x5ecdf0, `.aha` 0x49ec30, airport dialog 0x5e3340, `.apt` loader 0x5ee2a0.
Useful menu items `[DOC]`: "Export TR3 to 22.5m OBJ", "Export modified OBJ to TR3f", Export terrain/forest hash, flatten tool, `-hash <L>`.

**ObjectEditor.exe**: Blender/Wings open 0x52d09c / 0x52d070 -> 0x52ced8 -> OBJ line parser 0x51eabc, MTL parser 0x520958, OBJ export
0x521c31, Save C3D 0x5218bc (`SavetoC3D1Click` 0x52e688), C3Dx 0x52e738, grid fill 0x52f86c, sort by render order 0x52e288.

---------------------------------------------------------------------------------------------------------------------------------

## 13. What is on disk and what each experiment proved

- **Slovenia3**: untouched stock reference.
- **Crop8**: 8 x 8 tiles cut from Slovenia3 with renamed tiles and regenerated hashes -> proved georeference, tile naming, hash index.
- **Synth8**: first synthetic landscape with debug colours -> proved raster orientations, `.tm3` requirement, first custom airport.
- **ProofValley** (current demo): 6 x 5 tiles, meandering valley at about 500 m, mountains to 2770 m, a lake, fields, two forest classes.
  - `Talfeld` (world 20000, 13924; heading 67.75; 900 x 30 asphalt; tile 3,2): horizontal plane; Grass3D + Asphalt (muck) + AsphaltPaint +
    AsphaltPaintYellow + GrassPaint + Gravel; hangar with own texture; two windsocks (4.5 m and 10 m); material test row of spheres at local
    x = -80..-164, y = -150; world objects in a row at local y = -60, x = -200..-400 (three rotations, scale 2, the obj2c3d tower, the game's
    Winch); six autogen houses at local x = 230; airspace `TALFELD CTR`.
  - `Hangfeld` (world 24000, 12508; heading 74.76; 600 x 30 grass; tile 4,2): plane tilted 2 % / 1 %; Grass3D, GrassPaint, small Asphalt
    apron, shed, windsock.
  - Screenshots: `docs/img/proofvalley_*.jpg` (first version), `docs/img/s5_*.jpg` (current).
- In-game experiments and their outcome: `.apt` elevation +30 m -> no change (6.2); coplanar mesh -> fine; nothing copied from stock -> loads;
  material spheres (5.1); bad-UV apron -> flat grey (6.3); sub-texture alpha 0 on the apron -> no grass (6.5); windsock radius (6.6);
  spawn at two runway lengths (6.7); `.obj` rotations/scale/fallback folder (7.1); autogen houses (7.2); airspace circle (4.9);
  zone 31 and hemisphere S (4.1).

---------------------------------------------------------------------------------------------------------------------------------

## 14. Mistakes already made (do not repeat)

- "Not visible in a screenshot" was taken for "not rendered" several times: wrong winding (culled), mesh below the terrain, camera facing away,
  objects hidden under the instrument panel from 45 m up, glider drifted from the assumed position.
- The airport frame was first guessed (+X as runway axis) instead of fitted to stock data. Use stock files as ground truth.
- The first doc claimed `.oha` and `.aha` are not read by the game. Both are (7.2, 4.9).
- "Wrong hashes have no effect" was concluded from a ground test (9.2).
- "Stock Asphalt is only a dirt overlay" was a misreading: Asphalt is the paved surface; its asphalt look comes from the detail texture and
  its object texture is the (almost white) muck (6.3).
- `make_landscape.py` takes the landscape directory itself, not the `Landscapes` folder.
- A stale `.grs`, or a game not restarted, shows old airport data.
- Broad string searches and `rea` errors flood the context; search for specific text.

---------------------------------------------------------------------------------------------------------------------------------

## 15. Open questions

1. Which control axis the integrity penalty drives (9.2), and whether it can be seen in flight.
2. The red stock Asphalt of session 3 (6.3).
3. The rotation angle used for the UV fit of grass-type surfaces (airport object field +0x5c; most likely the runway direction, so that the
   fit happens in north-up axes - the north-up sub-texture mapping works).
4. `[RE]`-only items never provoked in game: border fade of `.trn`/tiles, thermal behaviour (8), texture residency distances.
5. Behaviour of the plain `Grass` object (Condor 2 style, without 3D grass) - not used in our demo.
6. Not part of landscapes and untouched: aircraft C3D features (LOD use, morph targets, canopy), C3Dx, GAU gauges, clouds.
