#!/usr/bin/env python3
"""Compare two compiled Condor landscapes (e.g. from tiles2condor v1 and v2) file by file, with numbers that say how different they are.

    .venv/bin/python rust/tiles2condor_v2/tools/compare.py <Landscapes>/A <Landscapes>/B
"""
import os, struct, sys, glob
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', 'tools'))
import c3d  # noqa: E402

A, B = sys.argv[1].rstrip('/'), sys.argv[2].rstrip('/')
na, nb = os.path.basename(A), os.path.basename(B)


def dxt_level0(path):
    d = open(path, 'rb').read()
    h, w = struct.unpack('<II', d[12:20])
    bs = 8 if d[84:88] == b'DXT1' else 16
    blk = np.frombuffer(d, np.uint8, (w // 4) * (h // 4) * bs, 128).reshape(h // 4, w // 4, bs)
    if bs == 16:
        blk = blk[:, :, 8:]
    u = blk.astype(np.uint32)
    c0, c1 = u[..., 0] | (u[..., 1] << 8), u[..., 2] | (u[..., 3] << 8)
    bits = u[..., 4] | (u[..., 5] << 8) | (u[..., 6] << 16) | (u[..., 7] << 24)

    def rgb(c):
        return np.stack([((c >> 11) & 31) * 255 // 31, ((c >> 5) & 63) * 255 // 63, (c & 31) * 255 // 31], -1).astype(np.int32)
    p0, p1 = rgb(c0), rgb(c1)
    four = (c0 > c1)[..., None]
    pal = np.stack([p0, p1, np.where(four, (2 * p0 + p1) // 3, (p0 + p1) // 2), np.where(four, (p0 + 2 * p1) // 3, 0)], 2)
    out = np.zeros((h // 4, w // 4, 4, 4, 3), np.int32)
    for k in range(16):
        sel = ((bits >> (2 * k)) & 3).astype(np.int64)
        out[:, :, k // 4, k % 4] = np.take_along_axis(pal, sel[:, :, None, None].repeat(3, 3), 2)[:, :, 0]
    return out.transpose(0, 2, 1, 3, 4).reshape(h, w, 3)


def report(rel, msg):
    print(f'{rel:45s} {msg}')


same = 0
files = sorted(set(os.path.relpath(p, A).replace(na + '.', '@.') for p in glob.glob(A + '/**/*', recursive=True) if os.path.isfile(p)))
for rel in files:
    fa, fb = os.path.join(A, rel.replace('@.', na + '.')), os.path.join(B, rel.replace('@.', nb + '.'))
    if not os.path.exists(fb):
        report(rel, 'MISSING in B'); continue
    da, db = open(fa, 'rb').read(), open(fb, 'rb').read()
    if da == db:
        same += 1; continue
    ext = rel.rsplit('.', 1)[-1]
    if ext == 'tr3f':
        a, b = np.frombuffer(da, '<f4'), np.frombuffer(db, '<f4'); d = np.abs(a - b)
        report(rel, f'heights: max diff {d.max():.3f} m, mean {d.mean():.4f} m, 99% {np.percentile(d, 99):.3f} m')
    elif ext == 'tr3':
        a, b = np.frombuffer(da, '<u2').astype(int), np.frombuffer(db, '<u2').astype(int); d = np.abs(a - b)
        report(rel, f'posts: {np.count_nonzero(d)} of {d.size} differ, max {d.max()} m')
    elif ext == 'trn':
        a, b = np.frombuffer(da[36:], '<u2').astype(int), np.frombuffer(db[36:], '<u2').astype(int); d = np.abs(a - b)
        report(rel, f'header same: {da[:36] == db[:36]}, posts: {np.count_nonzero(d)} of {d.size} differ, max {d.max()} m')
    elif ext == 'dds' and da[84:88] in (b'DXT1', b'DXT3'):
        a, b = dxt_level0(fa), dxt_level0(fb); d = np.abs(a - b).max(-1)
        report(rel, f'level 0: {100 * np.mean(d == 0):.1f}% px identical, {100 * np.mean(d > 16):.2f}% differ by >16, mean abs {np.abs(a - b).mean():.2f}')
    elif ext == 'dds':
        a, b = np.frombuffer(da, np.uint8)[128:].astype(int), np.frombuffer(db, np.uint8)[128:].astype(int)
        if a.size != b.size: report(rel, f'size {a.size} vs {b.size}'); continue
        d = np.abs(a - b).reshape(-1, 4).max(-1)
        report(rel, f'BGRA: {100 * np.mean(d == 0):.1f}% px identical, {100 * np.mean(d > 16):.2f}% differ by >16')
    elif ext == 'c3d':
        ca, cb = c3d.parse(da), c3d.parse(db)
        ta, tb = len(ca['indices']) // 3, len(cb['indices']) // 3
        msg = f'objects {len(ca["objects"])} / {len(cb["objects"])}, triangles {ta} / {tb} ({100 * (tb - ta) / max(ta, 1):+.2f}%)'
        va, vb = np.asarray(ca['vertices']), np.asarray(cb['vertices'])
        if va.shape == vb.shape:
            msg += f', vertex pos max diff {np.abs(va[..., :3] - vb[..., :3]).max():.4f} m, uv max diff {np.abs(va[..., 6:8] - vb[..., 6:8]).max():.4f}'
        report(rel, msg)
    elif ext == 'apt':
        for i in range(len(da) // 72):
            ra, rb = da[i * 72:(i + 1) * 72], db[i * 72:(i + 1) * 72]
            report(rel, f'#{i} {ra[1:1 + ra[0]].decode()}: lat/lon/elev/dir/len/wid {struct.unpack("<3f3i", ra[36:60])} vs {struct.unpack("<3f3i", rb[36:60])}')
    elif ext in ('tm3', 'bmp'):
        a, b = np.frombuffer(da, np.uint8).astype(int), np.frombuffer(db, np.uint8).astype(int)
        d = np.abs(a - b) if a.size == b.size else None
        report(rel, f'{np.count_nonzero(d)} of {d.size} bytes differ, max {d.max()}' if d is not None else 'size differs')
    else:
        report(rel, f'differs ({len(da)} / {len(db)} bytes)' + (f': {da[:150]!r} | {db[:150]!r}' if len(da) < 400 else ''))
print(f'{same} of {len(files)} files byte-identical')
