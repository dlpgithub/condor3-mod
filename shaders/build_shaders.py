#!/usr/bin/env python3
"""
Condor 3 shader mod builder.

Condor 3 compiles its shaders from HLSL text at startup. It looks for
    <game>/Effects/fx/<Name>.fx          (plain text, preferred)
and falls back to
    <game>/Effects/<Name>.fxc            (the same text, scrambled)
This tool reads the game's own shaders, applies the patches from patches.py,
checks that every result still compiles (using the game's own fxc.exe through
CrossOver), and installs the patched files into <game>/Effects/fx/.
The original .fxc files are never modified, so uninstalling is just deleting
the Effects/fx folder.

    python3 build_shaders.py build      # patch + validate into ./build/Effects/fx
    python3 build_shaders.py install    # build, then copy into the game
    python3 build_shaders.py uninstall  # remove the installed files
    python3 build_shaders.py dump DIR   # decode ALL original shaders to DIR (for reading)

Options live in options.json. Needs only Python 3.8+.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import patches  # noqa: E402

WINE = "/Applications/CrossOver.app/Contents/SharedSupport/CrossOver/bin/wine"
BOTTLE = "Condor3"


# ---------------------------------------------------------------- decoding
def decode_fxc(data):
    """Delphi: RandSeed := Length(s); s[i] := s[i] - Random(20)"""
    seed = len(data) & 0xFFFFFFFF
    out = bytearray(len(data))
    for i, b in enumerate(data):
        seed = (seed * 134775813 + 1) & 0xFFFFFFFF
        out[i] = (b - ((seed * 20) >> 32)) & 0xFF
    return bytes(out)


def load_originals(game):
    eff = os.path.join(game, "Effects")
    res = {}
    for f in sorted(os.listdir(eff)):
        if f.endswith(".fxc"):
            with open(os.path.join(eff, f), "rb") as fh:
                res[f[:-4]] = decode_fxc(fh.read()).decode("latin-1")
    if not res:
        sys.exit("No .fxc files found in %s - is game_dir right?" % eff)
    return res


# ---------------------------------------------------------------- options
def load_options(path, game_override):
    with open(path) as fh:
        o = json.load(fh)
    o["game_dir"] = os.path.expanduser(game_override or o.get("game_dir", "~/Condor3"))
    return o


def common_block(o):
    with open(os.path.join(HERE, "hlsl", "mod_common.hlsl")) as fh:
        t = fh.read()
    vals = {
        "saturation": "%.4f" % o["grade_saturation"],
        "air_gamma": "%.4f" % o.get("aircraft_gamma", 0.7),
        "air_gain": "%.4f" % o.get("aircraft_gain", 1.0),
        "air_amb": "%.4f" % o.get("aircraft_ambient", 1.0),
        "cockpit_gain": "%.4f" % o.get("cockpit_gain", 1.0),
        "air_fresnel": "%.4f" % o.get("aircraft_fresnel", 0.0),
        "knee": "%.4f" % o.get("highlight_knee", 1.0),
        "cloud_gain": "%.4f" % o.get("cloud_gain", 1.0),
        "curve": "%.4f" % o["grade_contrast"],
        "dither": "%.4f" % o["dither"],
        "macro_detail": "%.4f" % o["macro_detail"],
        "shadow_strength": "%.4f" % o["shadow_strength"],
        "warm_sun": "%.4f" % o["warm_sun"],
        "cool_ambient": "%.4f" % o["cool_ambient"],
        "hemi_ambient": "%.4f" % o["hemi_ambient"],
        "sun_gain": "%.4f" % o["sun_gain"],
        "amb_gain": "%.4f" % o["amb_gain"],
        "water_glint": "%.4f" % o["water_glint"],
    }
    for k, v in vals.items():
        t = t.replace("@%s@" % k, v)
    assert "@" not in t, "unreplaced placeholder in mod_common.hlsl"
    return t


def apply_patches(originals, o):
    common = common_block(o)
    out = {}
    for name, fn in patches.PATCHES.items():
        if name not in originals:
            sys.exit("Game has no %s.fxc - cannot patch." % name)
        crlf = "\r\n" in originals[name]
        src = originals[name].replace("\r\n", "\n")
        try:
            src = fn(src, o)
        except patches.PatchError as e:
            sys.exit("PATCH FAILED for %s: %s\n(The game's shader differs from what this mod expects.)" % (name, e))
        text = common + src
        out[name] = text.replace("\n", "\r\n") if crlf else text
    return out


# ---------------------------------------------------------------- validation
def fxc_compile(game, workdir, fx_name):
    """Compile with the game's own fxc.exe, same target/flags as the game (fx_5_0, strictness)."""
    if not os.path.exists(WINE):
        return None, "CrossOver not found at %s" % WINE
    env = dict(os.environ, WINEDEBUG="-all")
    cmd = [WINE, "--bottle", BOTTLE, os.path.join(game, "fxc.exe"), "/nologo",
           "/T", "fx_5_0", "/Ges", "/Fo", fx_name + ".fxo", fx_name]
    p = subprocess.run(cmd, cwd=workdir, env=env, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, universal_newlines=True, timeout=300)
    text = p.stdout
    ok = "compilation succeeded" in text
    errs = [l for l in text.splitlines() if re.search(r"error|X\d{4}", l) and "mvk" not in l.lower()]
    return ok, "\n".join(errs[:12])


def validate(game, outdir, names):
    """Compile every patched shader with the game's own 32-bit d3dx11_43 +
    D3DCompiler_43 via tools/fxtest.exe under CrossOver."""
    import subprocess, tempfile
    wine = "/Applications/CrossOver.app/Contents/SharedSupport/CrossOver/bin/wine"
    if not os.path.exists(wine):
        print("  (skipping validation: CrossOver not found)")
        return True
    work = tempfile.mkdtemp(prefix="c3fx_")
    for f in ("d3dx11_43.dll", "D3DCompiler_43.dll"):
        shutil.copy(os.path.join(game, f), work)
    shutil.copy(os.path.join(HERE, "tools", "fxtest.exe"), work)
    files = [os.path.join(outdir, n + ".fx") for n in names]
    r = subprocess.run([wine, "--bottle", "Condor3", os.path.join(work, "fxtest.exe")] + files,
                       cwd=work, capture_output=True, text=True, timeout=600)
    out = r.stdout + r.stderr
    shutil.rmtree(work, ignore_errors=True)
    bad = 0
    for n in names:
        m = re.search(re.escape(n) + r"\.fx (ok|FAILED)", out)
        good = bool(m and m.group(1) == "ok")
        print("  %-20s %s" % (n, "compiles" if good else "FAILED"))
        bad += not good
    if bad:
        print(out[-3000:])
    return bad == 0


# ---------------------------------------------------------------- commands
def cmd_build(args, install=False):
    o = load_options(args.options, args.game)
    game = o["game_dir"]
    outdir = os.path.join(HERE, "build", "Effects", "fx")
    shutil.rmtree(os.path.join(HERE, "build"), ignore_errors=True)
    os.makedirs(outdir)

    print("Game: %s" % game)
    originals = load_originals(game)
    built = apply_patches(originals, o)
    for n, text in built.items():
        with open(os.path.join(outdir, n + ".fx"), "w", newline="") as fh:
            fh.write(text)
    print("Patched %d shaders." % len(built))

    if not args.no_validate:
        print("Validating with the game's own shader compiler (via CrossOver):")
        if not validate(game, outdir, list(built)):
            sys.exit("\nValidation failed - NOT installing. Fix the patch or lower the tweaks in options.json.")
    manifest = {n: hashlib.sha256(t.encode("latin-1")).hexdigest() for n, t in built.items()}
    with open(os.path.join(HERE, "build", "manifest.json"), "w") as fh:
        json.dump({"options": o, "sha256": manifest}, fh, indent=2)

    if install:
        dst = os.path.join(game, "Effects", "fx")
        os.makedirs(dst, exist_ok=True)
        for n in built:
            shutil.copy(os.path.join(outdir, n + ".fx"), os.path.join(dst, n + ".fx"))
        print("\nInstalled %d files to %s" % (len(built), dst))
        print("Start Condor normally. To go back to stock: python3 build_shaders.py uninstall")
    else:
        print("\nBuilt into %s (not installed)." % outdir)


def cmd_uninstall(args):
    o = load_options(args.options, args.game)
    dst = os.path.join(o["game_dir"], "Effects", "fx")
    if not os.path.isdir(dst):
        print("Nothing installed (%s does not exist)." % dst)
        return
    for n in patches.PATCHES:
        p = os.path.join(dst, n + ".fx")
        if os.path.exists(p):
            os.remove(p)
            print("removed", p)
    left = os.listdir(dst)
    if not left:
        os.rmdir(dst)
        print("removed empty folder", dst)
    else:
        print("left in place (not ours):", ", ".join(left))


def cmd_dump(args):
    o = load_options(args.options, args.game)
    os.makedirs(args.dir, exist_ok=True)
    for n, t in load_originals(o["game_dir"]).items():
        with open(os.path.join(args.dir, n + ".fx"), "w", newline="") as fh:
            fh.write(t)
    print("Decoded originals written to", args.dir)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["build", "install", "uninstall", "dump"])
    ap.add_argument("dir", nargs="?", help="output folder for 'dump'")
    ap.add_argument("--game", help="Condor 3 folder (default: options.json game_dir)")
    ap.add_argument("--options", default=os.path.join(HERE, "options.json"))
    ap.add_argument("--no-validate", action="store_true", help="skip the fxc compile check")
    a = ap.parse_args()
    if a.command == "build":
        cmd_build(a)
    elif a.command == "install":
        cmd_build(a, install=True)
    elif a.command == "uninstall":
        cmd_uninstall(a)
    elif a.command == "dump":
        if not a.dir:
            ap.error("dump needs an output folder")
        cmd_dump(a)


if __name__ == "__main__":
    main()
