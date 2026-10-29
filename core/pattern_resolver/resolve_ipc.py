"""Zero-seed IPC resolver: steamclient64.dll in, ipc/steamclient/<sha>.toml out.

Emits the IPC TOML schema the client parses in IPCLoader.cpp:
per-interface [Name] (interface_id, vtable_rva) plus per-method
[Name.Method] (method_index, funcHash, wrapper_rva, fencepost, argc).

funcHash / fencepost values are wire constants the wrapper writes into the
outgoing IPC buffer (4-byte appends). They are NOT build-stable, and stable
and beta roll them independently: the 1792192922 beta re-rolled five of six
methods while stable kept the old pair. Select the channel's set with
--channel. Re-derive on failure: disassemble the slot's wrapper, find each
`mov dword ptr [..], imm32` followed by the 4-byte append helper call; first
immediate = funcHash, second = fencepost. Only the two RVAs resolve per
build. Resolution chain per interface, all asserted unique (FAIL loudly):

  RTTI TypeDescriptor name (pinned, e.g. .?AVIClientUserMap@@)
    -> Complete Object Locator (sig == 1, pTD == TD)
      -> vtable: the address X whose qword at X-8 == COL VA
        -> wrapper: qword at X + method_index * 8, must be a pdata entry
           whose first HASH_WINDOW bytes contain the method's funcHash.

Usage:
    python3 tools/resolve_ipc.py resolve --dll <dll> --channel stable
        --out-dir ipc/ [--verify <ground-truth.toml>]
"""

import argparse
import struct
import sys
import tomllib
from pathlib import Path

try:  # package import (vendored under core/pattern_resolver)
    from . import resolve_patterns as rp
except ImportError:  # direct CLI execution
    sys.path.insert(0, str(Path(__file__).parent))
    import resolve_patterns as rp

HASH_WINDOW = 0x400

# iface -> interface_id, RTTI TypeDescriptor name, and methods:
# (method, method_index, argc). Method order and indexes are wire-stable;
# only the funcHash/fencepost pair differs per channel.
IPC_IFACES = {
    "IClientUser": {
        "id": 1,
        "td": b".?AVIClientUserMap@@",
        "methods": [
            ("GetSteamID", 10, 0),
            ("GetAppOwnershipTicketExtendedData", 105, 2),
            ("RequestEncryptedAppTicket", 120, 2),
            ("GetEncryptedAppTicket", 121, 1),
        ],
    },
    "IClientUtils": {
        "id": 4,
        "td": b".?AVIClientUtilsMap@@",
        "methods": [
            ("GetAppID", 19, 0),
            ("GetAPICallResult", 24, 3),
        ],
    },
}

# Per-channel wire constants: iface -> method -> (funcHash, fencepost).
# Stable and beta are separate builds and have re-rolled these independently.
IPC_CONSTANTS = {
    "stable": {
        "IClientUser": {
            "GetSteamID": ("D6FC3200", "D7058CA5"),
            "GetAppOwnershipTicketExtendedData": ("C7E71245", "C8449840"),
            "RequestEncryptedAppTicket": ("25D6BB1D", "2646B663"),
            "GetEncryptedAppTicket": ("E0468CB4", "E0B80200"),
        },
        "IClientUtils": {
            "GetAppID": ("09607EC4", "0AFE7552"),
            "GetAPICallResult": ("2D3D3947", "2EDF5EE6"),
        },
    },
    "beta": {
        "IClientUser": {
            "GetSteamID": ("D6FC3200", "D7058CA5"),
            "GetAppOwnershipTicketExtendedData": ("5578709F", "55D3418F"),
            "RequestEncryptedAppTicket": ("BBB80477", "BC2703B9"),
            "GetEncryptedAppTicket": ("25D6BB1C", "2646F563"),
        },
        "IClientUtils": {
            "GetAppID": ("09607EC2", "0AFEF352"),
            "GetAPICallResult": ("2D3D3979", "2EDFDD1A"),
        },
    },
}


def channel_spec(iface, spec, channel):
    """Attach the channel's wire constants to one interface spec."""
    consts = IPC_CONSTANTS[channel][iface]
    methods = []
    for name, idx, argc in spec["methods"]:
        func_hash, fence = consts[name]
        methods.append((name, idx, func_hash, fence, argc))
    return {**spec, "methods": methods}


def image_base(buf) -> int:
    e = struct.unpack_from("<I", buf, 0x3C)[0]
    return struct.unpack_from("<Q", buf, e + 48)[0]


def data_spans(img):
    """(rva, size) of data sections that can host RTTI (order-stable)."""
    return [(va, max(vsz, rawsz)) for name, va, vsz, _raw, rawsz in img.secs
            if name in (b".rdata", b".data")]


def span_bytes(img, rva, size):
    """Contiguous bytes for [rva, rva+size), crossing sections if needed."""
    out = bytearray()
    cur = rva
    while len(out) < size:
        o = img.rva2off(cur)
        if o is None:
            return None
        end = cur + 1
        while end < rva + size and img.rva2off(end) == o + (end - cur):
            end += 1
        out += bytes(img.buf[o:o + (end - cur)])
        cur = end
    return bytes(out)


def _u32(img, rva):
    o = img.rva2off(rva)
    if o is None:
        return None
    return struct.unpack_from("<I", img.buf, o)[0]


def _u64(img, rva):
    o = img.rva2off(rva)
    if o is None:
        return None
    return struct.unpack_from("<Q", img.buf, o)[0]


def find_td(img, spans, name: bytes):
    """RVA of the TypeDescriptor whose mangled name matches (name at +16)."""
    cands = set()
    for rva, size in spans:
        blob = span_bytes(img, rva, size)
        if blob is None:
            continue
        off = 0
        key = bytes(name) + b"\x00"
        while True:
            i = blob.find(key, off)
            if i < 0:
                break
            cands.add(rva + i - 16)
            off = i + 1
    if len(cands) != 1:
        return None
    return next(iter(cands))


def find_vtable(img, spans, td_rva, base):
    """The one address X whose qword at X-8 is the VA of a COL for td."""
    cols = set()
    for rva, size in spans:
        blob = span_bytes(img, rva, size)
        if blob is None:
            continue
        for i in range(0, len(blob) - 4, 4):
            if struct.unpack_from("<I", blob, i)[0] != td_rva:
                continue
            col = rva + i - 12
            if _u32(img, col) == 1:
                cols.add(col)
    if not cols:
        return None, "0 COL cands"
    want = {base + c for c in cols}
    match = set()
    for rva, size in spans:
        blob = span_bytes(img, rva, size)
        if blob is None:
            continue
        for i in range(0, len(blob) - 8, 8):
            if struct.unpack_from("<Q", blob, i)[0] in want:
                match.add(rva + i + 8)
    if len(match) != 1:
        return None, f"{len(match)} vtable cands"
    return next(iter(match)), None


def check_wrapper(img, func_rva, funchash: str, pdata) -> bool:
    if func_rva not in pdata:
        return False
    o = img.rva2off(func_rva)
    if o is None:
        return False
    return bytes.fromhex(funchash)[::-1] in bytes(img.buf[o:o + HASH_WINDOW])


def resolve_iface(spec, img, spans, base, pdata):
    td = find_td(img, spans, spec["td"])
    if td is None:
        return None, None, f'no TD {spec["td"]!r}'
    vt, err = find_vtable(img, spans, td, base)
    if err is not None:
        return None, None, err
    methods = {}
    for name, idx, funchash, _fence, _argc in spec["methods"]:
        f = _u64(img, vt + idx * 8)
        if f is None:
            return None, None, f"slot {idx} unreadable"
        f -= base
        if not check_wrapper(img, f, funchash, pdata):
            return None, None, f"{name}: wrapper {f:#x} fails hash check"
        methods[name] = f
    return vt, methods, None


def render_toml(resolved) -> str:
    parts = []
    for iface, spec, vt, methods in resolved:
        parts.append("\n".join(
            [f"[{iface}]", f'interface_id = {spec["id"]}',
             f'vtable_rva = "0x{vt:X}"']))
        for name, idx, funchash, fence, argc in spec["methods"]:
            parts.append("\n".join(
                [f"[{iface}.{name}]", f"method_index = {idx}",
                 f'funcHash = "0x{funchash.upper()}"',
                 f'wrapper_rva = "0x{methods[name]:X}"',
                 f'fencepost = "0x{fence.upper()}"', f"argc = {argc}"]))
    head = "# MACHINE-GENERATED by tools/resolve_ipc.py - do not hand-edit"
    return head + "\n" + "\n\n".join(parts) + "\n"


def cmd_resolve(args):
    dll = Path(args.dll)
    img = rp.mp.Image(dll)
    spans = data_spans(img)
    if not spans:
        print("FAIL: no data sections")
        return 1
    base = image_base(img.buf)
    entries = sorted(img.pdata_entries())
    pdata = set(entries)
    resolved, failed = [], []
    for iface, base_spec in IPC_IFACES.items():
        spec = channel_spec(iface, base_spec, args.channel)
        vt, methods, err = resolve_iface(spec, img, spans, base, pdata)
        if err is not None:
            failed.append((iface, err))
            print(f"FAIL {iface}: {err}")
        else:
            resolved.append((iface, spec, vt, methods))
            print(f"{iface}: vtable=0x{vt:X} " +
                  " ".join(f"{m}=0x{w:X}" for m, w in methods.items()))
    if args.verify:
        truth = tomllib.load(open(args.verify, "rb"))
        for iface, spec, vt, methods in resolved:
            t = truth.get(iface, {})
            mark = "OK" if t.get("vtable_rva") == f"0x{vt:X}" else \
                f'MISMATCH truth={t.get("vtable_rva")}'
            print(f"{mark} {iface}.vtable_rva")
            for name, idx, funchash, _fence, _argc in spec["methods"]:
                tm = truth.get(iface, {}).get(name, {})
                mark = "OK" if tm.get("wrapper_rva") == \
                    f'0x{methods[name]:X}' else \
                    f'MISMATCH truth={tm.get("wrapper_rva")}'
                print(f"{mark} {iface}.{name}.wrapper_rva")
    if args.out_dir and not failed:
        from hashlib import sha256
        text = render_toml(resolved)
        out = Path(args.out_dir) / "steamclient" / f"{sha256(dll.read_bytes()).hexdigest()}.toml"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, newline="\n")
        print(f"wrote {out}")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("resolve")
    r.add_argument("--dll", required=True)
    r.add_argument("--channel", required=True, choices=sorted(IPC_CONSTANTS))
    r.add_argument("--out-dir", default=None)
    r.add_argument("--verify", default=None)
    args = ap.parse_args()
    if args.cmd == "resolve":
        return cmd_resolve(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
