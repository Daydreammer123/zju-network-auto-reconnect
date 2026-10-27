"""A/B 双向故障切换状态机。所有连接操作串行，无后台切网线程。"""
from dataclasses import dataclass
import os
import time

from network_checks import (DEFAULT_PROXY_URLS, ProbeState, proxy_available,
                            validate_proxy_urls)
from wifi_control import connect_wifi, last_snapshot_error, wifi_snapshot


def failover_enabled():
    value = os.environ.get("ZJU_WIFI_FAILOVER", "0").strip()
    if value not in ("0", "1"):
        raise ValueError("ZJU_WIFI_FAILOVER 必须为 0 或 1")
    return value == "1"


def _seconds(name, default):
    value = int(os.environ.get(name, str(default)))
    if not 1 <= value <= 86400:
        raise ValueError(f"{name} 必须为 1 至 86400 的整数")
    return value


@dataclass(frozen=True)
class FailoverConfig:
    profile_a: str
    profile_b: str
    interface: str
    interval: int
    threshold: int
    timeout: int
    connect_timeout: int
    cooldown: int
    urls: tuple[str, ...]
    link_failure_threshold: int = 2

    @classmethod
    def from_env(cls, interface, proxy_required=True):
        a = os.environ.get("ZJU_WIFI_PROFILE_A", "ZJUWLAN").strip()
        b = os.environ.get("ZJU_WIFI_PROFILE_B", "ZJUWLAN-Secure").strip()
        if not a or not b or a.casefold() == b.casefold() or not interface.strip():
            raise ValueError("双网络模式需要指定接口和两个不同的已有 Wi-Fi 配置")
        threshold = int(os.environ.get("PROXY_FAILURE_THRESHOLD", "2")) if proxy_required else 2
        if not 1 <= threshold <= 10:
            raise ValueError("PROXY_FAILURE_THRESHOLD 必须为 1 至 10 的整数")
        raw_urls = os.environ.get("PROXY_CHECK_URLS", ",".join(DEFAULT_PROXY_URLS)) if proxy_required else ",".join(DEFAULT_PROXY_URLS)
        urls = validate_proxy_urls(url.strip() for url in raw_urls.split(",") if url.strip())
        return cls(a, b, interface.strip(), _seconds("PROXY_CHECK_INTERVAL" if proxy_required else "CHECK_INTERVAL", 60), threshold,
                   _seconds("PROXY_CHECK_TIMEOUT", 8) if proxy_required else 8, _seconds("WIFI_CONNECT_TIMEOUT", 30),
                   _seconds("WIFI_FAILOVER_COOLDOWN", 300), urls,
                   _seconds("WIFI_RESTORE_FAILURE_THRESHOLD", 2))


class WifiFailover:
    def __init__(self, config, log, authenticate):
        self.config = config
        self.log = log
        self.authenticate = authenticate
        self.last_profile = config.profile_a
        self.active_profile = ""
        self.unmanaged = False
        self.failures = 0
        self.switched = False
        self.pending = ""
        self.pending_kind = ""
        self.switch_source = ""
        self.rollback_due = ""
        self.link_retry_at = 0.0
        self.connect_deadline = 0.0
        self.cooldown_until = 0.0
        self.connection_cooldown_until = 0.0
        self.next_probe = 0.0
        self.probe_enabled = True
        self.restore_failures = {}
        self.restore_target = ""
        self.windows_deadline = None

    def _managed(self, profile):
        for candidate in (self.config.profile_a, self.config.profile_b):
            if profile.casefold() == candidate.casefold():
                return candidate
        return ""

    def _reset(self):
        self.failures = 0
        self.switched = False
        self.pending = ""
        self.pending_kind = ""
        self.switch_source = ""
        self.rollback_due = ""
        self.link_retry_at = 0.0
        self.cooldown_until = 0.0
        self.connection_cooldown_until = 0.0
        self.next_probe = 0.0
        self.restore_failures.clear()
        self.restore_target = ""
        self.windows_deadline = None

    def _restore_failed(self, profile, saved):
        count = self.restore_failures.get(profile, 0) + 1
        self.restore_failures[profile] = count
        self.pending = self.pending_kind = ""
        self.link_retry_at = time.monotonic() + self.config.connect_timeout
        other = self.config.profile_b if profile == self.config.profile_a else self.config.profile_a
        if (count >= self.config.link_failure_threshold
                and self.connection_cooldown_until <= time.monotonic()
                and other.casefold() in {name.casefold() for name in saved}):
            self.restore_target = other
            self.restore_failures[profile] = 0
            self.log(f"{profile} 连续恢复失败 {count} 次，限速后尝试备用网络 {other}；不依赖代理是否启动。")
        else:
            self.restore_target = profile
            self.log(f"{profile} 恢复失败 {count} 次，将限速重试；不会持续提交连接请求。")

    def _cooldown(self):
        self.pending = ""
        self.pending_kind = ""
        self.rollback_due = ""
        self.failures = 0
        self.switched = False
        self.cooldown_until = time.monotonic() + self.config.cooldown
        self.next_probe = time.monotonic() + self.config.interval
        self.log(f"本轮恢复未成功，暂停切换 {self.config.cooldown} 秒；期间继续检测当前网络。")

    def _rollback(self):
        """切换失败先恢复原连接；冷却只限制换网，不阻止断线恢复。"""
        source = self.switch_source
        if not self.rollback_due:
            self.connection_cooldown_until = time.monotonic() + self.config.cooldown
        self.pending = ""
        self.pending_kind = ""
        self.failures = 0
        self.switched = False
        latest = wifi_snapshot(self.config.interface)
        if latest is None or latest.radio_on is None:
            self.rollback_due = source
            self.log("切换失败，暂时无法确认无线状态；保留原网络回退任务，下一轮重试读取。")
            return "rollback_waiting", 5
        if not latest.radio_on:
            self._reset()
            return "radio_off", self.config.interval
        current = self._managed(latest.profile) if latest.connected else ""
        if latest.connected and not current:
            self._reset()
            self.unmanaged = True
            self.active_profile = ""
            self.log("回退前已连接其他 Wi-Fi，尊重用户操作，不覆盖当前连接。")
            return "unmanaged_wifi", self.config.interval
        if not source or source.casefold() not in {name.casefold() for name in latest.profiles}:
            self._cooldown()
            self.log("原网络配置不可用，无法安全回退。")
            return "rollback_failed", self.config.interval
        self.last_profile = self.active_profile = source
        if current and latest.link_ready:
            self.last_profile = self.active_profile = current
            self._cooldown()
            self.next_probe = 0.0
            self.log(f"当前网络 {current} 已连接，无需再次断开；先验证其认证和代理。")
            return "rollback_ready", 1
        self.log(f"目标网络连接失败，立即回退原网络 {source}，不等待五分钟冷却。")
        if not connect_wifi(source, self.config.interface):
            self._cooldown()
            self.link_retry_at = time.monotonic() + self.config.connect_timeout
            self.log("原网络回退请求失败，将限速重试原网络，不反复尝试失败的目标。")
            return "rollback_failed", min(self.config.interval, self.config.connect_timeout)
        self.pending = source
        self.pending_kind = "rollback"
        self.rollback_due = ""
        self.connect_deadline = time.monotonic() + self.config.connect_timeout
        self.cooldown_until = time.monotonic() + self.config.cooldown
        self.link_retry_at = 0.0
        return "rolling_back", min(5, self.config.connect_timeout)

    def _request(self, profile, switching):
        # 认证/代理探测可能耗时：再次读取，防止覆盖用户刚选择的其他网络。
        latest = wifi_snapshot(self.config.interface)
        if latest is None or latest.radio_on is None:
            self.failures = 0
            self.log("切换前无法确认无线状态，取消本次连接请求。")
            return "wifi_unknown", 5
        if not latest.ip_known:
            return "ip_unknown", 5
        if not latest.radio_on:
            self._reset()
            self.log("切换前无线电已关闭，取消本次连接请求。")
            return "radio_off", self.config.interval
        current = self._managed(latest.profile) if latest.connected else ""
        if latest.connected and not current:
            self._reset()
            self.unmanaged = True
            self.active_profile = ""
            self.log("探测期间已连接其他 Wi-Fi，取消本次切换。")
            return "unmanaged_wifi", self.config.interval
        recovering_failed_switch = (not latest.link_ready and profile == self.last_profile
                                     and self.connection_cooldown_until > time.monotonic())
        restoring_alternative = not latest.link_ready and profile == self.restore_target
        if ((switching and (not current or current != self.active_profile)) or
                (not switching and current and current != profile
                 and not recovering_failed_switch and not restoring_alternative)):
            self.failures = 0
            self.log("探测期间 A/B 连接状态变化，取消旧的切换请求。")
            return "wifi_changed", 5
        if switching and latest.link_ready and not latest.default_route:
            self.failures = 0
            self.log("探测期间默认出口变化，取消本次切换。")
            return "other_default_route", self.config.interval
        if profile.casefold() not in {name.casefold() for name in latest.profiles}:
            self.failures = 0
            return "missing_profile", self.config.interval
        if switching:
            self.switch_source = self.active_profile
        if not connect_wifi(profile, self.config.interface):
            self.log(f"连接配置 {profile} 的请求失败；不立即反复尝试。")
            if switching:
                return self._rollback()
            self._restore_failed(profile, latest.profiles)
            return "connect_failed", min(self.config.interval, self.config.connect_timeout)
        self.pending = profile
        self.pending_kind = "switch" if switching else "restore"
        self.connect_deadline = time.monotonic() + self.config.connect_timeout
        self.failures = 0
        if switching:
            self.switched = True
        self.log(f"已请求连接 {profile}，等待目标配置和 IPv4/网关就绪（等待窗口 {self.config.connect_timeout} 秒）。")
        return "switching" if switching else "reconnecting", min(5, self.config.connect_timeout)

    def check_once(self):
        config = self.config
        cycle_started = time.monotonic()
        snapshot = wifi_snapshot(config.interface)
        now = time.monotonic()
        if snapshot is None or snapshot.radio_on is None:
            self.failures = 0
            self.log("无线状态未知，暂停连接操作和故障累计。" + last_snapshot_error())
            return "wifi_unknown", 5
        if not snapshot.radio_on:
            self._reset()
            self.log("无线电已关闭，等待人工恢复；不自动开启无线电。")
            return "radio_off", config.interval

        profile = self._managed(snapshot.profile) if snapshot.connected else ""
        if snapshot.connected and not profile:
            self._reset()
            self.unmanaged = True
            self.active_profile = ""
            self.log("当前连接其他 Wi-Fi，暂停 A/B 管理。")
            return "unmanaged_wifi", config.interval
        if not profile and self.unmanaged:
            self.failures = 0
            return "unmanaged_wifi", config.interval
        if profile:
            self.unmanaged = False

        if not snapshot.ip_known:
            self.failures = 0
            self.log("WLAN 连接可读取，但 IP/路由查询失败，暂不重复连接。" + last_snapshot_error())
            return "ip_unknown", 5

        if snapshot.connecting and not self.pending:
            if self.windows_deadline is None:
                self.windows_deadline = now + config.connect_timeout
            if now < self.windows_deadline:
                return "waiting_windows", max(1, min(5, self.windows_deadline - now))
        else:
            self.windows_deadline = None

        if self.rollback_due:
            return self._rollback()

        saved = {item.casefold() for item in snapshot.profiles}
        if any(item.casefold() not in saved for item in (config.profile_a, config.profile_b)):
            self._reset()
            self.log("A/B 配置未全部保存在此接口，暂停自动恢复；不会创建配置或读取密钥。")
            return "missing_profile", config.interval

        completed_rollback = False
        if self.pending:
            if profile == self.pending and snapshot.link_ready:
                completed_rollback = self.pending_kind == "rollback"
                self.last_profile = self.active_profile = profile
                self.pending = ""
                self.pending_kind = ""
                self.link_retry_at = 0.0
                self.restore_failures.clear()
                self.restore_target = ""
                self.next_probe = 0.0
                self.log(f"{profile} 连接及地址已确认。")
            elif now >= self.connect_deadline:
                self.log("目标 Wi-Fi 未在期限内取得连接和地址。")
                if self.pending_kind == "switch":
                    return self._rollback()
                was_rollback = self.pending_kind == "rollback"
                failed_profile = self.pending
                if was_rollback:
                    self._cooldown()
                    self.link_retry_at = time.monotonic() + config.connect_timeout
                else:
                    self._restore_failed(failed_profile, snapshot.profiles)
                return ("rollback_failed" if was_rollback else "connect_timeout"), min(config.interval, config.connect_timeout)
            else:
                return "waiting_link", max(1, min(5, self.connect_deadline - now))

        if not profile or not snapshot.link_ready:
            self.failures = 0
            if self.link_retry_at > now:
                return "waiting_reconnect", min(config.interval, self.link_retry_at - now)
            restore_profile = (self.last_profile if self.connection_cooldown_until > now
                               else self.restore_target or profile or self.last_profile)
            return self._request(restore_profile, switching=False)

        if profile != self.active_profile:
            # 非程序切换（包括第一次启动）采用当前配置，不优先回 A。
            self._reset()
            self.active_profile = self.last_profile = profile
            self.log(f"接管当前网络 {profile}，不主动回切。")

        if not snapshot.default_route:
            self.failures = 0
            self.log("当前默认出口不在指定 WLAN，暂停代理故障切网，避免干预其他出口。")
            return "other_default_route", config.interval
        if now < self.next_probe:
            return "waiting_probe", max(1, self.next_probe - now)

        # 以本轮开始时间安排下一轮；耗时超过间隔时不补跑并发任务。
        self.next_probe = cycle_started + config.interval
        if profile == config.profile_a:
            authenticated = self.authenticate()
            if not self.probe_enabled and not authenticated:
                self.log("开机网络恢复：A 认证尚未完成，稍后继续尝试。")
                self.next_probe = time.monotonic() + min(10, config.interval)
                return "boot_auth_pending", min(10, config.interval)
        if not self.probe_enabled:
            self.log(f"开机网络恢复：{profile} 链路及地址就绪；不读取系统代理，不启动桌面程序。")
            return "boot_online", config.interval
        # B 分支不创建门户客户端，也不查询认证服务器。
        result = proxy_available(config.urls, config.timeout)
        if result.state == ProbeState.ONLINE:
            self.failures = 0
            self.switched = False
            self.cooldown_until = 0.0
            self.log(f"{profile}：代理正常，保留当前连接。")
            state = "online"
        elif result.state in (ProbeState.UNKNOWN, ProbeState.UNAVAILABLE):
            self.failures = 0
            self.log(f"暂停代理故障累计：{result.reason}")
            state = "proxy_unavailable" if result.state == ProbeState.UNAVAILABLE else "proxy_unknown"
        else:
            self.failures = min(self.failures + 1, config.threshold)
            self.log(f"{profile}：代理连接失败 {self.failures}/{config.threshold}；{result.reason}")
            state = "proxy_failed"
            if max(self.cooldown_until, self.connection_cooldown_until) > time.monotonic():
                state = "cooldown"
            elif self.failures >= config.threshold:
                if self.switched:
                    self._cooldown()
                    state = "cooldown"
                else:
                    target = config.profile_b if profile == config.profile_a else config.profile_a
                    return self._request(target, switching=True)
        if completed_rollback:
            state = "rollback_online" if result.state == ProbeState.ONLINE else "rollback_no_proxy"
        return state, max(1, self.next_probe - time.monotonic())
