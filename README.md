# ZJU Network Auto Reconnect｜ZJU校园网自动重连

在 Windows 上自动恢复校园网连接、维护认证，并在 Clash 系统代理不可用时切换
`ZJUWLAN`（A）与 `ZJUWLAN-Secure`（B）。同时提供网易 UU 远程进程守护。

**运行和全部单元测试均只依赖 Python 标准库**，通过 Windows 原生接口管理网络连接和后台进程。

## 功能与行为

| 功能 | 行为 |
| --- | --- |
| A/B 故障切换 | 每 60 秒通过系统代理检测两个 HTTPS 目标；连续 2 轮全部连接失败才切换 |
| A：ZJUWLAN | 先恢复 Wi-Fi 和地址，再检查校园网门户；未认证或过期时自动重新登录 |
| B：ZJUWLAN-Secure | 复用 Windows 已保存配置，**不执行网页登录认证**；无线自身仍需完成 802.1X |
| 连接失败保护 | 目标连接失败先回退原网络，再冷却 5 分钟；冷却不阻止恢复原网络 |
| 保留正常连接 | 当前网络正常就不切换；不优先回 A，不修改 Clash 节点、订阅或规则 |
| 手动操作保护 | 其他 Wi-Fi、无线电关闭、网卡禁用或状态未知时不盲目切网 |
| 认证维护 | 根据服务器实际在线状态重登，不设置固定日期强制注销 |
| UU 远程守护 | 独立线程监控主进程，升级期间等待，退出后按配置自动恢复 |
| 断线备用恢复 | 同一配置恢复连续失败两次后，限速尝试另一已保存网络，不等待代理启动 |
| PC重启后自动联网 | 可选受保护的 SYSTEM 开机组件，无需先登录 Windows 即可恢复 WLAN/A 认证并守护 UU 官方服务 |

单无线网卡切换会短暂断流，不能保证已有 TCP/远程连接无缝迁移。
详细策略、代理结果分类和失败回退见 [WIFI_FAILOVER.md](WIFI_FAILOVER.md)。

## AGENT 下载安装提示词

将下面整段提示词复制给具有本机终端和文件操作能力的 AGENT，即可由其协助完成下载安装。
注意账号密码仅在本机填写，不要发送到对话中。

```text
请在这台 Windows 电脑上下载安装并配置 ZJU Network Auto Reconnect：
https://github.com/Daydreammer123/zju-network-auto-reconnect

请实际执行安装，而不只是给出命令；遇到权限、凭据或环境问题时说明阻塞，不要猜测成功。

1. 先一次性确认安装目录、单网络或 A/B 双网络模式，以及是否启用登录自启动、
   PC重启后自动联网和网易 UU 远程守护。未明确选择的可选功能不要启用。
   说明启动监控后可能自动认证或恢复网络，得到确认后再启用。

2. 检查 Windows、Git、GitHub CLI、Python 3.10+、PowerShell 和 WLAN 服务。
   缺少工具时先征得安装许可，仅从官方来源安装；不要替换已有 Python 环境。
   使用已有 GitHub CLI 登录状态访问私有仓库；如未登录，指导我通过
   gh auth login --web --git-protocol https 完成授权，不索要或输出访问令牌。
   无仓库访问权限时停止，不尝试改成公开仓库或绕过权限。

3. 在确认的目录执行：
   gh repo clone Daydreammer123/zju-network-auto-reconnect
   若目标目录已有文件或已有部署，先检查并征询处理方式，不覆盖配置、不强制拉取、
   不丢弃本地修改，不修改其他项目的 Git 远程地址。
   进入项目后先阅读 README.md、WIFI_FAILOVER.md 和 BOOT_RECOVERY.md。

4. 仅在 .env 不存在时，从 .env.example 复制创建。
   提示我在本机编辑器填写校园网账号密码，填写后只检查必填项是否就绪，不回显值。
   不把 .env、账号密码、令牌、Wi-Fi 密钥、代理订阅或完整日志读入对话、提交或上传。
   根据所选模式设置非敏感配置，保留已有凭据；不安装第三方 Python 依赖。
   A/B 模式必须确认两个 Wi-Fi 已在 Windows 保存，且两个 HTTPS 探测目标在 Clash
   规则中实际走代理；不要读取 Wi-Fi 密钥，也不要擅自修改代理规则。

5. 用选定的 Python 执行标准库测试：
   python -S -X utf8 -m unittest discover -s tests -p 'test_*.py'
   测试失败先报告原因，不继续启用监控。不要运行真实切网、注销认证或故障注入脚本。

6. 配置就绪并确认可启动后，按我的选择启用登录自启动，再启动后台监控：
   .\configure_autostart.ps1 enable
   .\native_monitor.ps1 start
   .\native_monitor.ps1 status
   未选择登录自启动时跳过第一条。已有监控需更新配置时，先确认再按文档停止并重启，
   不重复启动，也不要擅自停用其他安装的任务。

7. 仅在我选择“PC重启后自动联网”并明确同意管理员操作后，按 BOOT_RECOVERY.md
   从 python.org 下载脚本指定版本的官方独立运行时 ZIP，核验 SHA-256 和签名，
   安装受保护的 SYSTEM 开机组件：
   .\configure_boot.ps1 enable -RuntimeArchive '<已下载ZIP的完整路径>'
   不绕过脚本校验，不从用户可写目录以 SYSTEM 运行；若以不同管理员账号提权，
   用 -UserSid 指定实际使用本项目的用户。无法提权时给出待本人执行的完整命令。
   登录自启动和 SYSTEM 开机组件是两项独立配置，不要混为一谈。

8. 不修改 Windows 自动登录、Wi-Fi 安全配置、代理订阅或 UU 账号设置，
   不自动重启、注销、锁屏或主动断网。UU 未安装时报告并跳过相关功能。
   最后仅汇总安装结果、所选功能、后台进程和计划任务状态、失败项及停止方式。
   区分“已安装/进程运行”和“网络恢复/远程访问已验证”；重启及端到端远控由我自行验收。
```

## 环境与首次配置

- Windows，Python **3.10 或以上**，PowerShell 和系统 WLAN 服务可用。
- A/B 模式需要两个 Wi-Fi 都已在 Windows 上成功连接并保存。
- Clash 使用系统代理及本地 HTTP/Mixed 端口；本项目不安装或管理 Clash。

在项目目录打开 PowerShell，首次使用时创建配置文件：

```powershell
# 已存在 .env 时不覆盖，避免丢失账号密码。
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item .env.example .env
}
```

编辑 `.env`，保留原有凭据并设置所需模式：

```dotenv
ZJU_USERNAME=你的校园网账号
ZJU_PASSWORD=你的校园网密码
ZJU_WIFI_INTERFACE=WLAN
ZJU_WIFI_FAILOVER=1
ZJU_WIFI_PROFILE_A=ZJUWLAN
ZJU_WIFI_PROFILE_B=ZJUWLAN-Secure
PROXY_CHECK_INTERVAL=60
PROXY_FAILURE_THRESHOLD=2
WIFI_FAILOVER_COOLDOWN=300
PROXY_CHECK_URLS=https://www.google.com/generate_204,https://www.wikipedia.org/
```

值直接填写，不加引号；`$`、`#` 和密码中的引号会原样保留。进程环境变量优先于 `.env`。
`.env.example` 默认关闭双网络模式；按需设置 `ZJU_WIFI_FAILOVER=1` 启用。
配置名和接口名可通过 `netsh wlan show profiles`、`netsh wlan show interfaces` 确认。

**必须确认两个探测目标在 Clash 现有规则中走代理，而非 DIRECT。** 经本地代理端口发送请求
仍可能被 Clash 分流直连，不能把这种成功当成代理节点正常。

不需要执行 `pip install`；`requirements.txt` 仅说明项目没有第三方 Python 依赖。

## 启动与停止

```powershell
.\native_monitor.ps1 start
.\native_monitor.ps1 status

# 停止本项目后台监控
.\native_monitor.ps1 stop
```

后台进程在关闭终端后继续运行。监督进程复用已有监控，Python 退出后十秒重试；
已配置的用户计划任务会持续托管监督进程。字节锁避免重复运行和跨会话并发切网。
修改 `.env` 后先 `stop` 再 `start`；不需要重新安装依赖。

需要前台运行时使用 `python -X utf8 host_reconnect.py`，但不要与后台监控同时运行。

### 查看日志

```powershell
# 实时查看最近 50 行运行日志
Get-Content .\native_monitor.log -Encoding UTF8 -Tail 50 -Wait

# 查看异常输出
Get-Content .\native_monitor.error.log -Encoding UTF8 -Tail 30

# 查看登录自启动过程
Get-Content .\startup_reconnection.log -Encoding UTF8 -Tail 30
```

查看实时日志时按 `Ctrl+C` **只停止查看，不停止监控**。
新启动后台进程会将上一轮日志保存为 `.previous` 后重建；更早日志需自行备份。
日志使用 UTF-8；时间遵循 Windows 本地时区，不需要额外配置 `TZ`。

### 只读诊断

```powershell
python -X utf8 host_reconnect.py --diagnose --interface WLAN
```

双网络模式下只读取本地 WLAN、系统代理和 UU 状态，不登录、不切网、不探测外网；
不能据此证明代理目标可用。排查步骤见 [NETWORK_TROUBLESHOOTING.md](NETWORK_TROUBLESHOOTING.md)。

## Windows 登录自启动

```powershell
.\configure_autostart.ps1 enable
.\configure_autostart.ps1 status
.\configure_autostart.ps1 disable
```

计划任务名称为 `ZJU-Automatic-Reconnection`，在当前用户登录时调用
`startup_reconnection.ps1`，**持续监督后台监控进程**。
任务参数使用 PowerShell、Python 和项目的绝对路径，不包含账号密码。

移动项目目录或更换 Python 解释器后，应重新运行 `enable` 更新任务参数。
`disable` 只关闭后续登录自启动，不停止已运行的监控。

## PC重启后自动联网

如需 PC 重启后在 Windows 登录界面自动联网，须另行安装需管理员授权的 SYSTEM 开机组件。
该组件使用
`C:\ProgramData\ZJU-Automatic-Reconnection` 内的受保护代码、配置和官方独立 Python，
不从用户可写的项目或 Anaconda 目录以 SYSTEM 运行。
它不修改 Windows Wi-Fi 连接模式、802.1X、自动登录或 Clash；检测到用户登录后让出网络控制。
重启后尚未登录 Windows 时，UU 远控依赖已安装的 `GameViewerService`，不会启动 Session 0 桌面客户端。
安装、安全说明、日志和用户验收见 [BOOT_RECOVERY.md](BOOT_RECOVERY.md)。

## 网易 UU 远程守护

默认每 **5 秒**检测当前登录会话的 `GameViewer.exe`。最小化或留在托盘视为正常运行；
服务/崩溃报告进程不算主进程。发现升级器时等待升级完成，启动文件连续稳定 **30 秒**后才恢复启动；
启动失败则间隔 **60 秒**重试。网络检测阻塞不会阻塞此独立线程。

```dotenv
UU_REMOTE_MONITOR=1
UU_REMOTE_EXE=
UU_REMOTE_CHECK_INTERVAL=5
UU_REMOTE_UPDATE_GRACE=30
UU_REMOTE_START_RETRY=60
```

`UU_REMOTE_EXE` 留空时从卸载注册表自动识别；自定义路径指向软件启动入口，不加引号。
手动退出 UU 也会重新启动。需要保持关闭时设置 `UU_REMOTE_MONITOR=0` 并重启监控。
本项目不安装、卸载或修改 UU 软件。

## 原生单网络模式

设置 `ZJU_WIFI_FAILOVER=0`，通过 `ZJU_WIFI_PROFILE` 指定一个已有的 Wi-Fi 配置。
此模式使用 `CHECK_INTERVAL`、`LINK_RETRY_INTERVAL`、`NETWORK_CHECK_URLS` 等配置，
先恢复物理链路，再检查门户认证和直连外网，不执行 Clash A/B 故障切换。

**B 的门户认证隔离属于双网络模式**；不要将单网络模式用于需要跳过门户的 B。

## 测试

```powershell
# -S 禁用第三方 site-packages，验证全量测试仅使用标准库。
python -S -X utf8 -m unittest discover -s tests -p 'test_*.py'
```

单元测试模拟接口、代理和时钟，不切换真实 Wi-Fi；HTTP 集成测试只访问本机回环服务。
需要真实切换验收时必须单独授权、暂停生产监控，并阅读 [WIFI_FAILOVER.md](WIFI_FAILOVER.md)
中的验收说明；不要为日常排查主动制造断网。

## 主要文件

| 文件 | 用途 |
| --- | --- |
| `host_reconnect.py` | 校园网认证维护与后台线程协调 |
| `wifi_failover.py` / `wifi_control.py` | A/B 状态机、失败回退、Windows WLAN 读取与连接 |
| `network_checks.py` / `srun_client.py` | 直连/代理探测与门户原生认证 |
| `uu_remote_monitor.py` | UU 远程守护 |
| `native_monitor.ps1` | 后台启动、停止、状态查看 |
| `configure_autostart.ps1` / `startup_reconnection.ps1` | 登录计划任务与原生启动流程 |
| `configure_boot.ps1` / `runtime_control.py` | PC重启后自动联网组件的受保护部署、跨会话互斥与会话检测 |

账号密码仅保存在本地 `.env` 或进程环境变量中，`.env` 和运行日志已排除出 Git。
建议仅在个人设备上使用，并遵守校园网络使用规定。

## 致谢

感谢 [MUXYV/Automatic_reconnection](https://github.com/MUXYV/Automatic_reconnection.git)
项目提供的校园网自动重连思路与参考。
