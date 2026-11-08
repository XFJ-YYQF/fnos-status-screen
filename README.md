# NAS Screen

飞牛 fnOS 接显示器后默认只显示一个登录终端。**NAS Screen** 是一个 fpk 应用,把这块屏幕变成实时的 NAS 状态面板;如果你的 Windows 电脑开机了,屏幕会自动切换成 Windows 电脑的概况,关机后再切回来。

- 纯 Python 标准库,NAS 端没有额外依赖,直接画在控制台 `tty1`(不需要图形界面)
- 黑底,宽屏自动双栏,每秒刷新

## 显示内容

**NAS 概况**:系统/内核/运行时长/负载 · CPU 占用、温度、频率、功耗、每核占用 · Intel 核显(i915 引擎占用、频率、功耗)或 NVIDIA 显卡 · 内存/swap · 每块物理网卡的链路速率、IP、实时上下行、累计流量 · `/volN` 存储用量与磁盘读写 · 每块硬盘的型号、温度、SMART 健康、通电时长、NVMe 磨损、休眠状态 · 主板温度与风扇 · 进程排行 · Docker 容器 · **CPU / GPU 占用与温度曲线**

**Windows 概况**(装了配套 agent 且电脑开机时):系统、CPU(占用/频率/核心分布)、GPU(NVIDIA / 通用)、内存、网卡、各盘符、磁盘读写、进程排行、同样的 CPU/GPU 曲线,底部保留一行 NAS 简况。

## 安装

### NAS 端

1. 到 [Releases](../../releases) 下载 `nasscreen-<版本>.fpk`
2. fnOS 应用中心 → 手动安装 → 选择该文件 → 启动应用
3. 显示器上登录提示被概况面板取代。需要登录终端时,键盘按 `Ctrl+Alt+F2` 切到 tty2

> 应用以 root 运行(接管 tty、读取 SMART/PMU 需要)。停止应用时会恢复 `tty1` 登录提示。

### Windows 端(可选)

1. 安装 Python 3(python.org,勾选 *Add python.exe to PATH*)
2. 下载 `nasscreen-windows-agent-<版本>.zip`,**先解压**,右键 `install.bat` → 以管理员身份运行,按提示输入 NAS 的 IP
3. 它会注册一个开机任务(SYSTEM 身份,登录前就会开始汇报),每秒向 NAS 的 `18765` 端口发送一次状态

Windows 开机 → NAS 屏幕切到 Windows 概况;关机/睡眠/休眠 → 汇报停止,约 8 秒后切回 NAS 概况。卸载运行 `uninstall.bat`。

**可选:CPU/GPU 温度、风扇、CPU 功耗** — Windows 本身读不到这些,需要同时运行 [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)(管理员权限),在 *Options → Remote Web Server → Run* 打开远程 Web 服务(端口 8085),agent 会自动读取。

## 配置

NAS 端配置文件为应用 var 目录下的 `config.json`(首次启动自动生成,每 30 秒重载,改完不用重启;`agent_port` 需重启应用)。找不到路径时:`find / -name config.json -path '*nasscreen*' 2>/dev/null`。

| 键 | 默认 | 说明 |
|---|---|---|
| `tty` | `/dev/tty1` | 输出到哪个控制台 |
| `view` | `auto` | `auto` 有 Windows 汇报就切过去 / `nas` 只看 NAS / `windows` 强制 Windows |
| `agent_port` | `18765` | 接收 Windows agent 汇报的端口 |
| `agent_token` | `""` | 可选口令;设置后 agent 的 `agent.json` 里 `token` 必须一致 |
| `agent_allow_ip` | `""` | 可选:只接受该 IP 的汇报 |
| `agent_timeout_sec` | `8` | 多久没收到汇报就认为 Windows 关机 |
| `smart_interval_sec` | `60` | SMART 轮询间隔(用 `smartctl -n standby`,不会唤醒休眠的硬盘) |
| `graph_height` | `4` | 每条曲线的行数(屏幕小会自动减) |
| `graph_step_sec` | `3` | 曲线每列代表的秒数 |
| `graph` | `block` | 曲线字符:`block`(█ ▄)或 `ascii`(控制台字体缺方块字符时用) |
| `font` | `auto` | 宽度 ≥ 200 列时尝试加载 Terminus 32x16 字体;`off` 关闭 |

> 默认不设口令也不限制 IP,局域网内任何设备都能向 NAS 推送数据到屏幕。不放心就设置 `agent_token` / `agent_allow_ip`。

## 排错

- 在 NAS 上运行 `python3 <应用目录>/dashboard.py --once`,会把当前画面打印到终端
- 屏幕显示 `render error`:完整堆栈在 `config.json` 同目录的 `error.log`,欢迎提 issue
- Windows 端:`python nasscreen_agent.py --once` 打印将要发送的数据;日志 `C:\ProgramData\NasScreenAgent\agent.log`
- 提示 `Run as administrator` 但已经是管理员:确认用的是最新版 `install.bat`(旧版受 PATH 里的 Git `whoami` 影响)
- NAS 屏幕不切换:检查 Windows/NAS 防火墙是否放行 `18765`,安装时的连通性检查会给出提示

## 已知限制

- Intel 核显没有独立温度传感器,GPU 温度显示的是 CPU 封装温度(标注 `(pkg)`);读不到 i915 PMU 时占用率用 RC6 驻留估算
- 新一代 Intel 核显使用 `xe` 驱动时没有 PMU 支持,只显示频率/估算占用
- Windows 的 GPU 温度/风扇依赖 LibreHardwareMonitor 的 JSON 格式,其他版本可能字段不同
- 控制台字体不含中文,界面为英文;不同硬件的传感器路径不同,部分项目可能显示 `n/a`

## 工作原理

```
Windows 电脑                          NAS (fnOS)
┌────────────────────┐   HTTP POST    ┌─────────────────────────────┐
│ nasscreen_agent.py │ ─────────────► │ dashboard.py                │
│ psutil / PDH / LHM │  1 次/秒 JSON  │  ├ HTTP 接收线程 (:18765)    │
└────────────────────┘                │  ├ 采集 (/proc /sys smartctl)│
                                      │  └ 渲染 → /dev/tty1 (ANSI)   │
                                      └─────────────────────────────┘
```

fpk 本质是一个 tar.gz:`manifest`、图标、`cmd/`(生命周期脚本,`cmd/main` 负责接管/归还 tty1 并拉起 Python)、`config/`(权限)、`app.tgz`(程序本体)。

## 目录结构

```
fnos/            fpk 源码(manifest、cmd/、config/、app/dashboard.py、图标)
windows-agent/   Windows 端(nasscreen_agent.py、install.bat、uninstall.bat)
scripts/build.sh 本地打包
tests/e2e.py     端到端冒烟测试(NAS→Windows→NAS 切换)
.github/         CI 与发布流程
```

## 本地构建

```bash
pip install psutil
python tests/e2e.py          # 约 20 秒
bash scripts/build.sh         # 输出到 dist/
```

## 发布

推送 `v*` 标签即可,GitHub Actions 会运行测试、打包并创建 Release(附 fpk、Windows agent zip 和 SHA256SUMS):

```bash
git tag v1.4.0
git push origin v1.4.0
```

版本号取自标签(`v1.4.0` → `1.4.0`),会自动写入 manifest,无需手动改。也可以在 Actions 页手动触发 *Release* 做一次只产出构建文件(不发布)的试打包。

## 许可证

MIT
