#!/usr/bin/env python3
"""Condor airport list (<Landscape>.apt): array of 72-byte records.  Layout from LandscapeEditor.exe (airport dialog OK handler @0x5e3340,
loader @0x5ee2a0) and verified by byte-exact round trip of Slovenia3.apt (39 records).

  0x00  Pascal ShortString[35]: length byte + up to 35 chars (airport name, game name; also used for <name>G.c3d / <name>O.c3d)
  0x24  f32  latitude  [deg]
  0x28  f32  longitude [deg]
  0x2c  f32  elevation [m]
  0x30  i32  runway direction code = (whole_deg + 1000)*100 + hundredths   e.g. 106493 = 64.93 deg (geographic, clockwise from N)
  0x34  i32  runway length [m]
  0x38  i32  runway width  [m]
  0x3c  u8   asphalt runway (1) / grass (0)           (+3 bytes padding)
  0x40  f32  radio frequency [MHz] (e.g. 123.5)
  0x44  u8   "Primary dir reversed"
  0x45  u8   "Tow primary dir left side"
  0x46  u8   "Tow secondary dir left side"
  0x47  u8   unused (0)
"""
import struct, sys
REC = struct.Struct('<36sfffiiiB3xfBBBB')
assert REC.size == 72

def unpack(data):
    out = []
    for i in range(0, len(data), 72):
        n, lat, lon, elev, d, ln, w, asph, freq, rev, tp, ts, pad = REC.unpack_from(data, i)
        out.append(dict(name=n[1:1+n[0]].decode('latin1'), lat=lat, lon=lon, elev=elev, direction=d/100.0-1000.0,
                        length=ln, width=w, asphalt=bool(asph), freq=freq, reversed=bool(rev),
                        tow_primary_left=bool(tp), tow_secondary_left=bool(ts), _pad=pad, _rawname=n, _rawdir=d))
    return out

def pack_one(name, lat, lon, elev, direction, length, width, asphalt=False, freq=123.5, reversed=False,
             tow_primary_left=False, tow_secondary_left=False):
    nb = name.encode('latin1')[:35]
    return REC.pack(bytes([len(nb)]) + nb.ljust(35, b'\0'), lat, lon, elev, int(round((direction+1000)*100)), length, width,
                    int(asphalt), freq, int(reversed), int(tow_primary_left), int(tow_secondary_left), 0)

if __name__ == '__main__':
    d = open(sys.argv[1], 'rb').read()
    for a in unpack(d): print({k: v for k, v in a.items() if not k.startswith('_')})
