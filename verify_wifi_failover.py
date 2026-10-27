"""显式授权后验收：模拟两次探测失败，真实切换 A/B，实际验证 HTTPS 恢复。"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import patch

from host_reconnect import HostMonitor, load_env
from network_checks import ProbeResult, ProbeState, _proxy_target, system_proxy
from uu_remote_monitor import UURemoteMonitor
import wifi_failover
from wifi_control import connect_wifi, wifi_snapshot


class VerificationError(RuntimeError):
    pass


def event(**fields):
    print(json.dumps(fields, ensure_ascii=False), flush=True)


def require_exclusive_monitor():
    """避免和生产监控竞争；仅检查本项目入口，不停止其他程序。"""
    script = """
    $ErrorActionPreference = 'Stop'
    $entry = [System.IO.Path]::Combine($env:ZJU_VERIFY_PROJECT, 'host_reconnect.py')
    $workers = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -in @('python.exe','pythonw.exe') -and $_.CommandLine -and
        $_.CommandLine.IndexOf($entry, [StringComparison]::OrdinalIgnoreCase) -ge 0
    })
    if ($workers.Count) { exit 2 }
    """
    env = dict(os.environ, ZJU_VERIFY_PROJECT=str(Path(__file__).resolve().parent))
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, timeout=15, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise VerificationError("先停止本项目生产监控，再运行验收")


def check_targets(config):
    endpoint = system_proxy()
    if endpoint is None:
        raise VerificationError("系统代理未就绪")
    with ThreadPoolExecutor(max_workers=len(config.urls)) as pool:
        results = list(pool.map(lambda url: _proxy_target(endpoint, url, config.timeout), config.urls))
    rows = [{"target": url, "state": result.state.value, "reason": result.reason}
            for url, result in zip(config.urls, results)]
    event(real_https=rows)
    if not any(result.state == ProbeState.ONLINE for result in results):
        raise VerificationError("真实代理 HTTPS 未恢复")
    return rows


def verify_leg(config, source, target):
    monitor = HostMonitor(interface=config.interface)
    machine = monitor.failover
    real_probe = wifi_failover.proxy_available
    injected = []
    probe_times = []
    requests = []
    auth_calls = []
    real_auth = machine.authenticate

    def authenticate():
        auth_calls.append(time.monotonic())
        return real_auth()

    def probe(*args):
        if len(injected) < config.threshold:
            injected.append(round_started)
            probe_times.append(time.monotonic())
            return ProbeResult(ProbeState.FAILED, "验收故障注入，未修改 Clash")
        return real_probe(*args)

    def connect(profile, interface):
        # 目标失败时允许状态机回退原网络一次，不能让验收 guard 阻止安全恢复。
        permitted = (not requests and profile == target) or (requests == [target] and profile == source)
        if not permitted:
            raise VerificationError("验收禁止额外连接请求")
        requests.append(profile)
        return connect_wifi(profile, interface)

    machine.authenticate = authenticate
    started = time.monotonic()
    deadline = started + 180
    with patch.object(wifi_failover, "proxy_available", side_effect=probe), \
            patch.object(wifi_failover, "connect_wifi", side_effect=connect):
        while time.monotonic() < deadline:
            round_started = time.monotonic()
            state, delay = monitor.check_once()
            event(source=source, target=target, state=state,
                  injected_failures=len(injected), elapsed_seconds=round(time.monotonic() - started, 1))
            if len(injected) == 1 and requests:
                raise VerificationError("第一次失败就切换，验收失败")
            if state in ("connect_failed", "connect_timeout", "unmanaged_wifi", "radio_off",
                         "rollback_failed", "rollback_ready", "rollback_online", "rollback_no_proxy"):
                raise VerificationError("连接失败或用户状态变化，终止验收")
            if state == "online":
                snapshot = wifi_snapshot(config.interface)
                if (not snapshot or not snapshot.connected or snapshot.profile != target
                        or len(injected) != config.threshold or requests != [target]):
                    raise VerificationError("目标配置/切换次数不符合预期")
                if len(injected) > 1 and injected[1] - injected[0] < config.interval - 1:
                    raise VerificationError("两次故障检测间隔不足")
                # B 故障期间及 A->B 完成后，A 回调数取决于源网络；门户仅应在 A 调用。
                if source == config.profile_b and len(auth_calls) != 1:
                    raise VerificationError("B->A 认证回调次数异常")
                if source == config.profile_a and len(auth_calls) != config.threshold:
                    raise VerificationError("A->B 后仍调用 A 认证回调")
                return {"source": source, "target": target, "passed": True,
                        "injected_failures": len(injected), "real_connection_requests": requests,
                        "failure_interval_seconds": round(injected[1] - injected[0], 2),
                        "probe_interval_seconds": round(probe_times[1] - probe_times[0], 2),
                        "authentication_checks_on_a": len(auth_calls),
                        "real_https": check_targets(config),
                        "elapsed_seconds": round(time.monotonic() - started, 1)}
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(max(1, delay), remaining))
    raise VerificationError("单向验收超过三分钟时限")


def restore_if_needed(config, preferred):
    """保留当前可用网络；否则优先最近实际验证成功的网络，而不是盲试初始网络。"""
    def user_changed(snapshot):
        return (not snapshot or snapshot.radio_on is not True or
                (snapshot.connected and snapshot.profile not in (config.profile_a, config.profile_b)))

    def validate(snapshot):
        if not snapshot or not snapshot.connected or not snapshot.link_ready or not snapshot.default_route:
            return False
        if snapshot.profile == config.profile_a:
            HostMonitor(interface=config.interface).ensure_authenticated()
        try:
            check_targets(config)
            return True
        except VerificationError:
            return False

    snapshot = wifi_snapshot(config.interface)
    if user_changed(snapshot):
        event(restore="skipped_to_respect_user_or_unknown_state")
        return {"restored": False, "reason": "user_or_unknown_state"}
    if validate(snapshot):
        event(restore="current_network_verified", profile=snapshot.profile)
        return {"restored": True, "profile": snapshot.profile}
    other = config.profile_b if preferred == config.profile_a else config.profile_a
    for target in (preferred, other):
        snapshot = wifi_snapshot(config.interface)
        if user_changed(snapshot):
            return {"restored": False, "reason": "user_or_unknown_state"}
        if target.casefold() not in {name.casefold() for name in snapshot.profiles}:
            continue
        event(restore_requested=target)
        if not connect_wifi(target, config.interface):
            continue
        deadline = time.monotonic() + config.connect_timeout
        while time.monotonic() < deadline:
            snapshot = wifi_snapshot(config.interface)
            if user_changed(snapshot):
                return {"restored": False, "reason": "user_or_unknown_state"}
            if snapshot.profile == target and validate(snapshot):
                event(restore="confirmed", profile=target)
                return {"restored": True, "profile": target}
            time.sleep(2)
        event(restore="target_not_confirmed", profile=target)
    event(restore="failed_both_saved_profiles")
    return {"restored": False, "reason": "both_failed"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-wifi-switch", action="store_true", help="明确允许短暂断网和真实 A/B 切换")
    parser.add_argument("--report", type=Path, default=Path(__file__).with_name("wifi_failover_verification.json"))
    args = parser.parse_args()
    if not args.allow_wifi_switch or sys.platform != "win32":
        parser.error("仅 Windows；必须显式指定 --allow-wifi-switch")
    sys.stdout.reconfigure(encoding="utf-8")
    load_env(Path(__file__).with_name(".env"))
    require_exclusive_monitor()
    monitor = HostMonitor(interface=os.environ.get("ZJU_WIFI_INTERFACE", ""))
    if monitor.failover is None:
        parser.error("请先配置并启用双网络模式")
    config = monitor.failover.config
    if config.threshold != 2:
        parser.error("此验收要求连续失败阈值为 2")
    snapshot = wifi_snapshot(config.interface)
    if (not snapshot or not snapshot.connected or not snapshot.link_ready or not snapshot.default_route
            or snapshot.radio_on is not True or snapshot.profile not in (config.profile_a, config.profile_b)):
        raise VerificationError("初始 A/B 无线连接或默认路由未就绪，未发起切网")
    original = snapshot.profile
    other = config.profile_b if original == config.profile_a else config.profile_a
    check_targets(config)
    stop_event = threading.Event()
    remote = UURemoteMonitor(monitor.log)
    if remote.enabled:
        threading.Thread(target=remote.run, args=(stop_event,), daemon=True, name="verification-uu-monitor").start()
    report = {"time": datetime.now().astimezone().isoformat(), "initial_profile": original,
              "failure_mode": "test_scoped_probe_injection", "legs": []}
    last_working = original
    try:
        report["legs"].append(verify_leg(config, original, other))
        last_working = other
        report["legs"].append(verify_leg(config, other, original))
        report["passed"] = True
        event(verification="passed", final_profile=original)
    except (Exception, KeyboardInterrupt) as error:
        report["passed"] = False
        report["error_type"] = type(error).__name__
        event(verification="failed", error_type=type(error).__name__)
        try:
            report["recovery"] = restore_if_needed(config, last_working)
        except Exception as recovery_error:
            report["recovery"] = {"restored": False, "error_type": type(recovery_error).__name__}
            event(restore="failed", error_type=type(recovery_error).__name__)
    finally:
        stop_event.set()
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
