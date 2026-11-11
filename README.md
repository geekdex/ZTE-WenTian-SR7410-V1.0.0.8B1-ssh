# ZTE Wentian BE7200 Pro+ (ZXSLC SR7410) SSH 持久化工具包

> **适用固件**：`V1.0.0.8B1.8000`（SoC: Sanechips `ZX279133` AArch64）  
> **核心特性**：全离线运行时（免联网）、零日志无磨损、全流程幂等、防跳步阻断、开机静默自启。

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

## 1. 关键原理与设计要点

- **Telnet 解锁**：`config.bin` 使用标准 Type 4 格式（AES-256-CBC）加密，出厂内置账号 `root`，密码 `abc123!@#`。将 `TS_Enable` 与 `Lan_Enable` 置 1 即可开启内网 Telnet（自动自适应当前网关 IP）。
- **独立运行时**：原厂 libc 裁剪严重且缺少 PTY 支持。工具内置预编译重定位的 **Dropbear 2025.89 + Glibc 2.27** 独立运行时至 `/Plugin/sr7410-ssh`（JFFS2 持久闪存分区），挂载独立 devpts，不碰只读根分区。
- **开机自启**：利用 `/Plugin/dlspeed_rt/dlspeed_monitor.sh` 守护注入 Hook，开机由系统守护进程拉起 Dropbear。
- **UID 0 超级用户**：系统实际最高权限账号名为 **`Z500_w9bZ`**（末尾含大写 Z，共 9 位；BusyBox `ls -l` 默认只截取显示前 8 位导致常被误写为 `Z500_w9b`）。
- **零日志设计**：Dropbear 使用 `-E` 将日志定向至 stderr 并由外部直接丢弃到 `/dev/null`，不生成磁盘/RAM 日志文件，无任何后台轮询休眠脚本，**长期运行 0 内存增长、0% CPU 空转**。
- **防呆与幂等**：支持任意重复执行，配置与公钥自动增量同步；未解锁 Telnet 或未安装前置依赖时自动阻断并提示。

---

## 2. 离线文件与写入路径

### 2.1 预置离线包 (`packages/`)

- `dropbear_2025.89-1_aarch64-3.10.ipk` / `libc_2.27-12_aarch64-3.10.ipk` 等共 6 个官方归档包
- `ssh-runtime.tar.gz`：已预先重定位完成的纯净二进制独立运行时（~943 KB，免编译免联网）

### 2.2 路由器写入清单

| 目标路径 (路由器内)                     |  权限  | 说明                                                   |
| :-------------------------------------- | :----: | :----------------------------------------------------- |
| `/Plugin/sr7410-ssh/`                   | `0700` | SSH 持久化根目录（存放 Dropbear 主程序、依赖库及密钥） |
| `/Plugin/sr7410-ssh/authorized_keys`    | `0600` | 客户端 SSH 免密公钥白名单                              |
| `/Plugin/sr7410-ssh/autostart.enabled`  | `0600` | 开机自启生效开关标志                                   |
| `/Plugin/dlspeed_rt/dlspeed_monitor.sh` | `0755` | 注入自启 Hook 的监控脚本                               |
| `/var/sr7410-ssh/dropbear.pid`          | `0600` | Dropbear 运行 PID（仅存内存 RAM）                      |

---

## 3. 快速上手流程

### 步骤 0：环境准备（推荐 `uv`）

```bash
# 方式 A：使用 uv（推荐，免手动激活虚拟环境）
uv sync

# 方式 B：传统 pip
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

---

### 步骤 1：导出配置并解锁 Telnet

1. 登录路由器后台（如 `http://192.168.0.1`），进入【管理】$\rightarrow$【配置管理】$\rightarrow$【备份配置】，下载保存为 `config.bin`。
2. 运行解密与配置生成命令：
   ```bash
   uv run manage.py unlock-telnet config.bin
   # 若使用传统 pip 环境：python3 manage.py unlock-telnet config.bin
   ```
   _脚本会自动识别当前内网 IP、Telnet 账号密码，并在 `private/telnet/` 下生成待恢复配置。_

---

### 步骤 2：在后台恢复配置开启 Telnet

1. 回到路由器后台：【管理】$\rightarrow$【配置管理】$\rightarrow$【恢复配置】。
2. 选择上传刚生成的 `private/telnet/config-telnet-lan-only.bin`。
3. 点击“恢复配置”，等待路由器自动重启（约 1 分钟）。

---

### 步骤 3：一键部署 Dropbear SSH

本地生成密钥（若已有 `~/.ssh/sr7410_ed25519` 或 `~/.ssh/id_ed25519` 可跳过）：

```bash
ssh-keygen -t ed25519 -f ~/.ssh/sr7410_ed25519
```

执行安装（自动加载步骤 1 提取的密码并自动识别本地公钥）：

```bash
uv run manage.py install-ssh --host 192.168.0.1
# 若路由器修改过后台 IP，替换为实际 IP 即可（如 --host 192.168.1.1）
```

---

### 步骤 4：启用开机自启

```bash
uv run manage.py enable-autostart --host 192.168.0.1
```

_提示 `Autostart enabled` 即代表自启 Hook 已成功固化到闪存。_

---

### 步骤 5：免密登录

将以下配置添加至你的 `~/.ssh/config`（电脑或同局域网 Armbian 等）：

```ssh-config
Host sr7410
  HostName 192.168.0.1
  Port 22
  User Z500_w9bZ
  IdentityFile ~/.ssh/sr7410_ed25519
  IdentitiesOnly yes
  PasswordAuthentication no
```

执行登录：

```bash
chmod 600 ~/.ssh/sr7410_ed25519
ssh sr7410
```

秒进路由器超级管理员终端！

---

## 4. 常用维护指令

所有命令均使用 `uv run manage.py` 执行（传统环境替换为 `python3 manage.py`）：

- **连通性校验**：
  ```bash
  uv run manage.py verify-ssh
  ```
- **查看状态**：
  ```bash
  uv run manage.py status --host 192.168.0.1
  ```
- **备用 Telnet 手动连接**：
  ```bash
  telnet 192.168.0.1
  # 账号: root / 密码: abc123!@#
  ```
- **前台交互式调试（若遇到异常）**：
  ```sh
  /Plugin/sr7410-ssh/sbin/dropbear -F -E -s -D /Plugin/sr7410-ssh -p 22
  ```
- **彻底卸载 SSH**：
  ```bash
  uv run manage.py uninstall-ssh --host 192.168.0.1
  ```
