"""Windows 原生网络恢复，以及可选的 A/B 双 Wi-Fi 代理故障切换。

仅依赖标准库。--diagnose 为只读检查；--wifi-profile 明确启用系统 Wi-Fi 重连。
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from network_checks import internet_available, portal_urls
from srun_client import PortalError, PortalUnavailable, SrunClient
from uu_remote_monitor import UUServiceMonitor, UURemoteMonitor
from runtime_control import FileLease, network_lock_path, user_session_present
from wifi_failover import FailoverConfig, WifiFailover, failover_enabled
from wifi_control import connect_wifi as _connect_saved_wifi


def load_env(path):
    """读取 raw .env，不插值 $、不去除密码里的 # 或引号。"""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value)


def physical_link(interface=""):
    """只检查物理网卡的 IPv4/默认网关，排除虚拟网卡的假在线。返回 None 表示未知。"""
    if sys.platform != "win32":
        return None
    script = """
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
    $ErrorActionPreference = 'Stop'
    $rows = @(Get-NetAdapter -Physical | ForEach-Object {
        $config = Get-NetIPConfiguration -InterfaceIndex $_.ifIndex
        [pscustomobject]@{
            name = $_.Name
            up = ($_.Status -eq 'Up')
            gateway = [bool]$config.IPv4DefaultGateway
            ipv4 = @($config.IPv4Address.IPAddress)
        }
    })
    ConvertTo-Json -InputObject $rows -Compress -Depth 4
    """
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, encoding="utf-8", errors="replace", timeout=10,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            return None
        rows = json.loads(result.stdout)
        return any(row["up"] and row["gateway"]
                   and any(ip and not ip.startswith("169.254.") for ip in row["ipv4"])
                   for row in rows if not interface or row["name"].casefold() == interface.casefold())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def connect_wifi(profile, interface=""):
    """只连接用户明确指定的现有配置；不创建配置，不开无线电，不启用被禁用的网卡。"""
    return _connect_saved_wifi(profile, interface)


class HostMonitor:
    def __init__(self, wifi_profile="", interface="", boot_network=False):
        self.username = os.environ.get("ZJU_USERNAME", "").strip()
        self.password = os.environ.get("ZJU_PASSWORD", "")
        self.profile = wifi_profile
        self.interface = interface
        self.boot_network = boot_network
        self._handover_state = None
        self.interval = max(1, int(os.environ.get("CHECK_INTERVAL", "60")))
        self.retry_interval = max(1, int(os.environ.get("LINK_RETRY_INTERVAL", "5")))
        self.timeout = max(1, int(os.environ.get("NETWORK_CHECK_TIMEOUT", "5")))
        self.failover = (WifiFailover(FailoverConfig.from_env(interface, proxy_required=not boot_network), self.log, self.ensure_authenticated)
                         if boot_network or failover_enabled() else None)
        if boot_network:
            self.failover.probe_enabled = False

    def client(self):
        return SrunClient(portal_urls(os.environ.get("ZJU_URL", "https://net.zju.edu.cn/")),
                          acid=os.environ.get("ZJU_AC_ID"),
                          portal_ip=os.environ.get("ZJU_PORTAL_IP", "10.50.254.11"),
                          timeout=max(1, int(os.environ.get("ZJU_API_TIMEOUT", "5"))))

    def log(self, message):
        for secret in (self.username, self.password):
            if secret:
                message = message.replace(secret, "[REDACTED]")
        print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)

    def check_once(self):
        if self.boot_network and user_session_present():
            if self._handover_state != "user":
                self.log("检测到已登录用户（含锁屏/断开会话），开机网络组件让出控制；由用户监控负责恢复和切换。")
                self._handover_state = "user"
                self.failover._reset()
                self.failover.active_profile = ""
                self.failover.unmanaged = False
            return "user_session", 5
        self._handover_state = "boot"
        if self.failover is not None:
            return self.failover.check_once()
        return self.check_legacy_once()

    def coordinated_check(self):
        # 开机及用户监控共享同一字节锁。交接时也不允许两个进程并发认证/切网。
        with FileLease(network_lock_path()) as lease:
            if not lease.acquired:
                return "network_busy", self.retry_interval
            return self.check_once()

    def ensure_authenticated(self):
        """双网络模式仅在 A 调用。认证故障不阻断后续代理检测/故障切换。"""
        try:
            client = self.client()
            status = client.status()
            if client.is_online(status):
                return True
            if not self.username or not self.password:
                self.log("A 未认证，缺少 ZJU_USERNAME / ZJU_PASSWORD；不会重复提交空凭据。")
                return False
            self.log("A 未认证或认证已过期，正在重新认证。")
            client.reconnect(self.username, self.password, initial_status=status)
            self.log("A 校园网认证已确认。")
            return True
        except PortalError as error:
            self.log(f"A 认证未完成：{error}")
            return False

    def check_legacy_once(self):
        link = physical_link(self.interface)
        if link is False:
            self.log("系统物理网卡未连接或未取得 IPv4/默认网关，暂不提交校园网认证。")
            if self.profile:
                requested = connect_wifi(self.profile, self.interface)
                self.log("已请求 Windows 连接保存的 Wi-Fi 配置；下一轮确认连接和地址。" if requested
                         else "Windows Wi-Fi 连接请求失败；检查配置名、无线开关和网卡状态。")
            else:
                self.log("等待系统网络恢复；可设置 ZJU_WIFI_PROFILE 启用已保存 Wi-Fi 的自动连接。")
            return "link_down", self.retry_interval
        if link is None:
            self.log("无法读取物理网卡状态，将以认证服务器可达性判断网络。")
        client = self.client()
        try:
            # 每周期主动查询认证，避免其他出口/代理可达掩盖 14 天认证过期。
            status = client.status()
            if client.is_online(status):
                if internet_available(self.timeout):
                    self.log("校园网认证在线，网络正常")
                    return "online", self.interval
                self.log("校园网已认证，但外网不可达；检查 DNS、路由或校园网出口。")
                return "authenticated_no_internet", self.retry_interval
            if not self.username or not self.password:
                self.log("门户可达且未认证，请配置 ZJU_USERNAME / ZJU_PASSWORD。")
                return "need_credentials", self.interval
            self.log("校园网未认证或认证已过期，正在使用配置的账号密码重新认证...")
            client.reconnect(self.username, self.password, initial_status=status)
            if internet_available(self.timeout):
                self.log("校园网认证已确认，外网访问已恢复。")
                return "online", self.interval
            self.log("校园网已认证，但外网探测仍失败。")
            return "authenticated_no_internet", self.retry_interval
        except PortalUnavailable as error:
            self.log(f"认证服务器不可达，等待链路/路由恢复：{error}")
            return "portal_unreachable", self.retry_interval
        except PortalError as error:
            self.log(f"认证未完成：{error}")
            return "authentication_failed", self.interval

    def diagnose(self):
        if self.boot_network:
            from uu_remote_monitor import service_status
            self.log(f"开机组件只读检查：已有登录会话={user_session_present()}；UU 服务={service_status()['status']}。")
        remote = UURemoteMonitor(self.log)
        if self.boot_network:
            remote.enabled = False
        if remote.enabled:
            try:
                running, updating = remote.inspect()
                self.log(f"网易UU远程：入口={remote.executable or '未找到'}；当前会话运行={running}；升级中={updating}")
            except (OSError, ValueError, subprocess.SubprocessError):
                self.log("网易UU远程：无法读取进程状态。")
        if self.failover is not None:
            # 本地只读诊断，不探测外网、不查询门户、不累计故障。
            from network_checks import system_proxy
            from wifi_control import wifi_snapshot
            config = self.failover.config
            snapshot = wifi_snapshot(config.interface)
            self.log(f"双网络模式：A={config.profile_a}；B={config.profile_b}；接口={config.interface}")
            if snapshot is None:
                self.log("无法读取 WLAN 状态；运行时会暂停切网。")
            else:
                self.log(f"当前配置={snapshot.profile or '未连接'}；无线电={snapshot.radio_on}；"
                         f"地址就绪={snapshot.link_ready}；IP 查询已知={snapshot.ip_known}；WLAN 默认出口={snapshot.default_route}")
                self.log(f"A/B 已保存：{all(p.casefold() in {s.casefold() for s in snapshot.profiles} for p in (config.profile_a, config.profile_b))}")
            if self.boot_network:
                self.log("开机模式不读取当前用户系统代理；用户登录后由用户监控接管。")
            else:
                endpoint = system_proxy()
                self.log(f"系统代理={endpoint.host + ':' + str(endpoint.port) if endpoint else '未开启或不受支持'}")
                self.log(f"代理间隔={config.interval} 秒；连续失败阈值={config.threshold}；冷却={config.cooldown} 秒。")
            self.log("此诊断未发起外网/门户请求；B 不执行网页登录认证。")
            return
        self.log(f"物理网卡具备 IPv4 默认路由：{physical_link(self.interface)}")
        try:
            client = self.client()
            self.log(f"门户可达：True；当前认证在线：{client.is_online(client.status())}")
        except PortalError as error:
            self.log(f"门户检查失败：{error}")
        self.log(f"直连外网可达：{internet_available(self.timeout)}")
        self.log(f"Wi-Fi 自动连接已配置：{bool(self.profile)}")

    def run(self):
        if self.boot_network:
            self.log("登录前网络组件已启动；用户登录后让出网络控制，独立守护 UU 系统服务。")
        if self.failover:
            config = self.failover.config
            self.log(f"Windows 双网络监控已启动：A={config.profile_a}；B={config.profile_b}；接口={config.interface}。")
            if not self.boot_network:
                self.log(f"代理检测间隔 {config.interval} 秒，连续失败 {config.threshold} 轮切换，冷却 {config.cooldown} 秒。")
        else:
            self.log(f"Windows 原生监控已启动，正常检测间隔 {self.interval} 秒，链路恢复重试 {self.retry_interval} 秒。")
            self.log(f"Wi-Fi 自动恢复配置：{self.profile or '未启用'}；物理接口：{self.interface or '自动检测'}。")
        if self.boot_network:
            self.log("开机模式：仅 A 检查门户认证，B 跳过认证；连接恢复连续失败时尝试备用网络，不依赖 Clash。")
        else:
            self.log("双网络模式已启用：仅 A 检查门户认证，B 跳过认证；代理连续失败后双向切换。"
                     if self.failover else "每轮检查门户认证状态；未认证或认证过期时自动重新提交账号密码。")
        remote = UUServiceMonitor(self.log) if self.boot_network else UURemoteMonitor(self.log)
        stop_event = threading.Event()
        if remote.enabled:
            threading.Thread(target=remote.run, args=(stop_event,), name="uu-remote-monitor", daemon=True).start()
        try:
            while True:
                try:
                    _, delay = self.coordinated_check()
                except Exception as error:
                    if self.failover:
                        self.failover.failures = 0
                    self.log(f"监控异常：{type(error).__name__}，下一周期重试。")
                    delay = self.retry_interval
                time.sleep(delay)
        except KeyboardInterrupt:
            self.log("监控已停止。")
        finally:
            stop_event.set()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(__file__).with_name(".env"))
    parser.add_argument("--wifi-profile", help="自动连接的已保存 Wi-Fi 配置名称，例如 ZJUWLAN")
    parser.add_argument("--interface", help="仅检查/连接指定物理接口，例如 WLAN")
    parser.add_argument("--diagnose", action="store_true", help="只读诊断，不登录或重连 Wi-Fi")
    parser.add_argument("--once", action="store_true", help="只执行一个检测/恢复周期")
    parser.add_argument("--boot-network", action="store_true", help="登录前网络恢复和 UU 服务守护；登录后让出网络控制")
    parser.add_argument("--log-file", type=Path, help="追加 UTF-8 日志（供受保护的开机任务使用）")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("此项目只支持 Windows 原生运行")
    if args.boot_network:
        # Session 0 不能从用户可写的 PATH 加载可执行程序或 DLL。
        root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        os.environ["PATH"] = os.pathsep.join((str(root / "System32"),
                                            str(root / "System32/WindowsPowerShell/v1.0"), str(root)))
    load_env(args.env_file)
    if args.log_file:
        if args.log_file.exists() and args.log_file.stat().st_size > 5 * 1024 * 1024:
            args.log_file.replace(args.log_file.with_suffix(".log.previous"))
        sys.stdout = sys.stderr = args.log_file.open("a", encoding="utf-8", buffering=1)
    try:
        monitor = HostMonitor(args.wifi_profile if args.wifi_profile is not None else os.environ.get("ZJU_WIFI_PROFILE", ""),
                               args.interface if args.interface is not None else os.environ.get("ZJU_WIFI_INTERFACE", ""),
                               boot_network=args.boot_network)
    except ValueError:
        parser.error("网络监控配置无效；检查双网络配置、接口、HTTPS 探测地址及整数参数。")
    if args.diagnose:
        monitor.diagnose()
    elif args.once:
        state, _ = monitor.coordinated_check()
        return 0 if state in ("online", "boot_online", "user_session") else 1
    else:
        lock = Path(__file__).with_name(".boot.instance.lock" if args.boot_network else ".user.instance.lock")
        with FileLease(lock) as lease:
            if not lease.acquired:
                monitor.log("本模式监控已有实例运行，退出重复启动。")
                return 0
            monitor.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
