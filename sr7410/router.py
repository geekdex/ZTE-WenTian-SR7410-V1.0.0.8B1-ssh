"""Host-side SSH installer and maintenance commands. Does not reboot the router."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import getpass
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import ipaddress
import os
from pathlib import Path
import re
import secrets
import shlex
import socket
import subprocess
import tarfile
import tempfile
import threading
import xml.etree.ElementTree as ET

from .boot import make_boot_bundle
from .config import private_write, telnet_row
from .runtime import build, write_archive
from .telnet import RemoteError, Shell

BASE = "/Plugin/sr7410-ssh"
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(n) for n in
                         ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def lan_address(value):
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as exc:
        raise ValueError("Use a numeric LAN IPv4 address") from exc
    if not any(address in net for net in PRIVATE_NETWORKS):
        raise ValueError("Only RFC1918 LAN addresses are supported by this installer")
    return str(address)


def port_number(value):
    number = int(value)
    if not 1 <= number <= 65535:
        raise ValueError("Port must be in 1..65535")
    return number


def credentials(args):
    if args.config:
        values = telnet_row(args.config.read_bytes())
        return values["TS_UName"], values["TS_UPwd"], port_number(values["TS_Port"])
    default_config = Path("private/telnet/config-original.xml")
    if default_config.is_file():
        values = telnet_row(default_config.read_bytes())
        return values["TS_UName"], values["TS_UPwd"], port_number(values["TS_Port"])
    user = args.user or input("Telnet 用户名（原配置 TS_UName）：").strip()
    return user, getpass.getpass("Telnet 密码（输入不回显）："), args.telnet_port


def public_key(path: Path):
    # Strip comments so hostnames/email addresses never get copied unnecessarily.
    parts = path.read_text().strip().split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519" or not re.fullmatch(r"[A-Za-z0-9+/]+=*", parts[1]):
        raise ValueError("Provide an OpenSSH Ed25519 .pub file, not a private key")
    result = subprocess.run(["ssh-keygen", "-l", "-f", str(path)],
                            capture_output=True, text=True)
    if result.returncode:
        raise ValueError("ssh-keygen rejected the public key")
    return (parts[0] + " " + parts[1] + "\n").encode()


def local_address(host):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((host, 23))
        return sock.getsockname()[0]


@contextmanager
def payload_server(payload: bytes, bind_address: str, router_address: str):
    token = "/" + secrets.token_urlsafe(24) + "/payload"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != token or self.client_address[0] != router_address:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = HTTPServer((bind_address, 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://{bind_address}:{server.server_port}{token}"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


def transfer(shell, host: str, bind_address: str, payload: bytes, target: str):
    with payload_server(payload, bind_address, host) as url:
        shell.run("wget -q -O " + shlex.quote(target) + " " + shlex.quote(url), timeout=60)
    # The stock BusyBox has hexdump but lacks sha256sum/base64. Compare every byte
    # on the host before any downloaded archive is unpacked or executed.
    if shell.read_file(target) != payload:
        raise RemoteError("Downloaded payload failed complete byte read-back verification")


@contextmanager
def remote_stage(shell):
    path = "/var/sr7410-setup-" + secrets.token_hex(8)
    shell.run("umask 077; mkdir " + shlex.quote(path))
    try:
        yield path
    finally:
        try:
            shell.run("rm -rf " + shlex.quote(path))
        except (OSError, RemoteError, EOFError):
            pass


def probe(shell):
    arch = shell.run("uname -m").strip()
    if arch != "aarch64":
        raise ValueError("Only the tested aarch64 platform is supported")
    lan = shell.run("ip -4 addr show br0 | awk '/inet / {print $2; exit}' | cut -d/ -f1").strip()
    lan_address(lan)
    user = shell.run("awk -F: '$3 == 0 {print $1; exit}' /etc/passwd").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", user):
        raise ValueError("Cannot identify the system's native UID 0 account")
    shell.run("test -d /Plugin && test -w /Plugin")
    shell.run("(for c in wget hexdump wc cmp tar ip mount awk cut readlink grep tr; "
              "do command -v \"$c\" >/dev/null || exit 1; done)")
    return lan, user


def client_files(directory: Path, host: str, port: int, user: str, identity: Path, host_key: str):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = directory.resolve()
    host_field = host if port == 22 else f"[{host}]:{port}"
    private_write(directory / "known_hosts", f"{host_field} {host_key}\n".encode(), overwrite=True)
    def quote(value):
        value = str(value)
        if any(c in value for c in '\n\r"'):
            raise ValueError("Unsupported SSH config path")
        return '"' + value + '"'
    config = (
        f"Host sr7410\n"
        f"    HostName {host}\n    Port {port}\n    User {user}\n"
        f"    IdentityFile {quote(identity.resolve())}\n"
        f"    IdentitiesOnly yes\n    PasswordAuthentication no\n"
        f"    UserKnownHostsFile {quote(directory / 'known_hosts')}\n"
        f"    StrictHostKeyChecking yes\n    ConnectTimeout 8\n"
    )
    private_write(directory / "ssh_config", config.encode(), overwrite=True)


def install(args, shell, lan, user):
    if not args.public_key:
        candidates = [
            Path.home() / ".ssh" / "sr7410_ed25519.pub",
            Path.home() / ".ssh" / "id_ed25519.pub",
        ]
        for c in candidates:
            if c.is_file():
                args.public_key = c
                break
        if not args.public_key:
            raise ValueError(
                "未指定 SSH 公钥文件！\n\n"
                "【解决办法】：\n"
                "  请通过 --public-key 指定您的 Ed25519 公钥路径，\n"
                "  或者先在本地终端生成密钥对：ssh-keygen -t ed25519 -f ~/.ssh/sr7410_ed25519"
            )
    identity = args.identity or Path(str(args.public_key).removesuffix(".pub"))
    if not identity.is_file():
        raise ValueError(
            f"未找到与公钥匹配的本地私钥文件 ({identity})！\n"
            "请通过 --identity 指定私钥路径（私钥仅保存在本地电脑，绝不上载）。"
        )
    key = public_key(args.public_key)
    key_line = key.decode().strip()

    # 幂等检查：若路由器上已经安装了 SSH
    base_exists = shell.run("if test -d " + BASE + "; then echo yes; else echo no; fi").strip() == "yes"
    if base_exists:
        # 1. 确保公钥写入白名单且不重复
        current_keys = shell.run(f"cat {BASE}/authorized_keys 2>/dev/null || true")
        if key_line not in current_keys:
            shell.run(f"echo {shlex.quote(key_line)} >> {BASE}/authorized_keys")
        # 2. 补齐权限
        shell.run(f"chmod 600 {BASE}/authorized_keys {BASE}/settings.conf 2>/dev/null || true")
        shell.run(f"chmod -R 755 {BASE}/sbin {BASE}/lib 2>/dev/null || true")
        # 3. 检查是否为旧版有日志配置，若有则热升级为零日志模式
        has_old_log = shell.run(
            f"if grep -q 'ssh\\.log' {BASE}/start.sh 2>/dev/null; then echo yes; else echo no; fi"
        ).strip() == "yes"
        if has_old_log:
            router_dir = Path(__file__).resolve().parents[1] / "router"
            scripts_archive = io.BytesIO()
            with tarfile.open(fileobj=scripts_archive, mode="w:gz") as tar:
                for script in ("start.sh", "boot.sh", "stop.sh", "uninstall.sh"):
                    script_path = router_dir / script
                    if script_path.is_file():
                        tar.add(str(script_path), arcname=script)
            with remote_stage(shell) as stage:
                transfer(shell, args.host, args.bind_address or local_address(args.host),
                         scripts_archive.getvalue(), f"{stage}/scripts.tar.gz")
                shell.run(f"tar -xzf {stage}/scripts.tar.gz -C {BASE}/ && chmod 700 {BASE}/*.sh")
            # 清理旧日志并重启 Dropbear
            shell.run(f"{BASE}/stop.sh")
            shell.run("rm -f /var/sr7410-ssh/*.log /var/sr7410-ssh-boot.log")
            shell.run(f"{BASE}/start.sh")
            print("⚡ 检测到路由器运行的是旧版日志配置，已热升级为【零日志模式】，并清理了 RAM 中的历史日志！")
        else:
            # 确保服务运行中
            is_running = shell.run(
                f"if test -f /var/sr7410-ssh/dropbear.pid && kill -0 $(cat /var/sr7410-ssh/dropbear.pid) 2>/dev/null; "
                f"then echo yes; else echo no; fi").strip() == "yes"
            if not is_running:
                shell.run(f"{BASE}/start.sh")
        # 4. 同步本地客户端文件
        key_output = shell.run(BASE + "/sbin/dropbearkey -y -f " + BASE + "/dropbear_ed25519_host_key")
        matches = re.findall(r"(?m)^ssh-ed25519 ([A-Za-z0-9+/]+=*)(?:\s|$)", key_output)
        if len(matches) == 1:
            client_files(args.client_dir, args.host, args.ssh_port, user, identity, "ssh-ed25519 " + matches[0])
        print("✅ SSH 服务已安装并正常运行（幂等检查通过）：")
        print(f"路由器实际 UID 0 用户名: {user}")
        print(f"客户端公钥已同步加入白名单: {args.public_key}")
        print(f"主机公钥已更新固定到 {args.client_dir / 'known_hosts'}。")
        print(f"\n~/.ssh/config 配置参考（可直接追加到 ~/.ssh/config）：")
        print(f"Host sr7410")
        print(f"  HostName {args.host}")
        print(f"  Port {args.ssh_port}")
        print(f"  User {user}")
        print(f"  IdentityFile {identity}")
        print(f"  IdentitiesOnly yes\n")
        print(f"登录测试：ssh -F {shlex.quote(str(args.client_dir / 'ssh_config'))} sr7410")
        return

    try:
        connection = socket.create_connection((args.host, args.ssh_port), timeout=2)
    except (ConnectionRefusedError, TimeoutError, socket.timeout):
        pass
    else:
        connection.close()
        raise ValueError("The selected SSH port is already in use; select --ssh-port or use the existing server")
    prebuilt = Path(__file__).resolve().parents[1] / "packages" / "ssh-runtime.tar.gz"
    with tempfile.TemporaryDirectory(prefix="sr7410-build-") as temp:
        if prebuilt.is_file():
            archive = prebuilt
        else:
            archive = build(Path(temp) / "runtime.tar.gz", args.cache_dir)
        files, links = {}, {}
        with tarfile.open(archive) as source:
            for member in source:
                if member.isfile():
                    files[member.name] = source.extractfile(member).read()
                elif member.issym():
                    links[member.name] = member.linkname
                else:
                    raise ValueError("Unexpected runtime member")
        files["authorized_keys"] = key
        files["settings.conf"] = f"SSH_LAN_IP='{lan}'\nSSH_PORT='{args.ssh_port}'\n".encode()
        payload_archive = Path(temp) / "payload.tar.gz"
        write_archive(payload_archive, files, links)
        with remote_stage(shell) as stage:
            transfer(shell, args.host, args.bind_address or local_address(args.host),
                     payload_archive.read_bytes(), stage + "/payload.tar.gz")
            shell.run("mkdir " + stage + "/payload && tar -xzf " + stage +
                      "/payload.tar.gz -C " + stage + "/payload")
            shell.run("cd " + stage + "/payload && sh install.sh", timeout=150)
    key_output = shell.run(BASE + "/sbin/dropbearkey -y -f " + BASE + "/dropbear_ed25519_host_key")
    matches = re.findall(r"(?m)^ssh-ed25519 ([A-Za-z0-9+/]+=*)(?:\s|$)", key_output)
    if len(matches) != 1:
        raise RemoteError("Cannot read host public key from the trusted Telnet session")
    client_files(args.client_dir, args.host, args.ssh_port, user, identity,
                 "ssh-ed25519 " + matches[0])
    print("已安装 SSH；仅监听 LAN 与回环地址，密码登录已关闭，尚未启用开机自启。")
    print(f"路由器实际 UID 0 用户名: {user}")
    print(f"主机公钥通过当前 Telnet 会话固定到 {args.client_dir / 'known_hosts'}。")
    print(f"\n~/.ssh/config 配置参考（可直接追加到 ~/.ssh/config）：")
    print(f"Host sr7410")
    print(f"  HostName {args.host}")
    print(f"  Port {args.ssh_port}")
    print(f"  User {user}")
    print(f"  IdentityFile {identity}")
    print(f"  IdentitiesOnly yes\n")
    print(f"登录测试：ssh -F {shlex.quote(str(args.client_dir / 'ssh_config'))} sr7410")
    print("下一步：运行 enable-autostart 启用开机自启，然后运行 verify-ssh。")


def enable_boot(args, shell):
    has_base = shell.run(f"if test -f {BASE}/VERSION; then echo yes; else echo no; fi").strip() == "yes"
    if not has_base:
        raise RemoteError(
            f"未在路由器上检测到 SSH 运行时 ({BASE})！\n\n"
            f"【执行阻断原因】：\n"
            f"  开机自启依赖于已安装的 SSH 运行时，但当前系统尚未安装 SSH。\n\n"
            f"【必须先执行前置步骤】：\n"
            f"  python3 manage.py install-ssh --host {args.host}"
        )
    # Check if the existing hook has old logging or background sleep
    has_old_hook = shell.run(
        "if grep -E 'sr7410-ssh-boot\\.log|sleep 1800' /Plugin/dlspeed_rt/dlspeed_monitor.sh 2>/dev/null; "
        "then echo yes; else echo no; fi").strip() == "yes"

    # If already owned and hook is up-to-date, avoid another copy and let the router verify its snapshot.
    owned = shell.run("if test -f " + BASE + "/boot-created.tar.gz; then echo yes; else echo no; fi").strip()
    if owned == "yes" and not has_old_hook:
        out = shell.run(BASE + "/boot.sh enable").strip()
        print(f"✅ 开机自启配置已就绪（幂等检查通过）：{out}")
        return

    # 终止正在运行的旧版 trimmer 循环进程，清理开机日志
    shell.run("kill $(pgrep -f 'sleep 1800') 2>/dev/null || true")
    shell.run("rm -f /var/sr7410-ssh-boot.log")
    shell.run("rm -f " + BASE + "/boot-created.tar.gz /Plugin/apps/dlspeed_rt.tar.gz")
    stock = shell.read_file("/etc/dlspeed_rt.tar.gz")
    override = make_boot_bundle(stock)
    with remote_stage(shell) as stage:
        for name, data in (("stock.tar.gz", stock), ("override.tar.gz", override)):
            transfer(shell, args.host, args.bind_address or local_address(args.host),
                     data, stage + "/" + name)
        print(shell.run(BASE + "/boot.sh enable " + stage).strip())
    print("⚡ 自启 Hook 已升级为【零日志模式】，后台旧轮询进程与开机日志已清除。")
    print("请自行在管理后台重启，再运行 verify-ssh 验证重启后的命令与交互登录。")


def verify(args):
    config = args.client_dir / "ssh_config"
    if not config.is_file():
        raise ValueError(
            f"未找到本地 SSH 配置文件 ({config})！\n\n"
            f"【执行阻断原因】：\n"
            f"  尚未针对此设备执行过 SSH 安装或生成客户端密钥。\n\n"
            f"【必须先执行前置步骤】：\n"
            f"  python3 manage.py install-ssh --host <路由器IP>"
        )
    common = ["ssh", "-F", str(config), "-o", "BatchMode=yes"]
    command = subprocess.run(common + ["sr7410", "id -u"], capture_output=True, text=True, timeout=20)
    if command.returncode or command.stdout.strip() != "0":
        raise RemoteError(
            "SSH 连接验证失败！\n\n"
            "【可能原因与解决办法】：\n"
            "  1. 路由器可能已被恢复出厂设置，原有 SSH 服务被清除。\n"
            "     请必须按顺序重新执行：unlock-telnet -> install-ssh -> enable-autostart。\n"
            "  2. 客户端私钥不匹配或尚未解锁；\n"
            "  3. 可使用交互式详细排查命令：\n"
            f"     ssh -F {config} -v sr7410"
        )
    # Keep stdin open while the remote command runs. An immediate EOF on a
    # forced PTY can hang up the router shell before it produces any output.
    read_end, write_end = os.pipe()
    try:
        pty = subprocess.run(
            common + ["-tt", "sr7410", "test -t 0 && readlink /proc/$$/fd/0 && id -u"],
            stdin=read_end, capture_output=True, text=True, timeout=20)
    finally:
        os.close(read_end)
        os.close(write_end)
    lines = pty.stdout.replace("\r", "").splitlines()
    if pty.returncode or not any(line.startswith("/var/pts/") for line in lines) or "0" not in lines:
        raise RemoteError("SSH PTY verification failed; test connection with ssh -F private/ssh/ssh_config -v sr7410")
    print("通过：SSH 公钥认证、uid=0 命令执行、/var/pts 交互终端。")


def main():
    parser = argparse.ArgumentParser(description="SR7410 SSH 安装、校验与维护（不自动重启）")
    parser.add_argument("action", choices=("doctor", "install-ssh", "verify-ssh",
                                         "enable-autostart", "disable-autostart", "status", "uninstall-ssh"))
    parser.add_argument("--host", default="192.168.5.1")
    parser.add_argument("--config", type=Path, help="本地私有 XML，自动读取 Telnet 账号与密码")
    parser.add_argument("--user", help="不用 --config 时使用的 Telnet 用户名")
    parser.add_argument("--telnet-port", type=int, default=23)
    parser.add_argument("--ssh-port", type=int, default=22)
    parser.add_argument("--public-key", type=Path, help="电脑上的 Ed25519 公钥 .pub 文件")
    parser.add_argument("--identity", type=Path, help="电脑上的匹配私钥路径；私钥永不上传")
    parser.add_argument("--client-dir", type=Path, default=Path("private/ssh"))
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/entware"))
    parser.add_argument("--bind-address", help="电脑用于临时 HTTP 传输的 LAN 地址，通常自动检测")
    args = parser.parse_args()
    for name in ("config", "public_key", "identity", "client_dir", "cache_dir"):
        value = getattr(args, name)
        if value is not None:
            setattr(args, name, value.expanduser())
    try:
        args.host = lan_address(args.host)
        args.ssh_port, args.telnet_port = port_number(args.ssh_port), port_number(args.telnet_port)
        if args.bind_address:
            args.bind_address = lan_address(args.bind_address)
        if args.action == "verify-ssh":
            verify(args)
            return
        user, password, telnet_port = credentials(args)
        with Shell(args.host, telnet_port, user, password) as shell:
            lan, native_user = probe(shell)
            if args.action == "doctor":
                print(f"只读检查通过：aarch64，br0 地址 {lan}，UID 0 账号已识别。")
                print(shell.run("df -k /Plugin").strip())
            elif args.action == "install-ssh":
                install(args, shell, lan, native_user)
            elif args.action == "enable-autostart":
                enable_boot(args, shell)
            elif args.action == "disable-autostart":
                print(shell.run(BASE + "/boot.sh disable").strip())
            elif args.action == "status":
                print(shell.run(BASE + "/boot.sh status").strip())
                print(shell.run("if test -f /var/sr7410-ssh/dropbear.pid; then "
                                "kill -0 \"$(cat /var/sr7410-ssh/dropbear.pid)\" && echo 'SSH running'; "
                                "else echo 'SSH stopped'; fi").strip())
            elif args.action == "uninstall-ssh":
                print(shell.run(BASE + "/uninstall.sh", timeout=30).strip())
    except (OSError, ValueError, RemoteError, EOFError, ET.ParseError,
            tarfile.TarError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"停止：{exc}\n")


if __name__ == "__main__":
    main()
