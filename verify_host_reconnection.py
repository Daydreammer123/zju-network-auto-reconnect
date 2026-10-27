"""真实 Windows Wi-Fi 断开/恢复测试：仅在用户同意后运行，使用已保存的配置。"""
import argparse
from pathlib import Path
import subprocess
import sys
import threading
import time

from host_reconnect import HostMonitor, connect_wifi, load_env, physical_link
from network_checks import internet_available


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wifi-profile", required=True)
    parser.add_argument("--interface", required=True)
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("只能在 Windows 宿主机运行")
    sys.stdout.reconfigure(encoding="utf-8")
    load_env(Path(__file__).with_name(".env"))
    monitor = HostMonitor(args.wifi_profile, args.interface)
    if physical_link(args.interface) is not True:
        raise SystemExit("指定接口当前未连接；未执行断开测试。")

    # 即使主测试在检测期间异常，独立定时器也会请求恢复现有 Wi-Fi 配置。
    restore = threading.Timer(45, connect_wifi, args=(args.wifi_profile, args.interface))
    restore.daemon = True
    restore.start()
    try:
        result = subprocess.run(["netsh.exe", "wlan", "disconnect", "interface=" + args.interface],
                                capture_output=True, timeout=10)
        if result.returncode:
            raise RuntimeError("Windows 拒绝断开指定无线接口")
        deadline = time.monotonic() + 15
        while physical_link(args.interface) is not False:
            if time.monotonic() >= deadline:
                raise RuntimeError("未确认系统网络实际断开")
            time.sleep(1)
        monitor.log("已确认真实 Wi-Fi 断开，开始验证系统连接恢复。")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            state, _ = monitor.check_once()
            if state == "online":
                break
            time.sleep(monitor.retry_interval)
        else:
            raise RuntimeError("系统网络未在测试时限内恢复")
        client = monitor.client()
        authenticated = client.is_online(client.status())
        available = internet_available(monitor.timeout)
        linked = physical_link(args.interface)
        print(f"HOST RECONNECTION TEST: wifi_disconnected=True, wifi_restored={linked}, "
              f"authenticated={authenticated}, internet={available}", flush=True)
        return 0 if linked and authenticated and available else 1
    finally:
        connect_wifi(args.wifi_profile, args.interface)
        restore.cancel()
        restore.join(timeout=12)


if __name__ == "__main__":
    raise SystemExit(main())
