# Autogen streaming (beats the 32-bit memory limit)

`src/agstream.c` (option `ENABLE_AGSTREAM`, on by default) switches Condor's own autogen loading off and streams the autogen of the
current landscape from the 64-bit helper `c3helper.exe` (separate repo `../condor3-helper`, see its README and docs/).

- The game normally holds every autogen tile of a 4 x 4 tile window as one model, mesh kept twice (CPU + GPU): 2.3 GB in flight for
  OrlyDemo, crash for a 3x denser copy. With streaming: 0.9 GB / 1.3 GB, same look.
- Chunks of 1.44 km, LOD by distance from the glider (full detail to 2 km, LOD 1 to 5 km, LOD 2 to 12 km), textures and GPU buffers
  pooled, uploads through our own staging ring. Work on game objects happens once per frame from the HUD hook (`AgStream_FrameTick`).
- Without `c3helper.exe` next to Condor.exe (or with `enable 0` in `<game>/postfx/agstream.txt`) the game behaves as before.
- Install: build condor3-helper (`../condor3-helper/scripts/build.sh` puts `c3helper.exe` into `build/`), then `scripts/postfx.sh install`
  / `scripts\postfx.bat install "C:\Condor3"` copies it next to Condor.exe.
- Remote commands (postfx/cmd.txt): `ag` streaming state, `vm` address-space use, `vmbig` big allocations with their first bytes.
- Logs: `<game>/postfx/agstream.log`, `<game>/postfx/c3helper.log`.
- `scripts/viewshots.sh <views> [wait]`: visit absolute positions in a running (paused) flight and take screenshots.
