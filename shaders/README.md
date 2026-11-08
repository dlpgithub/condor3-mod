# Condor 3 shader mod

Patches Condor 3's HLSL effects (Land, Sky, AutoObjects, Grass, Cloud, Middle/High clouds, DX11File) for a nicer look.
No game files are overwritten: the game loads plain-text `Effects/fx/<Name>.fx` in preference to its scrambled `.fxc`.

## Use
    python3 shaders/build_shaders.py build      # patch + validate with the game's own compiler (CrossOver)
    python3 shaders/build_shaders.py install    # copy into <game>/Effects/fx/
    python3 shaders/build_shaders.py uninstall  # delete them again -> vanilla look
    python3 shaders/build_shaders.py dump       # decode the game's original .fxc to text

Tweak strengths/toggles in `options.json`, then rebuild.

## What it changes
- Anisotropic filtering 16x; cloud sampler point -> linear
- Per-pixel sky/fog/haze (removes vertex-interpolated banding), plus dither
- Mild saturation + S-curve grade, applied consistently to everything that blends with sky/fog
- Low-frequency detail to break up terrain tiling; sun-tinted water glint
- Fresnel sky reflection on lakes (`water_reflect`) and on glider gel-coat and canopy (`aircraft_reflect`)
- Soft (16-tap) terrain self-shadow and grass shadows (`soft_shadow`): no hard dark triangles on slopes
- Trees, grass and terrain share one linear-light model (sun + hemisphere ambient, neutral highlight shoulder)
- Clouds: warm sun-lit tops, cool shaded undersides
- postfx: screen-space contact shadows from the depth buffer (`contact_shadow`, `contact_dist`)

## How it works
`patches.py` does exact-match replacements on the decoded originals and fails loudly if a shader differs.
`tools/fxtest.exe` (32-bit) compiles via the game's `d3dx11_43.dll` + `D3DCompiler_43.dll`; these must sit next to it,
otherwise Wine's incomplete vkd3d compiler is used and the original Land shader fails too.
Originals are `.fxc` scrambled with Delphi `RandSeed := Length; byte -= Random(20)`.
