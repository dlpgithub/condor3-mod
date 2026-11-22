# Condor 3 post-processing

A real render-pipeline mod: `d3dx.dll` (our proxy) hooks Condor's frame **after the scene and before the HUD**,
takes the finished multisampled colour buffer and the depth buffer, and runs these passes
(all tweakable at the top of `postfx.hlsl`):

* SSAO from the depth buffer (grounds trees, buildings, creases)
* bloom (3-level), soft highlight shoulder, S-curve contrast, saturation, vignette
* sharpening limited to the local range, depth based blue haze, dither

## Use
    scripts/build_docker.sh Debug      # or Release (Debug writes runtime logs for the monitor tool)
    scripts/postfx.sh install          # backs up your current d3dx.dll first
    scripts/postfx.sh uninstall        # back to what you had

In the game: **F9** effect on/off (A/B comparison), **F8** reload `postfx.hlsl`, **F10** write `postfx/capture.bin`.
`<Condor>/postfx/postfx.hlsl` is hot-reloaded when saved; compile errors keep the old look and go to `postfx/postfx.log`.

## Offline tuning (no game, no screen takeover)
    scripts/pfxtest.sh postfx/test/capture.bin out.bmp 4     # 4 = MSAA samples (1 for off)
Runs the exact same code headless under CrossOver on a captured frame. `DEBUG_VIEW` in the HLSL shows AO / depth / bloom.

## How it hooks (found with REA)
See the header of `src/postfx_hook.c`.
