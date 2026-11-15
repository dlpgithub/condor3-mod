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
