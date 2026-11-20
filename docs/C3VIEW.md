# c3view: 3D preview of a compiled Condor landscape

`tools/c3view.py` renders a landscape **from the files the game reads** (not from the GLB sources), so it shows what a converter
really produced: terrain from `HeightMaps/22.5m/*.tr3f` (else `*.tr3`) with the game's triangulation and the `Textures/t*.dds` tile
textures, autogen from `Autogen/o*.c3d` (tiles listed in the `.oha`), airports from `.apt` + `Airports/<A>G.c3d` / `<A>O.c3d`, world
objects from `.obj`. DDS textures are uploaded as DXT, like in the game. Formats: `docs/TERRAIN_FORMAT.md`.

    .venv/bin/pip install glfw PyOpenGL          # once (numpy and Pillow are already there)
    .venv/bin/python tools/c3view.py /Users/lennard/Condor3/Landscapes/Eiffel [--size 1920x942] [--ctl DIR] [--hidden]

Interactive: left mouse drag = look, W/S/A/D = move, E/Q = up/down, Shift = x5, mouse wheel = speed, 1-4 = layers (terrain, autogen,
airports, objects), G = wireframe, F = fog, P = screenshot, R = start view (200 m after the runway start of the first airport), H = help.
The window title shows position, height above ground, yaw/pitch, speed and fps.

## Remote control (chat agents)
Write command lines into `<game>/postfx/viewer/cmd.txt` (or `--ctl DIR`); results are appended to `log.txt`, screenshots go to
`shots/<name>.png`. World frame and angles exactly as in the game and the mod (`x` west, `y` north, `z` altitude; yaw = compass heading,
pitch up, roll right wing down). The full command list is in the header of `tools/c3view.py`; the main ones:

| command | effect |
|---|---|
| `cam X Y Z [YAW [PITCH [ROLL]]]` / `look YAW [PITCH]` | absolute camera / direction |
| `move DX DY DZ`, `fly FWD [RIGHT [UP]]`, `turn DYAW [DPITCH]`, `agl H` | relative moves; `agl` = height above the terrain |
| `goto NAME` | a spot saved in the game with the mod's `dump` (`<game>/postfx/positions.txt`); uses the game's camera columns |
| `shot [NAME]`, `state`, `layer terrain off`, `wire on`, `fog off`, `fov 60.6`, `size W H`, `sun AZ EL`, `wait MS`, `quit` | |

`--hidden` runs without a visible window (screenshots still work), for agents that should not disturb the screen.

## Same view as the game
The mod's `dump` writes the glider (x y z pitch roll yaw) **and the camera the game renders with** (6 more columns: eye position and its
pitch/roll/yaw from the camera manager matrix, see `src/postfx_hook.c`). The cockpit eye sits 1.7 m ahead of the glider reference point
and looks 12.6 deg below the glider's nose (default head position). `goto NAME` in the viewer uses the camera columns, the vertical field
of view is the game's 60.6 deg, so with the same window size the images line up (verified at Eiffel: three spots, game vs. viewer).
Workflow: fly in Condor, `freeze on`, `goto`/`att`, `dump spot1`; in the viewer `goto spot1`, `shot spot1`.

Not modelled: the game's lighting, shadows, haze, clouds, detail textures, 3D grass, forest trees and water animation (water texels are
only tinted). Verified on the Eiffel landscape (`examples/eiffel.json`, export `earth-3dtiles-92695058`).
