# ZTE SR7410 (BE7200 Pro+) DIY 开发与硬件能力指南

## 1. 硬件能力边界：DIY 能做什么 / 不能做什么

| 维度                    | 实测规格与现状                                                                                                          |
| :---------------------- | :---------------------------------------------------------------------------------------------------------------------- |
| **SoC / 架构**          | Sanechips ZX279133（双核 Cortex-A53，Linux 5.4.196 `aarch64`）                                                          |
| **存储与内存**          | 可用 RAM ~436 MB；`/Plugin` (JFFS2) 可用 ~27 MB；`/DPITMP` (tmpfs) 61 MB                                                |
| **硬件加速 (NP / FFE)** | `FFE 3.6.6`（最大 120,000 条流表）：支持 **L2/L3 路由转发、NAPT、PPPoE 卸载、DS-Lite、组播、WLAN-LAN/WAN 直通**         |
| **自带 VPN 真实情况**   | 节点小宝、UU 加速器、OpenVPN 均纯 CPU 用户态运行；IPSec 时钟在开机时被固件关停（`wclk=0x0, aclk=0x0`），同样走 CPU 软算 |

### 结论：DIY 的正确发力点

- **不可为**：**不要**在路由器本机运行大吞吐加密代理（如 VLESS/Trojan/OpenVPN/Mihomo TUN）。所有走 TUN 设备或用户态加解密的流量都会脱离 FFE 硬件路径，直接打满双核 A53 CPU。
- **大有可为**：
  1. **控制面与数据面分离的高性能分流（核心推荐）**：本机只运行极轻量的控制面（如 Fake-IP DNS 解析、策略路由决策），数据面交由标准三层 IP 路由，**100% 触发 FFE 硬件线速转发**。
  2. **轻量级本地服务**：自定义 DNS 引擎（Fake-IP / 广告过滤 / 域名分流）、DDNS、内网穿透控制端、端口监控、自动化运维工具等。

---

## 2. 最佳网络架构：Fake-IP + FFE 硬件线速转发

路由器上只需处理几 KB 的 DNS 请求（控制面），海量数据包通过标准 IP 路由直接命中硬件流表，**0% CPU 占用**送往内网高性能网关（旁路由）：

```text
[终端] ──(1. DNS 查询)──▶ [路由器本地自定义 DNS (CPU 微秒级返回 198.18.x.y)]
  │
  └──(2. 访问 198.18.x.y)──▶ [SR7410 命中静态路由 198.18.0.0/15 via 192.168.0.254]
                                   │
                           (3. 触发 NP / FFE 硬件流表)
                                   │
                                   ▼ (0% CPU 线速直通)
                            [内网旁路由网关 (192.168.0.254)]
```

- **直连流量**：DNS 返回真实公网 IP，走默认网关 `ppp0`，同样命中 FFE 硬件 NAPT 线速直出。
- **代理流量**：DNS 返回保留网段（如 `198.18.0.0/15`），因不经过本机 TUN 设备，直接由 FFE 指令链（`ipv4_set -> eth_set`）硬件直通旁路由。

---

## 3. 自行开发与部署关键步骤

### 方案 A：独立静态二进制 + 守护 Hook（推荐，轻量零依赖）

#### 步骤 1：交叉编译

推荐使用 **Go** 静态编译（零动态库依赖，无需匹配系统裁剪过的 libc）：

```bash
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -ldflags="-s -w" -o my-app .
```

_(若使用 C/C++，需使用 `aarch64-linux-gnu` 工具链并匹配 glibc 2.27 / Entware `aarch64-3.10` 运行时)_

#### 步骤 2：部署存储（防 Flash 磨损）

1. 在 `/Plugin/` 下创建独立目录（如 `/Plugin/my-app/`），存放压缩后的程序包（`.tar.gz`）和配置文件。
2. 运行时在启动脚本中将其解压到内存盘 `/DPITMP/my-app/` 下执行：
   ```sh
   mkdir -p /DPITMP/my-app
   tar -xzf /Plugin/my-app/app.tar.gz -C /DPITMP/my-app
   /DPITMP/my-app/my-app -config /Plugin/my-app/config.yaml >/dev/null 2>&1 &
   ```
   _(注：日志重定向至 `/dev/null` 或内存，禁止频繁写入 `/Plugin` JFFS2 闪存)_

#### 步骤 3：挂载开机自启

利用已验证的持久化守护 Hook `/Plugin/dlspeed_rt/dlspeed_monitor.sh`，在其中检查进程 PID，若未运行则拉起 `/Plugin/my-app/start.sh`。

#### 步骤 4：网络/DNS 流量接入（以自定义 DNS 为例）

系统默认 `dnsmasq` 监听 53 端口，接入自定义 DNS 有两种无侵入方式：

- **方式 1（iptables 重定向，最稳妥）**：自定义 DNS 监听非 53 端口（如 `5353`），使用 `iptables` 将 `br0` 来的 53 端口 UDP/TCP 请求 `REDIRECT` 到 `5353`。
- **方式 2（串联）**：保持 `dnsmasq` 作为前端，或通过配置将其上游指向本地自定义 DNS 端口。

---

### 方案 B：中兴原生插件规范部署（`/Plugin/apps/`）

如需纳入系统 `pluginmgr` 托管（运行在 LXC 容器内），按官方 `opkg` 结构打包存放至 `/Plugin/apps/<app_name>/`：

1. **编写 `control` 文件**：
   ```text
   Package: my_app
   Version: 1.0.0
   Vendor: DIY
   Architecture: all
   StartMode: 0
   StartCMD: /my_app/start.sh
   StopCMD: /my_app/stop.sh
   ```
2. **编写 `start.sh` 与 `stop.sh`**：赋予 `0755` 权限，路径以容器挂载根目录 `/my_app/...` 编写。
3. **注意事项**：容器内网络命名空间与宿主机的隔离情况需视 `/lxc/nativeC.conf` 挂载配置而定，底层路由控制或硬件状态探测优先使用方案 A 在宿主机直接运行。
