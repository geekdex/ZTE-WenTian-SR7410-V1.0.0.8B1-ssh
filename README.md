# ZTE WenTian BE7200 Pro+ (ZXSLC SR7410) SSH 开启与持久化指南

> 适用固件版本：`V1.0.0.8B1.8000`（主控 SoC: 中兴微电子 Sanechips `ZX279133` AArch64）  
> 特性：全离线运行包、零云端依赖、防变砖安全校验、开机静默自启、非 root 用户名修复、自适应自定义内网网段。

---

> [!WARNING]
>
> ### ⚠️ 免责声明 (Disclaimer)
>
> 1. **适用范围与目的**：本项目包含的脚本、工具、文档及离线运行时**仅限用于安全研究、漏洞原理探讨及个人自用 DIY**，严禁用于任何商业牟利、非法渗透、未经授权的攻击或危害公共网络安全的行为。
> 2. **责任完全自负**：获取路由器底层权限（包括解密配置、开启 Telnet、持久化部署 SSH 及开机自启 Hook）属于高级系统定制与调试操作，可能存在导致设备配置丢失、网络暂时中断、硬件故障或影响厂商售后保修的潜在风险。**使用者须自行承担操作带来的全部风险与责任**，作者不对因使用或误用本项目所引起的任何直接或间接后果承担连带法律或经济责任。
> 3. **备份与无担保**：在执行任何步骤前，请务必妥善备份你的路由器原厂完整配置文件。作者已在目标硬件与对应固件环境下进行了严格的幂等性与安全测试，但不对任何环境差异下的可用性作绝对担保。
> 4. **商标与版权说明**：文档中涉及的中兴（ZTE）、Sanechips 等品牌名称及硬件型号商标权归各自合法所有者所有；项目中整合的第三方开源软件包（如 Dropbear、Entware Glibc 等）遵循其各自的原作者开源许可协议。

---

## 1. 核心原理与设计关键点

### 1.1 Telnet 激活与账号密码来源

- **配置加密结构**：中兴路由器的备份文件 `config.bin` 使用标准的 Type 4 格式加密（AES-256-CBC + zlib 分块压缩）。
  - 密钥（出厂固定）：Key = `SR7410Key80748010`，IV = `SR7410Iv80748010`。
- **Telnet 密码提取**：解密出的 XML 表中包含 `<Tbl name="TelnetCfg">`。原厂系统出厂内置的 Telnet 账号密码为：
  - **用户名 (TS_UName)**：`root`
  - **密　码 (TS_UPwd)**：`abc123!@#`
  - **端口号 (TS_Port)**：`23`
- **开启逻辑与自适应内网 IP**：
  - 脚本将 `TS_Enable` 与 `Lan_Enable` 置为 `1`，保持 `Wan_Enable="0"`（仅限局域网访问，避免公网风险）。
  - **关于路由器 IP**：中兴固件在 `Lan_Enable="1"` 时，会自动将 Telnet 服务绑定到路由器的内网网桥网卡（`br0`）。无论是原厂默认的 `192.168.0.1`，还是用户在后台自定义修改过的 `192.168.1.1`、`192.168.50.1` 或 `10.0.0.1` 等，Telnet 都会自动监听在**当前路由器的内网网关 IP** 上。
  - `manage.py unlock-telnet` 解密配置时会自动从 XML 中识别出你的路由器内网 IP，并提示后续对应的操作地址。

### 1.2 独立 Dropbear 运行时重定位

- 路由器原生 libc 严重裁剪，缺少 PTY 支持。
- 工具预先提取并锁定了 Entware AArch64 编译的 **Dropbear 2025.89** 及 **Glibc 2.27** 系列库文件，保存在 `packages/` 目录下。
- 通过二进制静态字串替换完成独立重定位（不改变文件长度）：
  - `/opt/lib` $\rightarrow$ `/var/zsl`（软链接至 `/Plugin/sr7410-ssh/lib`）
  - `/opt/etc` $\rightarrow$ `/var/zse`（软链接至 `/etc`）
  - `/dev/ptmx` $\rightarrow$ `/var/ptmx`，`/dev/pts` $\rightarrow$ `/var/pts`（自动 mount 新的 devpts 实例，彻底解决 PTY 交互终端分配失败报错）
- 整个 SSH 运行时完全隔离在可写闪存 `/Plugin/sr7410-ssh`（挂载于 `/dev/mtdblock12` JFFS2 分区），不修改固件只读分区。

### 1.3 开机自启动机制 (V1.0.0.8B1.8000 专有排坑)

- 固件在开机时通过 `/bin/dlspeedplugind` 守护进程拉起后台监控：
  `/bin/sh /dlspeedPlugin/dlspeed_rt/dlspeed_monitor.sh &`（PID 1020 左右）。
- 固件启动链 `/etc/dlspeed_tar.sh` 优先检查覆盖包 `/apps/apps/dlspeed_rt.tar.gz`（即 `/Plugin/apps/dlspeed_rt.tar.gz`）。
- 该版本原版 `dlspeed_rt.tar.gz` 的校验哈希为：
  `4b70059ff49906ac02b47ce1901b7efced216ef80ab383d93a26f78a55a2ee8d`
- **关键排坑**：`V1.0.0.8B1.8000` 版本的 `/etc/dlspeed_tar.sh` 增加了 `if [ $appFileDir ]; then echo "exists"; else tar ...; fi` 逻辑。如果闪存中的 `/Plugin/dlspeed_rt` 目录早已存在，开机不会重新解压覆盖包！
  - **解决手段**：`enable-autostart` 不仅在 `/Plugin/apps/` 下放置打好补丁的压缩包，还**直接将打了自启 Hook 的 `dlspeed_monitor.sh` 解包写入闪存 `/Plugin/dlspeed_rt/`**。
  - 路由器每次开机，PID 1020 会立即执行注入的 Hook，检测 `/Plugin/sr7410-ssh/autostart.enabled` 存在后在后台唤醒 Dropbear。

### 1.4 UID 0 超级用户名的真相：`Z500_w9bZ`

- 系统 `/etc/passwd` 中 UID 为 0 的超级管理员用户名为 **`Z500_w9bZ`**（共 9 位，末尾含大写字母 Z）。
- **截断陷阱**：BusyBox 的 `ls -l` 在格式化输出属主时，**默认只截取前 8 位字符**，显示为 `Z500_w9b`。如果客户端配置写成 `User Z500_w9b`，Dropbear 会在日志中报 `Login attempt for nonexistent user` 并拒绝公钥认证。
- SSH 客户端必须使用真正的全名：**`User Z500_w9bZ`**。

### 1.5 全流程幂等性保障 (Idempotent Design)

- **重复执行完全安全**：即使路由器当前已经正常运行 SSH，或者你已经配置完成，再次执行整个部署流程或单个指令进行可行性验证，系统均会自动识别现有状态并平滑同步，不会报错退出，也不会损坏现有服务：
  - `unlock-telnet`：允许重复生成并覆写待导入文件，不会报目录或文件已存在错误。
  - `install-ssh`：自动检测已存在的 `/Plugin/sr7410-ssh` 及运行中的 Dropbear 进程，自动补齐缺失权限、校验并同步客户端公钥至 `authorized_keys`（绝不重复添加多余公钥行），并更新本地客户端配置。
  - `enable-autostart`：若已配置自启，会自动校验闪存中的 Hook 脚本完整性并快速确认就绪，避免重复覆盖原厂备份。

### 1.6 零日志与零常驻开销设计 (Zero-Log & Zero-Overhead)

- **0 字节磁盘/内存占用**：为了支持路由器长达 1 年甚至数年不重启的稳定运行，所有守护进程输出均通过 `>/dev/null 2>&1` 丢弃，**不在闪存或 RAM 中产生任何追加日志文件**。
- **不污染系统 Syslog**：Dropbear 使用 `-E` 将日志直接导向 stderr 并丢弃至 `/dev/null`，避免了向系统日志（如 `dmesg` 或 `syslog`）写入冗余的连接与心跳信息。
- **0 后台监控开销**：不启动任何轮询休眠进程（无 `while true; sleep` 脚本常驻），自启脚本单次执行启动后立即退出。无 SSH 连接时 CPU 占用严格为 0%。
- **临时故障排查**：若平时遇到连接异常，无需查日志，直接使用 Telnet 进入路由器运行前台测试命令即可实时打印诊断信息：
  ```sh
  /Plugin/sr7410-ssh/sbin/dropbear -F -E -s -D /Plugin/sr7410-ssh -p 22
  ```

---

## 2. 文件分布与目标映射表

### 2.1 本地离线包目录 (`packages/`)

本地已经完整固化所有依赖，无需访问外网：

| 文件名                                | 校验 / 说明                                         |
| :------------------------------------ | :-------------------------------------------------- |
| `dropbear_2025.89-1_aarch64-3.10.ipk` | 官方 Entware 归档包 (115 KB)                        |
| `libc_2.27-12_aarch64-3.10.ipk`       | 包含 ld 与 libc (1.4 MB)                            |
| `libgcc_8.4.0-12_aarch64-3.10.ipk`    | GCC 运行库 (36 KB)                                  |
| `libpthread_2.27-12_aarch64-3.10.ipk` | 线程库 (44 KB)                                      |
| `librt_2.27-12_aarch64-3.10.ipk`      | 实时库 (14 KB)                                      |
| `libssp_8.4.0-12_aarch64-3.10.ipk`    | 栈保护库 (4.2 KB)                                   |
| **`ssh-runtime.tar.gz`**              | **已完成重定位并打包好的纯净二进制运行时 (964 KB)** |

### 2.2 路由器写入路径与权限清单

| 目标路径 (路由器内)                            | 权限 (Mode) | 属主             | 作用与功能说明                                              |
| :--------------------------------------------- | :---------- | :--------------- | :---------------------------------------------------------- |
| `/Plugin/sr7410-ssh/`                          | `0700`      | `Z500_w9bZ:root` | SSH 主运行目录（存放于 JFFS2 持久闪存）                     |
| `/Plugin/sr7410-ssh/sbin/dropbear`             | `0755`      | `Z500_w9bZ:root` | 重定位后的 Dropbear 服务器主程序                            |
| `/Plugin/sr7410-ssh/lib/`                      | `0755`      | `Z500_w9bZ:root` | 运行时依赖的 Glibc 动态链接库                               |
| `/Plugin/sr7410-ssh/authorized_keys`           | `0600`      | `Z500_w9bZ:root` | 允许登录的客户端 SSH 公钥列表                               |
| `/Plugin/sr7410-ssh/settings.conf`             | `0600`      | `Z500_w9bZ:root` | 监听配置（`SSH_LAN_IP` 自动侦测绑定网桥 IP，`SSH_PORT=22`） |
| `/Plugin/sr7410-ssh/dropbear_ed25519_host_key` | `0600`      | `Z500_w9bZ:root` | 路由器本地生成的主机私钥                                    |
| `/Plugin/sr7410-ssh/autostart.enabled`         | `0600`      | `Z500_w9bZ:root` | 开机自启生效开关标志                                        |
| `/Plugin/apps/dlspeed_rt.tar.gz`               | `0600`      | `Z500_w9bZ:root` | 打入开机自启 Hook 的测速组件覆盖包                          |
| `/Plugin/dlspeed_rt/dlspeed_monitor.sh`        | `0755`      | `Z500_w9bZ:root` | 闪存中当前运行的开机自启脚本                                |
| `/var/sr7410-ssh/dropbear.pid`                 | `0600`      | `Z500_w9bZ:root` | Dropbear 运行进程 PID 标识（仅在内存 RAM 中）               |

---

## 3. 完整操作流程（路由器重置后一键恢复）

假设路由器恢复出厂设置或重新升级固件，只需以下 5 步即可完全恢复：

### 步骤 0：准备工作与环境依赖

本工具推荐使用 Python 3.9+，安装依赖：

```bash
pip install -r requirements.txt
```

_(主要包含 `zcu`, `pycryptodomex`, `telnetlib3`)_

---

### 步骤 1：导出原版配置并一键解锁 Telnet

1. 浏览器打开路由器管理后台（例如 `http://192.168.0.1`，若你修改过后台 IP 则打开对应地址如 `http://192.168.1.1`）。
2. 依次点击：**【管理】 $\rightarrow$ 【配置管理】 $\rightarrow$ 【备份配置】**，下载保存为 `config.bin`。
3. 在本工具目录下执行：
   ```bash
   python3 manage.py unlock-telnet config.bin
   ```
   **输出结果示例**：
   ```text
   ✅ Telnet 配置解锁成功！
   路由器内网 IP   : 192.168.0.1 (从配置中自动识别)
   Telnet 端口号   : 23
   Telnet 用户名   : root
   Telnet 密码     : abc123!@#
   待导入加密配置  : private/telnet/config-telnet-lan-only.bin
   ```
   _(注：如果你的内网网段是 192.168.1.x 或其它子网，脚本会自动显示你的实际内网 IP)_

---

### 步骤 2：恢复配置激活 Telnet

1. 回到路由器管理后台：**【管理】 $\rightarrow$ 【配置管理】 $\rightarrow$ 【恢复配置】**。
2. 上传刚刚生成的 `private/telnet/config-telnet-lan-only.bin`。
3. 点击“恢复配置”，等待路由器自动重启（约 1 分钟）。
4. 重启完成后，Telnet（23端口）已在你的内网网段永久激活！

---

### 步骤 3：一键部署 Dropbear SSH

确认你的本地电脑已有 SSH 密钥（如果没有，执行 `ssh-keygen -t ed25519 -f ~/.ssh/sr7410_ed25519` 生成）。

执行一键安装（使用本地已打好的运行时，完全无需联网）：

```bash
python3 manage.py install-ssh \
  --host <路由器_LAN_IP> \
  --config private/telnet/config-original.xml \
  --public-key ~/.ssh/sr7410_ed25519.pub
```

> _(注：`<路由器_LAN_IP>` 替换为你路由器的实际内网 IP，默认通常为 `192.168.0.1`；脚本会自动识别 UID 0 用户名 `Z500_w9bZ`，安全传输运行时并立即拉起 Dropbear 服务)_

---

### 步骤 4：启用开机自启

```bash
python3 manage.py enable-autostart \
  --host <路由器_LAN_IP> \
  --config private/telnet/config-original.xml
```

_提示 `Autostart enabled; reboot separately when ready`，说明自启钩子已成功固化到持久闪存。_

---

### 步骤 5：配置客户端 SSH 登录

在电脑或需要登录路由器的其他内网设备（如 Armbian）上，配置 `~/.ssh/config`：

```ssh-config
Host sr7410
  HostName <路由器_LAN_IP>
  Port 22
  User Z500_w9bZ
  IdentityFile ~/.ssh/sr7410_ed25519
  IdentitiesOnly yes
  PasswordAuthentication no
  ConnectTimeout 8
```

_(将 `<路由器_LAN_IP>` 改为你的路由器 IP，如 `192.168.0.1` 或 `192.168.1.1`)_

确保私钥权限为 `600`：

```bash
chmod 600 ~/.ssh/sr7410_ed25519
```

直接测试登录：

```bash
ssh sr7410
```

秒进路由器超级管理员终端！重启路由器后直接敲 `ssh sr7410` 也依然秒连！

---

## 4. 常用维护与排查指令

- **运行自检**：
  ```bash
  python3 manage.py verify-ssh
  ```
- **查看路由器当前 SSH 状态**：
  ```bash
  python3 manage.py status --host <路由器_LAN_IP> --config private/telnet/config-original.xml
  ```
- **手动 Telnet 备用登录**：
  ```bash
  telnet <路由器_LAN_IP>
  # 用户名: root
  # 密  码: abc123!@#
  ```
- **前台测试与实时排错（如若需要）**：
  ```sh
  # 平时零日志运行；若遇异常可在 Telnet 中前台运行排查：
  /Plugin/sr7410-ssh/sbin/dropbear -F -E -s -D /Plugin/sr7410-ssh -p 22
  ```
- **如需彻底卸载 SSH**：
  ```bash
  python3 manage.py uninstall-ssh --host <路由器_LAN_IP> --config private/telnet/config-original.xml
  ```
