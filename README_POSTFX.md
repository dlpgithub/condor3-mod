# Condor 3 post-processing mod

In-engine post-process pass (SSAO, bloom, aerial haze, sky grading, tone/contrast, sharpen, vignette) hooked into the
D3D11 frame just before the HUD. No texture changes. Depth is captured before the engine's cockpit depth clear.

## Install
    scripts/build_docker.sh        # builds build/d3dx.dll
    scripts/postfx.sh install      # backs up d3dx.dll, installs mod + postfx/postfx.hlsl
    scripts/postfx.sh uninstall

In game: F9 effect on/off, F8 reload shader, F10 capture frame. Tweak values at the top of `postfx/postfx.hlsl`
(`scripts/postfx.sh sync` copies it; F8 hot-reloads). Overhead measured at ~1 fps (69 vs 68) on RX 5700 XT / CrossOver.

## Remote control (for testing)
The mod polls `<game>/postfx/cmd.txt` every 200 ms; results go to `postfx/postfx.log`. `scripts/autotest.sh`:
`launch`, `startflight`, `cmd "line"...`, `shot out.png`. Commands: `state`, `post on|off`, `shot NAME`, `kkey NAME`,
`mclick X Y`, `peek/poke EXPR`, `scan`, `watch/unwatch`, and `move dx dy dz` (offsets the glider's world position,
doubles at glider object +0x6d8/0x6e0/0x6e8). Altitude screenshots: `kkey p`, `move 0 0 N`, `wait 400`, `kkey p`,
then wait ~2.5 s before `shot` (the frame needs time to refresh).

### Freeze and attitude (look around without flying and without pausing)
- `freeze on|off|toggle` (in game: **Ctrl+Shift+F**) stops the glider's physics while the sim keeps running: the glider stays exactly
  where it is, `move dx dy dz` places it, `freeze off` (or the hotkey again) lets physics take over from the current state.
  Implemented by skipping the physics step FUN_00706988 (hook in `src/postfx_hook.c`) and only deriving what the game's replay mode
  derives (inverse matrix, floating-origin position).
- `att` prints pitch, roll, yaw (degrees), position and speed; `att PITCH ROLL YAW` sets the attitude: yaw = compass heading of the nose,
  pitch = nose up, roll = right wing down. The velocity turns with the glider, so after `freeze off` it flies on relative to its new attitude.
  Works frozen, unfrozen and paused (applied on the next rendered frame).
- `dump [name]` (in game: **Ctrl+Shift+D**) appends `name x y z pitch roll yaw` to `<game>/postfx/positions.txt` (default name
  `pos_HHMMSS`). The file is a views file: `scripts/viewshots.sh <game>/postfx/positions.txt` flies back to every saved spot, sets the
  attitude and takes a screenshot per line.
- `goto NAME` jumps back to the last spot saved under that name (position + attitude, one step);
  `goto X Y Z [PITCH ROLL YAW]` goes to absolute numbers. Best used frozen: a paused game may not redraw.
- Example (agent workflow): `freeze on`, `move 0 0 300`, `att -10 0 150`, screenshot, `move 500 0 0`, screenshot, ..., `freeze off`.

