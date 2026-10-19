#!/usr/bin/env python3
"""
Check the ReadyNAS NV+ (v1) X-RAID parity hypothesis before reconstructing anything.

For each surviving member it prints the LVM2 label and PV UUID found at the start
of the data partition, then the same for the XOR of all of them. If the XOR shows
'LABELONE' and the UUID of the PV that LVM reports as missing, the layout is
"linear LVM over data disks + one dedicated XOR parity disk", and the missing
disk can be rebuilt exactly.

  ./verify-parity.py --offset 4608003 /img/disk1.img /img/disk3.img /img/disk4.img
  ./verify-parity.py --offset 4608003 --metadata /img/disk1.img   # dump LVM metadata text
"""
import argparse
import re

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("images", nargs="+")
ap.add_argument("--offset", type=int, required=True, help="data partition start in 512-byte sectors")
ap.add_argument("--metadata", action="store_true", help="print the newest LVM metadata text from the first image")
args = ap.parse_args()

N = 1 << 20
OFF = args.offset * 512


def head(path):
    with open(path, "rb") as f:
        f.seek(OFF)
        return f.read(N)


def label(buf):
    for sector in range(4):                      # LVM2 label lives in one of the first 4 sectors
        b = sector * 512
        if buf[b:b + 8] == b"LABELONE":
            u = buf[b + 32:b + 64].decode("latin-1")
            uuid = "-".join([u[0:6], u[6:10], u[10:14], u[14:18], u[18:22], u[22:26], u[26:32]])
            return "LABELONE", sector, uuid
    return "-", None, "-"


bufs = [head(p) for p in args.images]
for p, b in zip(args.images, bufs):
    lab, sec, uuid = label(b)
    print(f"{p:40s} {lab:9s} sector={sec}  uuid={uuid!r}")

if len(bufs) > 1:
    acc = int.from_bytes(bufs[0], "little")
    for b in bufs[1:]:
        acc ^= int.from_bytes(b, "little")
    x = acc.to_bytes(N, "little")
    lab, sec, uuid = label(x)
    print(f"{'XOR of all of the above':40s} {lab:9s} sector={sec}  uuid={uuid!r}")

if args.metadata:
    text = bufs[0].decode("latin-1")
    starts = [m.start() for m in re.finditer(r"[A-Za-z0-9_+.-]+ \{\nid = ", text)]
    seqs = [(int(m.group(1)), m.start()) for m in re.finditer(r"seqno = (\d+)", text)]
    if not seqs:
        print("\nno LVM metadata text found")
    else:
        _, pos = max(seqs)
        begin = max([s for s in starts if s <= pos], default=pos)
        end = text.find("\0", pos)
        print("\n--- newest LVM metadata ---")
        print(text[begin:end if end > 0 else begin + 4000])
