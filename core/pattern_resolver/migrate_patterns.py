#!/usr/bin/env python3
"""Seed-era migration aid (not in the pipeline): rescan previous sigs
against new DLLs, entry-check via .pdata, emit per-SHA TOMLs.
Entries with 0 or 2+ hits are SKIPped for manual triage.

Usage:
    python3 tools/migrate_patterns.py \
        --steamclient-dll "C:/Program Files (x86)/Steam/steamclient64.dll" \
        --steamui-dll "C:/Program Files (x86)/Steam/SteamUI.dll" \
        --steamclient-toml <previous steamclient toml> \
        --steamui-toml <previous steamui toml> \
        --out-dir "C:/Program Files (x86)/Steam/opensteamtool/pattern"
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import re
import struct
import sys
from pathlib import Path

ENTRY_RE = re.compile(
    r'\[(0x[0-9A-Fa-f]+)\]\s*\nname = "([^"]+)"\s*\n'
    r'rva = "(0x[0-9A-Fa-f]+)"\s*\nsig = "([0-9A-Fa-f? ]+)"'
)

HEADER = ("# HANDROLLED test signature - generated locally by sig-scan of current DLL\n"
          "# Missing entries omitted on purpose (hook disables gracefully). Do not upstream as-is.\n")

HARDEN_CAP = 80  # max sig length when auto-hardening (CUtlMemoryGrow needed 56)


def harden_sig(toks: list[str], true_rva: int, true_bytes: bytes,
               scan_fn) -> str | None:
    """Extend a multi-hit sig with bytes at the known-good RVA until unique.
    Same-build only (prev RVA is ground truth); None past HARDEN_CAP."""
    cur = list(toks)
    while len(cur) < HARDEN_CAP and len(cur) < len(true_bytes):
        cur.append(f"{true_bytes[len(cur)]:02X}")
        hits = scan_fn(cur)
        if len(hits) == 1 and hits[0] == true_rva:
            return " ".join(cur)
    return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Image:
    def __init__(self, path: Path):
        self.buf = open(path, "rb").read()
        buf = self.buf
        e = struct.unpack_from("<I", buf, 0x3C)[0]
        assert buf[e:e + 4] == b"PE\x00\x00", f"{path} is not a PE"
        self.is64 = struct.unpack_from("<H", buf, e + 4)[0] == 0x8664
        nsec = struct.unpack_from("<H", buf, e + 6)[0]
        optsz = struct.unpack_from("<H", buf, e + 20)[0]
        o = e + 24 + optsz
        self.secs = []
        for _ in range(nsec):
            name = buf[o:o + 8].rstrip(b"\x00")
            vsz, va, rawsz, raw = struct.unpack_from("<IIII", buf, o + 8)
            self.secs.append((name, va, vsz, raw, rawsz))
            o += 40
        dd = e + 24 + 112  # PE32+ data directories
        self.exc_rva, self.exc_sz = struct.unpack_from("<II", buf, dd + 8 * 3)
        text = next(s for s in self.secs if s[0] == b".text")
        _, self.tva, _, self.traw, self.trawsz = text

    def rva2off(self, rva: int) -> int | None:
        for _, va, vsz, raw, rawsz in self.secs:
            if va <= rva < va + max(vsz, rawsz):
                return raw + (rva - va)
        return None

    def pdata_entries(self) -> list[int]:
        off = self.rva2off(self.exc_rva)
        if off is None:
            return []
        n = self.exc_sz // 12
        out = set()
        for i in range(n):
            (begin,) = struct.unpack_from("<I", self.buf, off + i * 12)
            out.add(begin)
        return sorted(out)

    def scan(self, sig: list[int | None]) -> list[int]:
        if all(b is None for b in sig):
            return []
        pat = b"(?=" + b"".join(
            b"." if b is None else re.escape(bytes((b,)))
            for b in sig) + b")"
        lo, hi = self.traw, self.traw + self.trawsz
        tva = self.tva
        return [tva + (m.start() - self.traw)
                for m in re.compile(pat).finditer(self.buf)
                if lo <= m.start() < hi]


def parse_sig(s: str) -> list[int | None]:
    return [None if t in ("??", "?") else int(t, 16) for t in s.split()]


def load_toml(path: Path):
    return [(m.group(1), m.group(2), m.group(3), m.group(4))
            for m in ENTRY_RE.finditer(path.read_text())]


def entry_status(entries: list[int], rva: int) -> str:
    i = bisect.bisect_right(entries, rva) - 1
    if i < 0:
        return "no-pdata"
    ent = entries[i]
    return "ISENTRY" if ent == rva else f"interior +{rva - ent:#x} of {ent:#x}"


def migrate(dll: Path, prev_toml: Path, out_path: Path) -> int:
    img = Image(dll)
    entries = img.pdata_entries()
    prev = load_toml(prev_toml)
    same_build = prev_toml.stem == out_path.stem
    kept, skipped = [], []
    for h, name, old_rva, sig in prev:
        toks = sig.split()
        hits = img.scan(parse_sig(sig))
        if len(hits) != 1:
            if len(hits) > 1 and same_build:
                off = img.rva2off(int(old_rva, 16))
                hard = (harden_sig(toks, int(old_rva, 16),
                                   img.buf[off:off + HARDEN_CAP],
                                   lambda t: img.scan(parse_sig(" ".join(t))))
                        if off is not None else None)
                if hard is not None:
                    print(f"  OK {name}: {old_rva} -> {old_rva} "
                          f"[hardened {len(toks)}->{len(hard.split())}]")
                    kept.append((h, name, old_rva, hard))
                    continue
            skipped.append((name, f"{len(hits)} hits"))
            continue
        rva = hits[0]
        off = img.rva2off(rva)
        raw = img.buf[off:off + len(sig.split())]
        toks = sig.split()
        if not all(t == "??" or b == int(t, 16) for b, t in zip(raw, toks)):
            skipped.append((name, "bytes mismatch at hit"))
            continue
        print(f"  OK {name}: {old_rva} -> 0x{rva:X} [{entry_status(entries, rva)}]")
        kept.append((h, name, f"0x{rva:X}", sig))
    lines = [HEADER]
    for h, name, rva, sig in kept:
        lines += [f"[{h}]", f'name = "{name}"', f'rva = "{rva}"', f'sig = "{sig}"', ""]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines))
    for name, why in skipped:
        print(f"  SKIP {name}: {why} (needs manual triage)")
    print(f"wrote {out_path.name}: {len(kept)} kept, {len(skipped)} skipped")
    return len(skipped)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--steamclient-dll", required=True)
    ap.add_argument("--steamui-dll", required=True)
    ap.add_argument("--steamclient-toml", required=True)
    ap.add_argument("--steamui-toml", required=True)
    ap.add_argument("--out-dir", required=True)
    a = ap.parse_args()
    for label, p in [("steamclient-toml", a.steamclient_toml),
                     ("steamui-toml", a.steamui_toml),
                     ("steamclient-dll", a.steamclient_dll),
                     ("steamui-dll", a.steamui_dll)]:
        if not Path(p).is_file():
            print(f"error: --{label} not found: {p!r}")
            return 2
    out = Path(a.out_dir)
    sc_dll, sui_dll = Path(a.steamclient_dll), Path(a.steamui_dll)
    sc_sha, sui_sha = sha256_file(sc_dll), sha256_file(sui_dll)
    print(f"steamclient64: {sc_sha}")
    print(f"SteamUI:       {sui_sha}")
    bad = 0
    bad += migrate(sc_dll, Path(a.steamclient_toml),
                   out / "steamclient" / f"{sc_sha}.toml")
    bad += migrate(sui_dll, Path(a.steamui_toml),
                   out / "steamui" / f"{sui_sha}.toml")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
