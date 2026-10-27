# 登录前联网与 UU 远控

## 使用约定

- 不改 Windows 的 Wi-Fi 连接模式、802.1X 凭据、代理、自动登录或电源设置。
- A/B 都使用已有配置；当前正常连接不主动切回 A。
- 不主动制造断网、不锁屏/注销、不自动重启；真实断线和重启验收由用户执行。

## 两层监控

| 组件 | 账户/触发 | 职责 |
| --- | --- | --- |
| `ZJU-Automatic-Reconnection-Boot` | SYSTEM，系统启动 | 尚未登录时恢复 WLAN 和 A 的校园网认证；B 不执行门户认证；独立守护官方 `GameViewerService` |
| `ZJU-Automatic-Reconnection` | 当前用户，登录 | 完整 A/B、Clash 检测、用户会话 UU 客户端守护 |

开机组件检测到已有登录用户后让出网络控制；锁屏或断开的用户会话仍属于已登录。
跨进程字节锁保证交接时不并发认证或切网。会话检测失败时不猜测“无人登录”。
开机模式不依赖 Clash 或 HKCU 系统代理，也不会在 Session 0 启动 `GameViewer.exe`。
UU 官方服务负责登录界面的远控能力；服务 Running 不能证明账号绑定、被控设置或端到端远控可用。
请在 UU 设置中自行确认“开机自动启动”及无人值守访问设置；本项目不修改这些设置。
官方说明：https://uuyc.163.com/blog/guide-auto-20250527.html

任务结果 `267009`（`0x41301`）表示任务仍在运行，不是错误码。

## 安全部署

不能让 SYSTEM 从普通用户可写的项目或 Anaconda 目录运行代码。
`configure_boot.ps1` 会创建：

```text
C:\ProgramData\ZJU-Automatic-Reconnection\
  releases\<版本时间>\runtime\    官方独立 Python
  releases\<版本时间>\app\        本次代码快照
  config\boot.env                  仅管理员/SYSTEM 可读写的配置副本
  state\network.lock               只允许指定用户锁定，不允许其删除
  logs\boot_monitor.log
  installation.json                版本和代码校验记录，不包含凭据
```

代码、解释器及目录仅管理员/SYSTEM 可修改；普通用户只读非敏感文件。
官方 ZIP 的 SHA-256 和解释器 Authenticode 签名均须通过，`._pth` 禁用外部包和用户路径。
系统任务跟踪 Python 全生命周期，异常退出后一分钟重试；用户任务持续监督 Python，退出后十秒重试。
当前用户对开机任务仅有读取状态/配置的权限，没有运行、修改、删除或变更权限的权限。
不要把解释器路径改回用户可写的 Anaconda，也不要只复制脚本而忽略权限。

### 安装或更新

先在普通用户会话运行测试：

```powershell
python -S -X utf8 -m unittest discover -s tests -p 'test_*.py'
```

从官方获取独立运行时（不会替换现有 Python）：

- https://www.python.org/ftp/python/3.13.17/python-3.13.17-embed-amd64.zip
- SHA-256：`9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38`
- 发布页：https://www.python.org/downloads/release/python-31317/

在**管理员 PowerShell** 中，从项目目录执行：

```powershell
.\configure_boot.ps1 enable -RuntimeArchive '下载目录\python-3.13.17-embed-amd64.zip'
```

该脚本会执行本地只读预检，再安装/启动开机任务；当前已有登录用户时，开机组件不会切网。
更新 `.env` 或代码不会自动更新受保护副本，需再次执行以上安装命令。
旧发布快照不会自动删除，便于追溯；配置副本按本次源 `.env` 更新。

在普通 PowerShell 更新用户任务并重启本项目监控（不主动断网）：

```powershell
.\native_monitor.ps1 stop
.\configure_autostart.ps1 enable
.\native_monitor.ps1 start
.\native_monitor.ps1 status
.\configure_boot.ps1 status
```

`stop` 会先停止用户计划任务/监督进程，再停止 Python，避免立刻被拉起。
不会停止开机组件或 UU 官方服务。开机组件此时因存在登录用户而让出网络控制。

## 已修复的断线恢复路径

1. 接口名称通过 IP Helper API 读取，不依赖 DHCP 或默认路由。
2. WLAN 已断开时不查询 IP，返回明确的离线快照。
3. 无默认路由是正常离线状态；真实 IP 查询错误单独记录并保守等待。
4. Windows 正在关联/认证时先等待一个有限连接窗口，不每五秒重置认证。
5. 同一网络恢复失败两次后，限速尝试另一个已保存网络，独立于代理是否可用。
6. 切换失败后的原网络回退冷却仍有效，不立即再次尝试刚失败的目标。

默认连接等待窗口 30 秒，失败后限速 30 秒；两次完整超时后才尝试备用网络。
实际时间还包括系统状态查询和认证耗时。可用 `WIFI_RESTORE_FAILURE_THRESHOLD` 调整次数。
手动关无线电、禁用网卡或选择其他 Wi-Fi 时不会强行抢回。

## 用户实机验收

先确认身边可操作电脑，或有备用访问方式。

### 断线恢复

1. 用 `native_monitor.ps1 status` 确认监控和监督进程运行。
2. 保持 Wi-Fi 开关开启，只手动“断开”当前 A/B。
3. 查看日志，应出现“已请求连接…”、“连接及地址已确认”，随后认证/代理确认。
4. 若原网络不可用，应在有限重试后尝试另一网络，而不是永远“无线状态未知”。
5. 不要通过关闭 Wi-Fi 或禁用网卡模拟意外断线；程序会尊重这种人工操作。

### 重启后登录前远控

1. 确认两个任务已启用、UU 服务为自动启动；不改 Windows 登录密码。
2. 用户自行重启电脑，停留在登录界面，不在本机登录。
3. 用另一台设备通过 UU 尝试连接，确认能显示并操作登录界面。
4. 远程正常输入 Windows 登录凭据，确认进入桌面后用户监控接管。
5. 检查 `boot_monitor.log`：登录前有链路/认证记录，登录后有“让出控制”。

仅本地预检或任务 Running 不能替代这项验收。若重启后网卡被禁用、无线电关闭、
凭据失效、AP 不可用、磁盘未解锁，软件无法保证自动联网。

## 日志、停用

```powershell
Get-Content .\native_monitor.log -Encoding UTF8 -Tail 50 -Wait
Get-Content 'C:\ProgramData\ZJU-Automatic-Reconnection\logs\boot_monitor.log' -Encoding UTF8 -Tail 50
.\configure_autostart.ps1 disable
.\native_monitor.ps1 stop
# 以下需要管理员权限，仅停用本项目开机任务，不停止 UU 服务：
.\configure_boot.ps1 disable
```

源项目 `.env` 和受保护副本均含凭据，不要分享；日志和部署报告也应先检查再分享。
