"""Strict, offline SR7410 type-4 configuration preparation."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import struct
import xml.etree.ElementTree as ET
import zlib

import zcu.compression
from zcu.xcryptors import CBCXcryptor

KEY = "SR7410Key80748010"  # Public model parameter, NOT a user password.
IV = "SR7410Iv80748010"
MAGIC = 0x01020304
MAX_CONFIG = 16 * 1024 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def decode(binary: bytes) -> tuple[bytes, int]:
    """Accept only the exact type-4/no-outer-header format tested here."""
    require(72 <= len(binary) <= MAX_CONFIG, "Unsupported configuration size")
    header = struct.unpack(">15I", binary[:60])
    require(header == (MAGIC, 4) + (0,) * 13,
            "Unsupported outer format: expected SR7410 type 4 without extra header")
    plain_size, encrypted_size, more = struct.unpack(">3I", binary[60:72])
    require(more == 0 and encrypted_size == len(binary) - 72 and
            encrypted_size % 16 == 0 and 60 <= plain_size <= encrypted_size and
            encrypted_size - plain_size < 16, "Invalid AES payload lengths")
    decoder = CBCXcryptor()
    decoder.set_key(KEY, IV)
    compressed = decoder.decrypt(io.BytesIO(binary[60:])).read()
    require(len(compressed) == plain_size, "Invalid decrypted length")
    inner = struct.unpack(">15I", compressed[:60])
    require(inner[0:2] == (MAGIC, 0) and inner[7:] == (0,) * 8,
            "Unsupported compression format")
    require(0 < inner[2] <= MAX_CONFIG and inner[4] == 65536,
            "Unsupported XML size or compression chunk size")
    require(inner[6] == zlib.crc32(compressed[:24]) & 0xFFFFFFFF,
            "Compression header CRC mismatch")
    cursor, total, crc = 60, bytearray(), 0
    while True:
        require(cursor + 12 <= len(compressed), "Missing compression block")
        size, packed_size, next_block = struct.unpack(">3I", compressed[cursor:cursor+12])
        cursor += 12
        require(0 < size <= inner[4] and 0 < packed_size <= len(compressed) - cursor,
                "Invalid compression block size")
        packed = compressed[cursor:cursor+packed_size]
        cursor += packed_size
        inflater = zlib.decompressobj()
        chunk = inflater.decompress(packed, size + 1)
        require(len(chunk) == size and inflater.eof and not inflater.unused_data and
                not inflater.unconsumed_tail, "Invalid compressed block")
        total.extend(chunk)
        require(len(total) <= MAX_CONFIG, "Decompressed configuration is too large")
        crc = zlib.crc32(packed, crc)
        if not next_block:
            require(inner[3] == cursor - packed_size - 12,
                    "Compression cumulative length mismatch")
            break
        require(next_block == cursor, "Compression block offset mismatch")
    require(cursor == len(compressed) and len(total) == inner[2] and
            crc & 0xFFFFFFFF == inner[5], "Compression data CRC/length mismatch")
    xml = bytes(total)
    require(b"<!DOCTYPE" not in xml.upper() and b"<!ENTITY" not in xml.upper(),
            "XML entities/DTD are not supported")
    ET.fromstring(xml)
    return xml, inner[4]


def encode(xml: bytes, chunk_size: int = 65536) -> bytes:
    compressed = zcu.compression.compress(io.BytesIO(xml), chunk_size=chunk_size)
    encoder = CBCXcryptor(payload_type=4)
    encoder.set_key(KEY, IV)
    return encoder.encrypt(compressed).read()


def telnet_row(xml: bytes):
    tables = [t for t in ET.fromstring(xml).iter("Tbl") if t.get("name") == "TelnetCfg"]
    require(len(tables) == 1, "Expected exactly one TelnetCfg table")
    rows = tables[0].findall("Row")
    require(len(rows) == 1 and rows[0].get("No") == "0",
            "Expected exactly one TelnetCfg Row 0")
    fields = rows[0].findall("DM")
    require(len({f.get("name") for f in fields}) == len(fields),
            "Duplicate TelnetCfg fields")
    values = {f.get("name"): f.get("val") for f in fields}
    require(all(k in values for k in ("TS_Enable", "Lan_Enable", "Wan_Enable",
                                     "TS_UName", "TS_UPwd", "TS_Port")),
            "Required TelnetCfg fields missing")
    return values


def patch_telnet(xml: bytes) -> tuple[bytes, list[dict]]:
    fields = telnet_row(xml)
    require(fields["Wan_Enable"] == "0", "WAN Telnet must already be disabled; refusing")
    require(fields["TS_Enable"] in ("0", "1") and fields["Lan_Enable"] in ("0", "1"),
            "Unsupported Telnet switch values")
    tables = list(re.finditer(
        rb'<Tbl\b(?=[^>]*\bname="TelnetCfg")[^>]*>.*?</Tbl>', xml, re.S))
    require(len(tables) == 1, "Unsupported XML layout")
    match = tables[0]
    table, changes = match.group(), []
    for field in ("TS_Enable", "Lan_Enable"):
        pattern = rb'(<DM\b(?=[^>]*\bname="' + field.encode() + rb'")[^>]*\bval=")([01])(")'
        hits = list(re.finditer(pattern, table))
        require(len(hits) == 1, "Unsupported switch XML layout")
        if fields[field] == "0":
            table = re.sub(pattern, rb"\g<1>1\3", table)
            changes.append({"table": "TelnetCfg", "row": "0", "field": field,
                            "before": "0", "after": "1"})
    modified = xml[:match.start()] + table + xml[match.end():]
    expected = dict(fields, TS_Enable="1", Lan_Enable="1")
    require(telnet_row(modified) == expected, "Unexpected Telnet field changes")
    # Byte-preserving edits above leave everything outside these value bytes intact.
    return modified, changes


def private_write(path: Path, data: bytes, overwrite: bool = True):
    """Write private file with mode 0600. Idempotent when overwrite=True."""
    flags = os.O_CREAT | (os.O_TRUNC if overwrite else os.O_EXCL) | os.O_WRONLY
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def prepare(source: Path, destination: Path) -> dict:
    binary = source.read_bytes()
    xml, chunk_size = decode(binary)
    require(encode(xml, chunk_size) == binary,
            "Original encode/decode round trip differs: STOP, do not import")
    modified, changes = patch_telnet(xml)
    output = encode(modified, chunk_size)
    require(decode(output)[0] == modified, "Modified configuration round trip failed")
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(destination, 0o700)
    for name, data in (
        ("config-original.bin", binary), ("config-original.xml", xml),
        ("config-telnet-lan-only.xml", modified), ("config-telnet-lan-only.bin", output),
    ):
        private_write(destination / name, data, overwrite=True)
    report = {
        "profile": "ZXSLC SR7410 / V1.0.0.7B4.8000",
        "changes": changes, "wan_telnet_enabled": False,
        "credentials_preserved": True, "original_roundtrip_identical": True,
        "modified_roundtrip_identical": True,
        "original_sha256": hashlib.sha256(binary).hexdigest(),
        "modified_sha256": hashlib.sha256(output).hexdigest(),
    }
    private_write(destination / "report.json",
                  (json.dumps(report, indent=2, ensure_ascii=False) + "\n").encode(),
                  overwrite=True)
    return report


def main():
    parser = argparse.ArgumentParser(description="离线生成并校验 SR7410 LAN Telnet 配置")
    parser.add_argument("config", type=Path, help="从自己的路由器导出的 config.bin")
    parser.add_argument("--output-dir", type=Path, default=Path("private/telnet"),
                        help="必须是尚不存在的新目录，默认 private/telnet")
    args = parser.parse_args()
    args.config = args.config.expanduser()
    args.output_dir = args.output_dir.expanduser()
    os.umask(0o077)
    try:
        report = prepare(args.config, args.output_dir)
    except (OSError, ValueError, ET.ParseError, struct.error, zlib.error) as exc:
        parser.exit(1, f"停止：{exc}\n")
    print(f"校验通过；修改了 {len(report['changes'])} 个开关。")
    print(f"待导入文件：{args.output_dir / 'config-telnet-lan-only.bin'}")
    print("WAN Telnet 保持关闭，账号密码保持原值；不会连接或修改路由器。")


if __name__ == "__main__":
    main()
