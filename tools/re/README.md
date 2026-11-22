Small reverse-engineering helpers used for Condor.exe / LandscapeEditor.exe / ObjectEditor.exe (32-bit Delphi PE files):
- `dasm.py exe VA [nbytes|f]`  objdump wrapper that annotates Delphi string literals and float constants (do not name it dis.py: shadows the stdlib)
- `strref.py exe text...`      find strings containing text and the code addresses that reference them (use specific text, it is case-insensitive)
- `callers.py exe VA...`       direct callers (E8 rel32) of an address
- `dfm.py exe outdir`          dump Delphi forms (RCDATA TPF0) as text: captions, event handler names
- `meth` idea: handler address = u32 right before the ShortString method name in the class method table (see docs, session 5)
- `uvfit.py n`                 affine UV fit of every G-file object of all stock airports
