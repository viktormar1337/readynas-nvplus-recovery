#!/usr/bin/env python3
"""
Materialise a missing ReadyNAS NV+ (v1) X-RAID member as an image file:
    missing = XOR of all surviving members (remaining data disks + parity).

Only the data partition is reconstructed (the LVM physical volume), so the output
can be attached directly with `losetup -r` and LVM will find the PV on it.
Reads the sources only. Safe to interrupt; re-run the same command to resume.

Example:
  ./make-missing-disk.py --offset 4608003 --size 3902384303 \
      --out /dest/disk2-p5.img /img/disk1.img /img/disk3.img /img/disk4.img
"""
import argparse
import os
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("images", nargs="+", help="surviving member images (data + parity)")
ap.add_argument("--offset", type=int, required=True, help="partition start in 512-byte sectors")
ap.add_argument("--size", type=int, required=True, help="partition length in 512-byte sectors")
ap.add_argument("--out", required=True, help="output image (partition only)")
ap.add_argument("--block", type=int, default=64, help="block size in MiB (default 64)")
args = ap.parse_args()
if len(args.images) < 2:
    ap.error("need at least two source images")

OFF, SIZE, BS, AHEAD = args.offset * 512, args.size * 512, args.block << 20, 3
fds = [os.open(p, os.O_RDONLY) for p in args.images]
out = os.open(args.out, os.O_WRONLY | os.O_CREAT, 0o644)
start = min(os.fstat(out).st_size // BS * BS, SIZE)       # resume on a whole block
pool = ThreadPoolExecutor(len(fds) * AHEAD)


def rd(fd, pos, n):
    buf = bytearray(n)
    mv = memoryview(buf)
    got = 0
    while got < n:
        r = os.preadv(fd, [mv[got:]], OFF + pos + got)
        if r <= 0:
            raise IOError(f"short read on source {fd} at byte {OFF + pos + got}")
        got += r
    return buf


def submit(pos):
    n = min(BS, SIZE - pos)
    return pos, [pool.submit(rd, fd, pos, n) for fd in fds]


queue = [submit(p) for p in range(start, min(start + AHEAD * BS, SIZE), BS)]
nxt = start + AHEAD * BS
t0, last = time.time(), 0.0
print(f"{args.out}: starting at {start / 1e9:.1f} GB of {SIZE / 1e9:.1f} GB", flush=True)
while queue:
    pos, futs = queue.pop(0)
    if nxt < SIZE:
        queue.append(submit(nxt))
        nxt += BS
    acc = np.frombuffer(futs[0].result(), dtype=np.uint8)   # writable (bytearray)
    for f in futs[1:]:
        acc ^= np.frombuffer(f.result(), dtype=np.uint8)
    mv = memoryview(acc)
    w = 0
    while w < len(mv):
        w += os.pwrite(out, mv[w:], pos + w)
    done = pos + len(mv)
    if time.time() - last > 10 or done == SIZE:
        last = time.time()
        rate = (done - start) / max(last - t0, 1e-9) / 1e6
        eta = (SIZE - done) / 1e6 / rate / 3600 if rate else 0
        print(f"{done / 1e9:9.1f} / {SIZE / 1e9:.1f} GB  {100 * done / SIZE:5.1f} %  "
              f"{rate:6.0f} MB/s  ~{eta:4.1f} h left", flush=True)
os.ftruncate(out, SIZE)
os.fsync(out)
print("Done.", flush=True)
