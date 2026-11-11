#!/usr/bin/env python3
"""SR7410 (V1.0.0.8B1.8000) 专有一键式 SSH 管理工具

支持全离线运行：
1. unlock-telnet   : 解密 config.bin，开启 LAN Telnet 并提取密码
2. install-ssh     : 部署 Dropbear SSH 到 /Plugin/sr7410-ssh
3. enable-autostart: 注入开机自启钩子并固化到闪存
4. verify-ssh      : 验证 SSH 命令与 PTY 终端连通性
5. build-runtime   : 离线从 packages/*.ipk 重建运行包
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

# 引入本目录下的 sr7410 模块
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sr7410.config import prepare, telnet_row
from sr7410.router import main as router_main
from sr7410.runtime import build as runtime_build


def extract_lan_ip(xml_bytes: bytes) -> str:
    try:
        root = ET.fromstring(xml_bytes)
        tbl = root.find('.//Tbl[@name="DHCP4sPool"]')
        if tbl is not None:
            for dm in tbl.findall('.//DM'):
                if dm.attrib.get('name') == 'IPRouters':
                    val = dm.attrib.get('val', '').strip()
                    if val:
                        return val
    except Exception:
        pass
    return "192.168.0.1"


def cmd_unlock_telnet(args):
    source = Path(args.config).expanduser()
    output_dir = Path(args.output_dir).expanduser()
    if not source.is_file():
        sys.exit(f"错误：配置文件不存在: {source}")
    os.umask(0o077)
    report = prepare(source, output_dir)
    xml_data = (output_dir / "config-original.xml").read_bytes()
    vals = telnet_row(xml_data)
    lan_ip = extract_lan_ip(xml_data)
    
    print("\n" + "=" * 50)
    print("✅ Telnet 配置解锁成功！")
    print(f"路由器内网 IP   : {lan_ip} (从配置中自动识别)")
    print(f"Telnet 端口号   : {vals.get('TS_Port', '23')}")
    print(f"Telnet 用户名   : {vals.get('TS_UName', 'root')}")
    print(f"Telnet 密码     : {vals.get('TS_UPwd', 'abc123!@#')}")
    print(f"待导入加密配置  : {output_dir / 'config-telnet-lan-only.bin'}")
    print("=" * 50)
    print("\n【下一步操作】：")
    print(f"1. 登录路由器管理后台 (http://{lan_ip})")
    print("2. 进入【管理】->【配置管理】->【恢复配置】")
    print(f"3. 选择上传刚刚生成的: {output_dir / 'config-telnet-lan-only.bin'}")
    print(f"4. 等待路由器重启完毕后，执行:")
    print(f"   python3 manage.py install-ssh --host {lan_ip}\n")


def cmd_build_runtime(args):
    dest = Path(args.output).expanduser()
    cache = Path(args.cache_dir).expanduser()
    print(f"正在从 {cache} 构建独立运行时...")
    out = runtime_build(dest, cache)
    print(f"✅ 构建成功: {out} ({out.stat().st_size} 字节)")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "unlock-telnet":
        parser = argparse.ArgumentParser(description="解密并修改 SR7410 备份配置，开启 LAN Telnet")
        parser.add_argument("action", choices=["unlock-telnet"])
        parser.add_argument("config", help="从路由器后台导出的 config.bin")
        parser.add_argument("--output-dir", default="private/telnet", help="输出目录 (默认 private/telnet)")
        args = parser.parse_args()
        cmd_unlock_telnet(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "build-runtime":
        parser = argparse.ArgumentParser(description="离线从 packages/*.ipk 构建运行时")
        parser.add_argument("action", choices=["build-runtime"])
        parser.add_argument("--output", default="packages/ssh-runtime.tar.gz", help="目标输出包路径")
        parser.add_argument("--cache-dir", default="packages", help="存放 ipk 的目录")
        args = parser.parse_args()
        cmd_build_runtime(args)
    else:
        # 其他指令交由 router_main 处理 (install-ssh, enable-autostart, verify-ssh, status, uninstall-ssh)
        # 默认使用 packages 目录作为缓存与运行时源
        if "--cache-dir" not in sys.argv:
            sys.argv.extend(["--cache-dir", "packages"])
        router_main()


if __name__ == "__main__":
    main()
