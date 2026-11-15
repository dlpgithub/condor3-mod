#!/usr/bin/env python3
"""Condor 3 tile hash (FUN_004b46c8). usage: terrain_hash.py <file> <tx> <ty> <tileCountY>"""
import sys
def condor_hash(data: bytes, idx: int) -> int:
    s = (sum(data) + len(data)) & 0xFFFFFFFF
    seed = ((idx + 0x14A68F2) * 0x41C64E6D + 1) & 0xFFFFFFFF
    return int(round(s * (seed / 4294967296.0)))
if __name__ == "__main__":
    f, tx, ty, cy = sys.argv[1], *map(int, sys.argv[2:5])
    print(condor_hash(open(f, "rb").read(), cy * tx + ty))
