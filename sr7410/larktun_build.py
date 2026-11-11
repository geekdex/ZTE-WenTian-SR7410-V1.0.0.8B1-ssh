"""Build the tested static ARM64 Larktun runtime; no accounts or node state."""
from __future__ import annotations
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import urllib.request
import certifi
from .runtime import ROOT, write_archive


def verify_binary(data: bytes, item: dict):
    if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
        raise ValueError(f"Size/SHA256 mismatch: {item['filename']}; do not bypass the lock")
    if data[:6] != b"\x7fELF\x02\x01" or data[18:20] != b"\xb7\x00":
        raise ValueError("Expected a little-endian AArch64 ELF64")
    offset = struct.unpack_from("<Q", data, 32)[0]
    entry_size, count = struct.unpack_from("<HH", data, 54)
    if entry_size < 56 or offset + entry_size * count > len(data):
        raise ValueError("Invalid ELF program headers")
    if any(struct.unpack_from("<I", data, offset + i * entry_size)[0] == 3 for i in range(count)):
        raise ValueError("Expected a static binary without a dynamic ELF interpreter")


def download(item: dict, urls: list[str], cache: Path) -> bytes:
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / item["filename"]
    if destination.exists():
        data = destination.read_bytes()
        verify_binary(data, item)
        return data
    for base_url in urls:
        try:
            url = base_url + item["filename"]
            if shutil.which("curl"):
                result = subprocess.run(
                    ["curl", "--fail", "--location", "--silent", "--show-error",
                     "--connect-timeout", "10", "--max-time", "180", "--retry", "1",
                     "--max-filesize", "67108864", url],
                    capture_output=True, timeout=370, check=True)
                data = result.stdout
            else:
                with urllib.request.urlopen(url, timeout=180) as response:
                    data = response.read(64 * 1024 * 1024 + 1)
        except (OSError, subprocess.SubprocessError):
            continue
        # A successful download with the wrong digest is a hard error, not a
        # reason to accept a different build or refresh the expected hash.
        verify_binary(data, item)
        partial = destination.with_suffix(".download")
        partial.write_bytes(data)
        partial.replace(destination)
        return data
    raise OSError(f"Cannot download {item['filename']}; use a verified --cache-dir")


def build(output=Path("build/larktun-runtime.tar.gz"), cache=Path(".cache/larktun")):
    lock = json.loads((ROOT / "larktun.lock.json").read_text())
    files, sizes = {}, []
    for item in lock["binaries"]:
        print(f"校验/准备 {item['filename']} ...", flush=True)
        data = download(item, lock["base_urls"], cache)
        files[item["target"] + ".gz"] = gzip.compress(data, compresslevel=9, mtime=0)
        sizes.append(f"{item['target'].upper()}_BYTES={len(data)}")
    files["sizes.conf"] = ("\n".join(sizes) + "\n").encode()
    files["ca-certificates.crt"] = Path(certifi.where()).read_bytes()
    for script in sorted((ROOT / "router/larktun").glob("*.sh")):
        files[script.name] = script.read_bytes()
    files["VERSION"] = (lock["version"] + "\n").encode()
    # Account state, Auth Keys and device names are deliberately absent.
    write_archive(output, files, {})
    return output


def main():
    parser = argparse.ArgumentParser(description="构建已校验的 SR7410 Larktun ARM64 安装包")
    parser.add_argument("--output", type=Path, default=Path("build/larktun-runtime.tar.gz"))
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/larktun"))
    args = parser.parse_args()
    try:
        output = build(args.output.expanduser(), args.cache_dir.expanduser())
    except (OSError, ValueError, struct.error) as exc:
        parser.exit(1, f"构建停止：{exc}\n")
    print(f"已生成：{output}；不包含账号、Auth Key 或登录状态。")


if __name__ == "__main__":
    main()
