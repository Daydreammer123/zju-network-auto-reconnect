# 原生网络监控排查

项目仅使用 Windows 原生后台入口 `host_reconnect.py`，无需容器或浏览器。
首次运行、日志查看和登录自启动见 [README.md](README.md)；双网络状态机见
[WIFI_FAILOVER.md](WIFI_FAILOVER.md)。

## 先区分故障层次

| 层次 | 确认内容 | 处理方式 |
| --- | --- | --- |
| 无线链路 | 接口、无线电、当前配置、无线关联及安全认证 | 恢复已保存 Wi-Fi；关闭/禁用/未知状态不强行操作 |
| 地址与路由 | 非 APIPA IPv4、默认网关、WLAN 是否为默认出口 | 等待地址；不能把“连接请求成功”当作网络已恢复 |
| A 的门户认证 | 服务器是否返回已在线，是否需要重登 | 未认证时使用原生接口登录并回查结果 |
| B 的无线认证 | Windows 是否完成 WPA2-Enterprise/802.1X | B 不需要网页，但无线安全认证仍可能失败 |
| Clash 本地代理 | 系统代理是否开启、本地端口是否监听 | 未就绪时暂停代理故障切网，不重启 Clash |
| 代理上游 | 两个独立 HTTPS 目标是否都连接失败 | 连续两轮失败才切网；同一轮故障最多尝试 5 次；HTTP 拒绝响应不直接算断网 |

系统 Wi-Fi 已断开时，换成固定 IP 或重新提交门户账密不能建立无线连接。
正确顺序始终是：无线连接 → 地址/路由 → A 的认证（B 跳过）→ 实际外网/代理验证。

## 不切网的检查方式

在项目目录的 PowerShell 中执行：

```powershell
.\native_monitor.ps1 status
python -X utf8 host_reconnect.py --diagnose --interface WLAN
netsh wlan show interfaces
netsh wlan show profiles
Get-Content .\native_monitor.log -Encoding UTF8 -Tail 50
Get-Content .\native_monitor.error.log -Encoding UTF8 -Tail 30
```

双网络模式的 `--diagnose` 只读取本地状态，不请求门户、不访问外网、不累计故障。
单网络模式的诊断会只读查询门户和直连探测，但不会登录或连接 Wi-Fi。
不要公开 `.env`、完整认证请求、Wi-Fi 密钥、代理订阅或 API 密钥。

## A 未认证或约 14 天到期

正常周期查询服务器在线状态。返回未认证时获取 challenge、自动识别 AC 区域，
使用 `.env` 的账密提交认证，再回查在线状态及账号一致性。
Wi-Fi 同时断线时先恢复系统连接，不设固定 14 天强制注销定时器。

认证域名不可达时，默认认证配置可使用同域 HTTP 回退及固定 IP 后备路径，
但这仍依赖可用的校园网路由。不要随意把 `ZJU_AC_ID` 固定为 `1`，
实际区域由门户返回；也不要通过关闭 TLS 证书校验掩盖网络问题。

HTTP 502/503/504 不能单凭状态码判定来源。DNS 失败、上游不可达、拒绝连接和超时
由原生认证客户端分类记录，不输出可能带有凭据的完整认证 URL 或正文。
接口返回“提交成功”但未回查到在线状态，仍不能报告认证成功。

## B 切换失败：802.1X 与回退

A→B 切换时，Windows WLAN 事件可能先记录无线关联成功，随后出现：

```text
原因码：0x4800f
802.1x 指示没有验证器，但配置文件需要使用 802.1x。
```

这是无线安全认证失败，发生在 IPv4/DHCP 之前，不是代理探测误判；
单纯增加地址等待时间不能解决它。同一 B 配置之后成功连接，也不足以断言此前是密码错误。
仅凭此码不能断定具体 AP、认证服务或驱动是根因；频繁出现时应结合学校网络及驱动诊断。

可在事件查看器的 `Microsoft-Windows-WLAN-AutoConfig/Operational` 查看连接失败及安全认证事件，
重点关注事件 `8002`、`12013` 和对应连接的原因码，不要导出含身份信息的完整日志公开分享。

程序现在会在目标请求失败/超时后先恢复切换前的网络，再进入五分钟冷却；
如果已有 A/B 恢复连接则先验证，不重复断开。回退也失败时限速重试原网络，
冷却只限制再次换网，不会让断线设备空等五分钟。
验收脚本同样优先恢复最近实际验证成功的网络，并单独记录恢复结果。

单网卡无法同时维持 A 与 B，正常切换也会短暂断流，已有 TCP/远程连接可能需要重建。

## 代理正常与直连正常不是同一件事

认证请求和单网络直连探测绕过应用层代理；双网络代理探测则显式建立本地代理 CONNECT 隧道。
但 Clash 规则仍可能将目标分流为 DIRECT，必须在现有规则/连接记录中确认探测目标走代理。
本程序不修改节点、订阅或规则，不把本地端口存在视为代理上游一定可用。

单个网站返回 403/429、证书错误或无法判断的响应不会触发切网；
系统代理关闭、端口退出或配置改变同样不会累计校园 Wi-Fi 故障。

### 持续失败后不再切网

日志出现“代理故障切换已达上限 5/5”是防反复切换保护，不是监控停止。
`PROXY_MAX_SWITCHES` 默认 5，统计本轮代理故障发出的切网尝试，连接请求失败也计入。
达到上限后仍按 `PROXY_CHECK_INTERVAL` 经系统代理探测 HTTPS 外网，继续维护 A 的认证，
但不再因代理失败换网；五分钟冷却到期、关闭再开启代理、未知响应都不会重置次数。
请检查 Clash 节点、订阅或共同上游；任意探测目标成功后自动清零并恢复故障切换能力。
Wi-Fi 真正断线的恢复及失败回退不受此上限限制。次数仅保存在监控进程内，重启监控会清零。

## 登录自启动与日志

计划任务 `ZJU-Automatic-Reconnection` 在当前用户登录时只启动或复用原生监控。
两种网络模式均无需容器运行时。升级旧版任务、移动项目或更换 Python 后重新注册：

```powershell
.\configure_autostart.ps1 enable
.\configure_autostart.ps1 status
Get-Content .\startup_reconnection.log -Encoding UTF8 -Tail 30
```

用户任务正常应持续处于 `Running`，监督 Python 并在退出后重试；`Ready` 不证明监控存活。
后台进程用 `native_monitor.ps1 status` 单独查看。关闭自启动不会停止当前监控。
修改 `.env` 后停止再启动，日志时区使用 Windows 本地配置，无需设置 `TZ`。

### 断线恢复回归

B 断线可能记录 `0x50006`（“身份验证程序不再存在”），随后驱动断开连接。
无默认路由查询会报未找到对象；旧实现吞掉异常后返回未知，导致重连分支不可达。
现在接口识别/断线读取不依赖 IP，并包含实际 PowerShell 脚本的无路由回归测试。
不能只凭通用未知日志确定底层异常，应结合阶段/错误类型排查。

登录前联网、UU 官方服务守护、受保护部署及重启验收见 [BOOT_RECOVERY.md](BOOT_RECOVERY.md)。

## 测试与参考

```powershell
python -S -X utf8 -m unittest discover -s tests -p 'test_*.py'
```

全量测试不需要第三方包，包含门户 JSONP/固定 IP/区域识别、认证成功回查、
认证页误判、代理分类、双向切换、连接失败回退及独立 UU 守护。
测试使用假接口/假时钟和本机 HTTP 服务，不注销真实认证，不切换真实 Wi-Fi。
真实验证脚本会影响连接，必须另行授权后按各脚本说明执行；本次清理不执行这些测试。

此前核对并采用的设计参考：

| 实现 | 参考要点 |
| --- | --- |
| [Xinzhe99/zju-autologin](https://github.com/Xinzhe99/zju-autologin) | 区分无校园链路、未认证和认证后无外网；认证流量直连 |
| [Mmx233/BitSrunLoginGo](https://github.com/Mmx233/BitSrunLoginGo) | 明确接口选择、自动 AC 识别、分层恢复；未复制其 AGPL 源码 |
| [vidar-team/srun-login](https://github.com/vidar-team/srun-login) | 原生 challenge/portal 认证，使用服务器识别的校园网客户端 IP |
