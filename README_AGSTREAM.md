# Autogen streaming (beats the 32-bit memory limit)

`src/agstream.c` (option `ENABLE_AGSTREAM`, on by default) switches Condor's own autogen loading off and streams the autogen of the
current landscape from the 64-bit helper `c3helper.exe` (separate repo `../condor3-helper`, see its README and docs/).

- The game normally holds every autogen tile of a 4 x 4 tile window as one model, mesh kept twice (CPU + GPU): 2.3 GB in flight for
  OrlyDemo, crash for a 3x denser copy. With streaming: 0.9 GB / 1.3 GB, same look.
- Chunks of 1.44 km (dense ones split down to 90 m), 4 levels of detail per chunk (the landscape's detail sets from `Autogen/lods.txt`,
  else vertex clustering), chosen by screen-space error from the camera (`tol` 2 px, `range` 15 km in agstream.txt). Textures and GPU
  buffers pooled, uploads through our own staging resources.
- Stutters: the CPU side of a chunk model (game objects, arrays, bounds) is built on the stream thread like the game's own cache thread
  does, the collision trees only for LOD 0 chunks within 500 m of the glider (also on the stream thread), and models are destroyed
  there too. The main thread (`AgStream_FrameTick`, HUD hook) only uploads textures and 4 MB buffer pieces and hooks models into the
  game's list, within a time budget per frame (`budget`, 3 ms). Slow frames are logged with their cause in agstream.log.
- Without `c3helper.exe` next to Condor.exe (or with `enable 0` in `<game>/postfx/agstream.txt`) the game behaves as before.
- Install: build condor3-helper (`../condor3-helper/scripts/build.sh` puts `c3helper.exe` into `build/`), then `scripts/postfx.sh install`
  / `scripts\postfx.bat install "C:\Condor3"` copies it next to Condor.exe.
- Remote commands (postfx/cmd.txt): `ag` streaming state + frame statistics, `vm` address-space use (`vm auto on|off`: every 10 s,
  off by default since walking the address space stalls), `vmbig` big allocations with their first bytes.
- Logs: `<game>/postfx/agstream.log`, `<game>/postfx/c3helper.log`.
- `scripts/viewshots.sh <views> [wait]`: visit absolute positions in a running (paused) flight and take screenshots.
