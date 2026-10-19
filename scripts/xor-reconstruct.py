"""
nbdkit plugin: expose a missing ReadyNAS NV+ (v1, SPARC) X-RAID member as the
byte-wise XOR of all surviving members (remaining data disks + parity disk).

Usage (read-only):
  nbdkit -r python xor-reconstruct.py \
      images=/img/disk1.img,/img/disk3.img,/img/disk4.img \
      offset=<partition start, 512-byte sectors> \
      size=<partition length, 512-byte sectors>

Then attach it:  nbd-client -b 512 localhost /dev/nbd0
"""
import os
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import nbdkit
import numpy as np

API_VERSION = 2

CHUNK = 4 << 20    # cache unit for sequential reads
PIECE = 1 << 20    # each source read is at most 1 MiB, all issued in parallel
AHEAD = 6          # chunks to prefetch once reads are clearly sequential
KEEP = 64          # chunks kept in cache (~256 MiB RAM)

cfg = {"images": None, "offset": None, "size": None}
io = ThreadPoolExecutor(32)
bg = ThreadPoolExecutor(8)
lock = threading.Lock()
cache = OrderedDict()            # chunk index -> Future(bytes)
state = {"end": -1, "run": 0}
fds = []


def config(key, value):
    if key == "images":
        cfg["images"] = [p for p in value.split(",") if p]
    elif key in ("offset", "size"):
        cfg[key] = int(value)
    else:
        raise RuntimeError(f"unknown parameter: {key}")


def config_complete():
    if not cfg["images"] or len(cfg["images"]) < 2:
        raise RuntimeError("images= needs at least two comma-separated files")
    if cfg["offset"] is None or cfg["size"] is None:
        raise RuntimeError("offset= and size= (512-byte sectors) are required")


def thread_model():
    return nbdkit.THREAD_MODEL_PARALLEL


def open(readonly):
    with lock:
        if not fds:
            fds.extend(os.open(p, os.O_RDONLY) for p in cfg["images"])
    return 1


def get_size(h):
    return cfg["size"] * 512


def can_multi_conn(h):
    return True


def _xor_range(off, n):
    base = cfg["offset"] * 512 + off
    futs = [[io.submit(os.pread, fd, min(PIECE, n - p), base + p)
             for p in range(0, n, PIECE)] for fd in fds]
    r = np.zeros(n, dtype=np.uint8)
    for row in futs:
        p = 0
        for f in row:
            piece = np.frombuffer(f.result(), dtype=np.uint8)
            if len(piece) == 0:
                raise IOError("short read from a source image")
            np.bitwise_xor(r[p:p + len(piece)], piece, out=r[p:p + len(piece)])
            p += len(piece)
    return r.tobytes()


def _chunk(idx):
    start = idx * CHUNK
    return _xor_range(start, min(CHUNK, get_size(None) - start))


def _future(idx):                # call with lock held
    f = cache.get(idx)
    if f is None:
        f = bg.submit(_chunk, idx)
        cache[idx] = f
    else:
        cache.move_to_end(idx)
    return f


def pread(h, buf, offset, flags):
    n = len(buf)
    first, last = offset // CHUNK, (offset + n - 1) // CHUNK
    size = get_size(None)
    with lock:
        # sequential = forward continuation of the previous read (small gap allowed)
        if 0 <= offset - state["end"] <= (256 << 10):
            state["run"] += 1
        else:
            state["run"] = 0
        seq = state["run"] >= 2
        state["end"] = offset + n
        use_cache = seq or all(i in cache for i in range(first, last + 1))
        if use_cache:
            futs = [_future(i) for i in range(first, last + 1)]
            if seq:
                for j in range(last + 1, last + 1 + AHEAD):
                    if j * CHUNK < size:
                        _future(j)
            while len(cache) > KEEP:
                cache.popitem(last=False)
    if use_cache:
        pos = 0
        for i, f in zip(range(first, last + 1), futs):
            chunk = memoryview(f.result())
            s = offset + pos - i * CHUNK
            take = min(n - pos, len(chunk) - s)
            buf[pos:pos + take] = chunk[s:s + take]
            pos += take
    else:
        buf[:] = _xor_range(offset, n)   # random access: read exactly what is needed
