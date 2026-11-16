#!/usr/bin/env python3
"""DDS writers for Condor (see docs/TERRAIN_FORMAT.md 4.4 and 6.4): DXT1/DXT3 with a full mip chain, and uncompressed 32-bit BGRA."""
import math, struct
import numpy as np

def dds_header(w, h, fourcc, mips):
    bs = 8 if fourcc == b'DXT1' else 16
    return (b'DDS ' + struct.pack('<7I', 124, 0xA1007, h, w, max(1, w//4)*max(1, h//4)*bs, 0, mips) + b'\0'*44 +
            struct.pack('<2I4s5I', 32, 4, fourcc, 0, 0, 0, 0, 0) + struct.pack('<5I', 0x401008, 0, 0, 0, 0))

def bc1_blocks(img):
    h, w, _ = img.shape
    ph, pw = max(4, (h+3)//4*4), max(4, (w+3)//4*4)
    if (ph, pw) != (h, w): img = np.pad(img, ((0, ph-h), (0, pw-w), (0, 0)), mode='edge')
    b = img.reshape(ph//4, 4, pw//4, 4, 3).transpose(0, 2, 1, 3, 4).reshape(-1, 16, 3)
    out = np.empty((b.shape[0], 8), np.uint8)
    def to565(c): return ((c[..., 0]*31+127)//255 << 11) | ((c[..., 1]*63+127)//255 << 5) | ((c[..., 2]*31+127)//255)
    def from565(v): return np.stack([((v >> 11) & 31)*255//31, ((v >> 5) & 63)*255//63, (v & 31)*255//31], -1)
    sh = (2*np.arange(16, dtype=np.uint32))
    for s in range(0, b.shape[0], 65536):
        blk = b[s:s+65536].astype(np.int32)
        c0 = to565(blk.max(1)); c1 = to565(blk.min(1))
        c0, c1 = np.maximum(c0, c1), np.minimum(c0, c1)
        p0, p1 = from565(c0), from565(c1)
        # project every pixel on the axis p1 -> p0 and quantise to the 4 palette entries (0: c0, 1: c1, 2: 2/3 c0, 3: 1/3 c0)
        ax = (p0-p1).astype(np.int32); den = np.maximum((ax*ax).sum(-1), 1)
        t = (((blk-p1[:, None, :])*ax[:, None, :]).sum(-1)*6 + den[:, None]) // (2*den[:, None])                      # 0..3 along p1 -> p0
        idx = np.array([1, 3, 2, 0], np.uint32)[np.clip(t, 0, 3)]
        idx[(c0 == c1)] = 0
        bits = (idx << sh).sum(1, dtype=np.uint32)
        o = out[s:s+65536]
        o[:, 0] = c0 & 255; o[:, 1] = c0 >> 8; o[:, 2] = c1 & 255; o[:, 3] = c1 >> 8
        for k in range(4): o[:, 4+k] = (bits >> (8*k)) & 255
    return out

def alpha_blocks(a):
    h, w = a.shape
    ph, pw = max(4, (h+3)//4*4), max(4, (w+3)//4*4)
    if (ph, pw) != (h, w): a = np.pad(a, ((0, ph-h), (0, pw-w)), mode='edge')
    n = (a.reshape(ph//4, 4, pw//4, 4).transpose(0, 2, 1, 3).reshape(-1, 16).astype(np.uint16)+8)//17
    return (n[:, 0::2] | (n[:, 1::2] << 4)).astype(np.uint8)

def write_dxt(path, rgb, alpha=None):
    """rgb (H,W,3) uint8 with power-of-two sides; alpha (H,W) uint8 -> DXT3, else DXT1. Full mip chain, exact file size (the game checks it)."""
    h, w = rgb.shape[:2]; mips = int(math.log2(max(w, h)))+1
    parts = [dds_header(w, h, b'DXT3' if alpha is not None else b'DXT1', mips)]
    cur = rgb; ca = alpha
    for m in range(mips):
        blk = bc1_blocks(cur)
        if ca is not None: blk = np.concatenate([alpha_blocks(ca), blk], 1)
        parts.append(blk.tobytes())
        if m == mips-1: break
        hh, ww = max(1, cur.shape[0]//2), max(1, cur.shape[1]//2)
        fy, fx = cur.shape[0]//hh, cur.shape[1]//ww
        cur = (cur[:hh*fy, :ww*fx].reshape(hh, fy, ww, fx, 3).astype(np.uint16).sum((1, 3))//(fy*fx)).astype(np.uint8)
        if ca is not None: ca = (ca[:hh*fy, :ww*fx].reshape(hh, fy, ww, fx).astype(np.uint16).sum((1, 3))//(fy*fx)).astype(np.uint8)
    open(path, 'wb').write(b''.join(parts))

def write_bgra(path, rgb, alpha):
    """uncompressed 32-bit, no mips: the format of the stock airport sub-textures (alpha = 3D grass length)"""
    h, w = rgb.shape[:2]
    bgra = np.dstack([rgb[..., 2], rgb[..., 1], rgb[..., 0], alpha])
    hdr = b'DDS ' + struct.pack('<31I', 124, 0xA1007, h, w, w*h*4, 0, 1, *([0]*11), 32, 65, 0, 32, 0xFF0000, 0xFF00, 0xFF, 0xFF000000, 0x401008, 0, 0, 0, 0)
    open(path, 'wb').write(hdr + np.ascontiguousarray(bgra).tobytes())
