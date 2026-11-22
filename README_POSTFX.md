# Condor 3 post-processing mod

In-engine post-process pass (SSAO, bloom, aerial haze, sky grading, tone/contrast, sharpen, vignette) hooked into the
D3D11 frame just before the HUD. No texture changes. Depth is captured before the engine's cockpit depth clear.

## Install
    scripts/build_docker.sh        # builds build/d3dx.dll
    scripts/postfx.sh install      # backs up d3dx.dll, installs mod + postfx/postfx.hlsl
    scripts/postfx.sh uninstall

## Release package
    scripts/build_docker.sh && python3 shaders/build_shaders.py build && scripts/build_rust.sh v2   # (Windows: scripts\build_rust.bat v2)
    scripts/package_release.sh      # -> dist/condor3-mod-<date>-<commit>-<platform>.zip (macOS / Linux / WSL)
The zip holds the mod, the helper, shaders, install scripts, the landscape compiler and `README.md` (usage of every file,
for players and landscape builders).

In game: F9 effect on/off, F8 reload shader, F10 capture frame. Tweak values at the top of `postfx/postfx.hlsl`
(`scripts/postfx.sh sync` copies it; F8 hot-reloads). Overhead measured at ~1 fps (69 vs 68) on RX 5700 XT / CrossOver.

## Remote control

The mod has a small remote-control interface for automated tests, screenshots of exact spots and debugging. It works the same on
Windows and under CrossOver, needs no network and no macOS accessibility permission.

- **How it works:** a thread inside the game checks `<Condor>/postfx/cmd.txt` every 200 ms. When the file exists it reads up to
  64 lines, **deletes the file** and runs the lines in order, one command per line. Results and errors are appended to
  `<Condor>/postfx/postfx.log` (every command is echoed there as `cmd: ...` first).
- **Sending commands:** write the file and wait until it is gone, e.g. `printf 'freeze on\natt\n' > ~/Condor3/postfx/cmd.txt`, or use
  `scripts/autotest.sh cmd "freeze on" "att"` (writes the lines, waits until the game consumed them, then 1 s more).
- **Line format:** `command [arg1] [arg2 ...]`, case-insensitive command names, numbers as plain decimals. Unknown commands are
  echoed and ignored. Lines are limited to 255 characters.
- **Hotkeys** (no file needed): **Ctrl+Shift+F** = `freeze toggle`, **Ctrl+Shift+D** = `dump`. **F9** / **F8** / **F10** are the
  effect toggle, shader reload and frame capture (see above).
- **Coordinates:** world positions are Condor's landscape metres: **x counts west, y north, z = altitude** (above sea level).
  Angles are degrees.

### Commands

**Status and diagnostics**

| command | what it does |
|---|---|
| `state` | logs `state: status='<game status text>' flying=0/1 frames=N postfx=on/off core=ready/none`, then the game's windows. `flying=1` means frames are being rendered (a flight is running and not paused); `status` is the game's current loading/status message |
| `windows` | logs every window of the game: handle, title, client size, screen position of the client area |
| `ag` | autogen streaming statistics: chunks per LOD, textures, queue, memory pools, glider/camera position, resident triangles and draw calls, frame-time histogram |
| `atmo` | the atmosphere values the postfx reads from the terrain effect (view matrix row, camera position, fog, sun direction/colour, ambient) |
| `vm` | summary of the game's 32-bit address space: committed image / mapped / private memory, reserved, free, largest free block, allocation size histogram |
| `vm auto 1` / `vm auto 0` | log the `vm` summary every ~10 s / stop it |
| `vmbig` | every committed private allocation of 4 MB or more: base, size, first bytes (to find out who owns big memory) |

**Post-processing**

| command | what it does |
|---|---|
| `post on` / `post off` | picture effects on / off, like F9. `off` also turns off the extra yaw-string draws and the depth copy, so the game renders exactly as without the effect |
| `shot NAME` | writes the next frame's colour (before the effect) + depth + projection to `<Condor>/postfx/shots/NAME.bin` for offline tuning with `scripts/pfxtest.sh` (the depth is the one the effect uses: cockpit depth where the cockpit is, world depth elsewhere). Needs the effect on |
| `yawlayer on` / `yawlayer off` | debugging: the yaw string's translucent layer (two extra draws per frame that let the effect grade the string separately from the world behind it) |

Picture settings are not commands: they live in `<Condor>/postfx/tune.txt` (one `name value` per line, checked every few frames;
`scripts/tune.sh name value ...` edits it, `scripts/tune.sh reset` clears it). The names and defaults are the `//@tune` lines at the top
of `postfx.hlsl`.

**Glider: freeze, position, attitude** (the sim keeps running, nothing is paused)

| command | what it does |
|---|---|
| `freeze on` / `freeze off` / `freeze toggle` | stops / restarts the glider's physics. Frozen, the glider stays exactly where it is while the sim, clouds and streaming keep running; `move`, `att` and `goto` place it. After `freeze off` physics continues from the current state. (Skips the physics step FUN_00706988 and only derives what replay mode derives: inverse matrix, floating-origin position) |
| `att` | logs pitch, roll, yaw, position and speed of the glider |
| `att PITCH ROLL YAW` | sets the attitude: yaw = compass heading of the nose, pitch = nose up, roll = right wing down. The velocity turns with the glider. Applied on the next rendered frame (works frozen, unfrozen and paused) |
| `move DX DY DZ` | moves the glider by DX m west, DY m north, DZ m up; logs the new position (`move 0 0 0` just reports where it is) |
| `goto X Y Z [PITCH ROLL YAW]` | absolute position (and attitude) in one step |
| `goto NAME` | position + attitude of the **last** line called NAME in `<Condor>/postfx/positions.txt` |
| `dump [NAME]` | appends `NAME x y z pitch roll yaw` of the glider plus the rendered camera's `x y z pitch roll yaw` to `positions.txt` (default name `pos_HHMMSS`). Same as Ctrl+Shift+D |

`positions.txt` is a plain text file you can edit; `goto NAME` uses the first six numbers after the name. `tools/c3view.py` uses the
camera columns (docs/C3VIEW.md), and `scripts/viewshots.sh <Condor>/postfx/positions.txt` visits every line and takes a screenshot.
`goto` and `att` are best used frozen: a paused game may not redraw.

**Free camera** (detached from the glider; the engine camera is overwritten at the start of the scene pass)

| command | what it does |
|---|---|
| `cam here` | takes over the current camera position and direction, then keeps it fixed |
| `cam X Y Z [YAW PITCH]` | camera at an absolute world position; yaw 0 = looking north, clockwise from above; pitch up positive |
| `camrel FWD RIGHT UP [DYAW DPITCH]` | moves the free camera relative to its own direction (metres) and turns it (degrees) |
| `cam off` | back to the game's own camera |

**Input to the game** (client coordinates = pixels inside the game window, top left 0,0; see `windows` for its size)

| command | what it does |
|---|---|
| `mclick X Y` | real mouse click (cursor moved there, button down/up, like a physical mouse). Works in the game's menus |
| `mdrag DX DY` | holds the left button in the middle of the window and moves by DX, DY (mouse look) |
| `click X Y` | posts a click message to the deepest child window at that point (classic Windows controls only) |
| `kkey KEY [N]` | real key press (like a physical keyboard), N times. Use this for flight controls and game hotkeys |
| `kdown KEY` / `kup KEY` | hold / release a key (real input) |
| `key KEY [N]` | key press posted as a window message to the game window (dialogs and text fields; the 3D view usually ignores it) |
| `keydown KEY` / `keyup KEY` | hold / release as window messages |

KEY is a single letter or digit (`p`, `7`), `f1`..`f12`, one of `enter esc space tab up down left right pgup pgdn home end pause back
plus minus`, or a virtual-key code in hex (`0x6B`). Real input (`mclick`, `mdrag`, `kkey`) goes to whatever window has the focus: keep
the game window in front. Note: the in-flight game menu (Esc) does not react to the injected clicks under CrossOver; leave a flight with
the real mouse.

**Memory tools** (reverse engineering; addresses in hex)

| command | what it does |
|---|---|
| `peek EXPR [LEN]` | logs LEN bytes (default 64, 4..4096) at EXPR as 32-bit words, four per row: float value and hex |
| `poke EXPR VALUE` | writes VALUE as a 32-bit float at EXPR |
| `watch EXPR [LEN]` | logs every write to LEN bytes at EXPR (guard page) with the writing code address (Condor.exe address as in IDA/Ghidra) |
| `unwatch` | stops watching; logs how many writes and writers were seen |
| `scan X Y Z [TOL]` | searches writable memory for three consecutive floats within TOL (default 2) of X, Y, Z (find objects by position) |
| `scand X Y Z [TOL]` | the same for three consecutive doubles |

EXPR is `[*]ADDR[+OFF]`: `ADDR` hex; prefix `i:` = an address in Condor.exe as shown in IDA/Ghidra (converted to the running image);
a leading `*` first reads the 32-bit pointer stored at ADDR, then OFF is added. Example: `peek *i:7d96f8+148 64` = the camera's
world matrix (rows right, up, forward, position).

**Scripting**

| command | what it does |
|---|---|
| `wait MS` | pauses the rest of the file for MS milliseconds |

### Helper scripts (macOS / CrossOver)
| script | what it does |
|---|---|
| `scripts/autotest.sh launch` | starts Condor (kills a running one) |
| `scripts/autotest.sh startflight` | launch, open Free Flight, press Start, wait until frames are rendered (uses the pilot's current `Flightplan.fpl`) |
| `scripts/autotest.sh cmd "line" ...` | sends command lines and waits until they were consumed |
| `scripts/autotest.sh shot out.png` | screenshot of the whole screen, scaled to 1200 px |
| `scripts/flytest.sh [views]` | starts a flight, then visits the positions of a views file and takes a screenshot per line |
| `scripts/viewshots.sh views [wait_s]` | the same for a flight that is already running |
| `scripts/tune.sh name value ...` | live picture settings (`tune.txt`) |

### Examples
Look at a spot from above without flying, then continue:

    freeze on
    move 0 0 300
    att -10 0 150
    wait 2500
    dump overview
    freeze off

Back to a saved spot later (`goto` restores position and attitude in one step):

    freeze on
    goto overview

A/B comparison of the effect at the same frozen frame (screenshots taken between the two files):

    post off
    (screenshot)
    post on
    (screenshot)

Notes: after moving or turning, give the frame ~2.5 s before a screenshot (streamed autogen and the frame need time to follow).
Results are only in `postfx.log`; when scripting, read the log after the command file has disappeared.
