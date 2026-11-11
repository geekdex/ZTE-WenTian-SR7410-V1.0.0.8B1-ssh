"""Patch exactly one script inside this device's hash-verified stock component."""
from __future__ import annotations
import copy
import gzip
import hashlib
import io
from pathlib import PurePosixPath
import tarfile

STOCK_SHA256 = "a3cb1f4c29f1228f313b1b1f9385317fa8cc9cf58010c56b95acd10690eac241"
KNOWN_STOCK_SHA256S = {
    STOCK_SHA256,
    "4b70059ff49906ac02b47ce1901b7efced216ef80ab383d93a26f78a55a2ee8d",
}
MONITOR = "dlspeed_rt/dlspeed_monitor.sh"
HOOK = """#!/bin/sh
# BEGIN SR7410 SSH AUTOSTART
if [ -f /Plugin/sr7410-ssh/autostart.enabled ]; then
    /Plugin/sr7410-ssh/start.sh >/dev/null 2>&1 &
    if [ -f /Plugin/sr7410-larktun/autostart.enabled ]; then
        /Plugin/sr7410-larktun/start.sh >/dev/null 2>&1 &
    fi
fi
# END SR7410 SSH AUTOSTART
""".encode("utf-8")


def make_boot_bundle(stock: bytes) -> bytes:
    digest = hashlib.sha256(stock).hexdigest()
    if digest != STOCK_SHA256 and digest not in KNOWN_STOCK_SHA256S:
        raise ValueError("Untested stock component version; refusing to patch")
    target, seen, count = io.BytesIO(), set(), 0
    with tarfile.open(fileobj=io.BytesIO(stock), mode="r:gz") as source:
        with gzip.GzipFile(fileobj=target, mode="wb", mtime=0, filename="") as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as dest:
                for original in source:
                    member = copy.copy(original)
                    name = member.name.removeprefix("./")
                    path = PurePosixPath(name)
                    if path.is_absolute() or ".." in path.parts or path.parts[0] != "dlspeed_rt":
                        raise ValueError("Unexpected stock archive layout")
                    if name in seen:
                        raise ValueError("Duplicate stock archive member")
                    seen.add(name)
                    data = source.extractfile(original).read() if original.isfile() else None
                    if name == MONITOR:
                        if data is None or not data.startswith(b"#!/bin/sh") or b"SR7410 SSH AUTOSTART" in data:
                            raise ValueError("Unexpected stock monitor")
                        data = HOOK + data
                        member.size = len(data)
                        count += 1
                    dest.addfile(member, io.BytesIO(data) if data is not None else None)
    if count != 1:
        raise ValueError("Expected exactly one stock monitor")
    return target.getvalue()
