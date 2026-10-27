"""模拟认证过期：真实注销当前账号，再由 Windows 原生监控提交账密并验证。"""
from pathlib import Path
import hashlib
import os
import sys
import time
from unittest.mock import patch

from host_reconnect import HostMonitor, load_env, physical_link
from network_checks import internet_available
from srun_client import SrunClient


def main():
    if sys.platform != "win32":
        raise SystemExit("此测试应在 Windows 宿主机运行。")
    sys.stdout.reconfigure(encoding="utf-8")
    load_env(Path(__file__).with_name(".env"))
    monitor = HostMonitor(os.environ.get("ZJU_WIFI_PROFILE", ""), os.environ.get("ZJU_WIFI_INTERFACE", ""))
    if not monitor.username or not monitor.password:
        raise SystemExit("账号密码未配置，未执行注销。")
    if physical_link(monitor.interface) is not True:
        raise SystemExit("系统链路未就绪，未执行注销。")
    client = monitor.client()
    status = client.status()
    submitted = []
    try:
        if client.is_online(status):
            account = status.get("user_name") or status.get("username")
            domain = status.get("domain") or ""
            if monitor.username not in (account, account + domain):
                raise RuntimeError("当前在线账号与配置不同，未执行注销。")
            challenge = client.request("get_challenge", {"username": monitor.username}, origin=client.origin)
            ip = challenge.get("client_ip")
            if not ip:
                raise RuntimeError("未获得服务器识别的客户端 IP，未执行注销。")
            # ZJU 的 MacAuth 门户使用 DM 下线/解绑，而非普通 srun_portal logout。
            stamp, unbind = str(int(time.time())), "1"
            sign = hashlib.sha1((stamp + account + ip + unbind + stamp).encode()).hexdigest()
            result = client.request("rad_user_dm", {
                "username": account, "ip": ip, "time": stamp, "unbind": unbind, "sign": sign,
            }, origin=client.origin)
            if result.get("error") != "ok":
                raise RuntimeError("认证服务器未接受测试注销。")
        for _ in range(10):
            if not client.is_online(client.status()):
                break
            time.sleep(1)
        else:
            raise RuntimeError("未确认认证失效，未执行重登验证。")
        monitor.log("已真实注销当前认证，模拟 14 天到期后的未认证状态。")
        original_request = SrunClient.request

        def record_login(self, endpoint, params=None, origin=None):
            if endpoint == "srun_portal" and params and params.get("action") == "login":
                submitted.append(True)
            return original_request(self, endpoint, params, origin)

        with patch.object(SrunClient, "request", record_login):
            state, _ = monitor.check_once()
        verified_client = monitor.client()
        authenticated = verified_client.is_online(verified_client.status())
        available = internet_available(monitor.timeout)
        print(f"NATIVE EXPIRY TEST: password_login_submitted={bool(submitted)}, "
              f"authenticated={authenticated}, internet={available}", flush=True)
        return 0 if state == "online" and submitted and authenticated and available else 1
    finally:
        # 正常或失败退出均恢复认证，不依赖浏览器。
        monitor.client().reconnect(monitor.username, monitor.password)


if __name__ == "__main__":
    raise SystemExit(main())
