#!/usr/bin/env python3
"""Build a Condor 3 thermal/terrain map (.tm3) from a .trn (90 m heights).

Format (verified against Slovenia3.tm3 and LandscapeEditor.exe 'Export thermal map', routine at 0x5ecdf0):
  width*height*3 bytes, rows y = 0..H-1 (y=0 is the SOUTH edge), columns x = 0..W-1 (x=0 is the EAST edge),
  i.e. tm3[y][x] pairs with trn[x][y].  One cell = 90 m.
  byte0 = user thermal map value (grayscale ThermalMap.bmp, north-up/west-left bitmap; 0=no thermals ... 255=strong)
  byte1 = clamp(stddev(h in disk) * 0.01, 0, 1) * 255
  byte2 = clamp(mean((h0-h)/dist) * 0.005, -0.5, 0.5) * 254 + 127          (rounded)
  disk: sample offsets (i,j) in [-8,8]^2 with i^2+j^2 <= 64, sample cell = (x+2i, y+2j) (skipped outside the map),
        dist = sqrt(i^2+j^2) (index units, not scaled by the stride), centre excluded for the slope term.
usage: make_tm3.py in.trn out.tm3 [thermal_gray_value_or_file.npy]
"""
import sys, struct, numpy as np
STRIDE, R = 2, 8
OFFS = [(i, j) for i in range(-R, R+1) for j in range(-R, R+1) if i*i+j*j <= R*R]

def load_trn(p):
    d = open(p, 'rb').read(); w, h = struct.unpack_from('<II', d, 0)
    return np.frombuffer(d, dtype='<u2', offset=36, count=w*h).reshape(w, h)          # [x][y]

def channels(H, xs, xe):
    """channels 1,2 for columns x in [xs,xe) and all y -> uint8 arrays [y][x]"""
    W, Hh = H.shape; nx = xe-xs
    Hf = H.astype(np.float64)
    n = np.zeros((nx, Hh)); s1 = np.zeros((nx, Hh)); s2 = np.zeros((nx, Hh)); sl = np.zeros((nx, Hh)); ns = np.zeros((nx, Hh))
    h0 = Hf[xs:xe]
    for i, j in OFFS:
        ox, oy = i*STRIDE, j*STRIDE
        x0 = max(xs+ox, 0); x1 = min(xe+ox, W); y0 = max(oy, 0); y1 = min(Hh+oy, Hh)
        if x0 >= x1 or y0 >= y1: continue
        sub = Hf[x0:x1, y0:y1]
        dx = slice(x0-ox-xs, x1-ox-xs); dy = slice(y0-oy, y1-oy)
        n[dx, dy] += 1; s1[dx, dy] += sub; s2[dx, dy] += sub*sub
        if i or j:
            sl[dx, dy] += (h0[dx, dy]-sub)/np.hypot(i, j); ns[dx, dy] += 1
    sd = np.sqrt(np.maximum((s2-s1*s1/n)/n, 0))
    g = np.rint(np.clip(sd*0.01, 0, 1)*255)
    b = np.rint(np.clip(sl/np.maximum(ns, 1)*0.005, -0.5, 0.5)*254+127)
    return g.T.astype(np.uint8), b.T.astype(np.uint8)

def make_tm3(H, thermal=102):
    W, Hh = H.shape
    out = np.zeros((Hh, W, 3), np.uint8)
    out[..., 0] = thermal                                   # scalar or [y][x] array
    for xs in range(0, W, 256):
        xe = min(xs+256, W); g, b = channels(H, xs, xe)
        out[:, xs:xe, 1] = g; out[:, xs:xe, 2] = b
    return out

if __name__ == '__main__':
    H = load_trn(sys.argv[1])
    th = 102
    if len(sys.argv) > 3:
        th = int(sys.argv[3]) if sys.argv[3].isdigit() else np.load(sys.argv[3])
    make_tm3(H, th).tofile(sys.argv[2])
