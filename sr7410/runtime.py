"""Download hash-pinned Entware packages and build the isolated SSH runtime."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
LIB_FILES = (
    "ld-2.27.so", "libc-2.27.so", "libcrypt-2.27.so", "libgcc_s.so.1",
    "libnss_dns-2.27.so", "libnss_files-2.27.so", "libresolv-2.27.so",
    "libutil-2.27.so",
)
LINKS = {
    "ld-linux-aarch64.so.1": "ld-2.27.so", "libc.so.6": "libc-2.27.so",
    "libcrypt.so.1": "libcrypt-2.27.so", "libnss_dns.so.2": "libnss_dns-2.27.so",
    "libnss_files.so.2": "libnss_files-2.27.so", "libresolv.so.2": "libresolv-2.27.so",
    "libutil.so.1": "libutil-2.27.so",
}
# Counts and original hashes recorded from the public upstream binaries used
# in the successful installation. Refuse a different binary, even if compatible.
PATCHES = {
    "sbin/dropbear": ("25ea272775127222a6106aee59e712e1a9b20a70e53a25e612a50dedeae39de4", 3, 3),
    "lib/libc-2.27.so": ("61023ec5dade3909b7e0ef12099282328d4f8ab10ca0fe3c1d20a2f49785d080", 2, 4),
    "lib/ld-2.27.so": ("531b2f139616cd3174f19186cca3f83a28e9012d3e4cda0bea1a1962589de66c", 1, 3),
    "lib/libnss_files-2.27.so": ("e618e1a6481e5f59aabf68012a9df9a1d7f1c602552fe3dc6a123fff608add9a", 0, 4),
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def relocate(name: str, data: bytes) -> bytes:
    expected, lib_count, etc_count = PATCHES[name]
    if sha(data) != expected:
        raise ValueError(f"Unexpected upstream binary: {name}")
    if data.count(b"/opt/lib") != lib_count or data.count(b"/opt/etc") != etc_count:
        raise ValueError(f"Unexpected relocation counts: {name}")
    result = data.replace(b"/opt/lib", b"/var/zsl").replace(b"/opt/etc", b"/var/zse")
    if name == "lib/libc-2.27.so":
        for original, replacement, count in (
            (b"/dev/ptmx", b"/var/ptmx", 1), (b"/dev/pts", b"/var/pts", 2),
        ):
            if result.count(original) != count:
                raise ValueError("Unexpected libc PTY paths")
            result = result.replace(original, replacement)
    if len(result) != len(data):
        raise ValueError("Binary relocation changed file length")
    return result


def fetch(package: dict, base_url: str, cache: Path) -> bytes:
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / package["filename"]
    if path.exists():
        data = path.read_bytes()
    else:
        url = base_url + package["filename"]
        if shutil.which("curl"):
            try:
                result = subprocess.run(
                    ["curl", "--fail", "--location", "--silent", "--show-error",
                     "--connect-timeout", "10", "--max-time", "25", "--retry", "2",
                     "--retry-delay", "1", "--max-filesize", "8388608", url],
                    capture_output=True, timeout=85, check=True)
                data = result.stdout
            except subprocess.SubprocessError as exc:
                raise OSError(f"Download failed: {package['filename']}; "
                              "retry or provide the hash-verified package in --cache-dir") from exc
        else:
            with urllib.request.urlopen(url, timeout=25) as response:
                data = response.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise ValueError("Unexpectedly large package")
        if sha(data) != package["sha256"]:
            raise ValueError(f"Package SHA256 mismatch: {package['filename']}")
        temp = path.with_suffix(".download")
        temp.write_bytes(data)
        temp.replace(path)
    if sha(data) != package["sha256"]:
        raise ValueError(f"Cached package SHA256 mismatch: {package['filename']}")
    return data


def package_files(data: bytes) -> dict[str, bytes]:
    """Read a tiny allowlist, never extract an upstream archive into the filesystem."""
    selected = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as ipk:
        members = [m for m in ipk.getmembers() if m.name.lstrip("./") == "data.tar.gz"]
        if len(members) != 1 or not members[0].isfile():
            raise ValueError("Unsupported IPK container")
        with tarfile.open(fileobj=ipk.extractfile(members[0]), mode="r:gz") as payload:
            for member in payload:
                name = member.name.removeprefix("./")
                target = None
                if name == "opt/sbin/dropbear":
                    target = "sbin/dropbear"
                elif name in {"opt/lib/" + f for f in LIB_FILES}:
                    target = "lib/" + PurePosixPath(name).name
                if target is not None:
                    if not member.isfile() or member.size > 4 * 1024 * 1024 or target in selected:
                        raise ValueError("Unexpected runtime archive member")
                    selected[target] = payload.extractfile(member).read()
    return selected


def write_archive(destination: Path, files: dict[str, bytes], links: dict[str, str]):
    """Deterministic tar.gz without local usernames, timestamps or absolute paths."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                for name, data in sorted(files.items()):
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    info.mode = 0o755 if name.startswith("sbin/") or name.endswith(".sh") else 0o644
                    archive.addfile(info, io.BytesIO(data))
                for name, target in sorted(links.items()):
                    info = tarfile.TarInfo(name)
                    info.type, info.linkname, info.mode = tarfile.SYMTYPE, target, 0o777
                    archive.addfile(info)


def build(destination=Path("build/ssh-runtime.tar.gz"), cache=Path(".cache/entware")) -> Path:
    lock = json.loads((ROOT / "entware.lock.json").read_text())
    files = {}
    for package in lock["packages"]:
        contents = package_files(fetch(package, lock["base_url"], cache))
        if files.keys() & contents.keys():
            raise ValueError("Duplicate files across packages")
        files.update(contents)
    expected = {"sbin/dropbear"} | {"lib/" + f for f in LIB_FILES}
    if set(files) != expected:
        raise ValueError("Incomplete runtime")
    for name in PATCHES:
        files[name] = relocate(name, files[name])
    # Linux little-endian ELF64, AArch64 e_machine=183.
    binary = files["sbin/dropbear"]
    if binary[:6] != b"\x7fELF\x02\x01" or binary[18:20] != b"\xb7\x00":
        raise ValueError("Expected aarch64 ELF64")
    for path in sorted((ROOT / "router").glob("*.sh")):
        files[path.name] = path.read_bytes()
    files["VERSION"] = b"sr7410-ssh-toolkit-1\n"
    links = {"lib/" + name: target for name, target in LINKS.items()}
    links["sbin/dropbearkey"] = "dropbear"
    write_archive(destination, files, links)
    return destination


def main():
    parser = argparse.ArgumentParser(description="构建固定版本、校验 SHA256 的独立 SSH 运行包")
    parser.add_argument("--output", type=Path, default=Path("build/ssh-runtime.tar.gz"))
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/entware"))
    args = parser.parse_args()
    try:
        output = build(args.output.expanduser(), args.cache_dir.expanduser())
    except (OSError, ValueError, tarfile.TarError) as exc:
        parser.exit(1, f"构建失败：{exc}\n")
    print(f"已构建：{output}\nSHA256：{sha(output.read_bytes())}")


if __name__ == "__main__":
    main()
