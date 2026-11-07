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
