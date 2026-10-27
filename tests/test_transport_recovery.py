import io
import json
import os
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

import host_reconnect as host
from network_checks import internet_available
from srun_client import PortalError, PortalUnavailable, SrunClient, http_reason


class TransportRecoveryTests(unittest.TestCase):
    def test_entire_portal_path_unavailable_matches_real_disconnect_log(self):
        client = SrunClient(["https://net.zju.edu.cn", "http://net.zju.edu.cn"], portal_ip="10.50.254.11")

        def unavailable(request, **kwargs):
            url = request.full_url
            if url.startswith("https:"):
                raise URLError(ConnectionResetError())
            if urlsplit(url).hostname == "net.zju.edu.cn":
                raise HTTPError(url, 403, "Forbidden", {}, io.BytesIO(b"dial tcp: lookup net.zju.edu.cn: no such host"))
            raise HTTPError(url, 502, "Bad Gateway", {}, io.BytesIO(b"network is unreachable"))

        with patch.object(client.opener, "open", side_effect=unavailable):
            with self.assertRaisesRegex(PortalUnavailable, "上游网络/路由不可达"):
                client.status()

    def test_unavailable_portal_retries_link_quickly_in_native_monitor(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"ZJU_WIFI_FAILOVER": "0"}):
            monitor = host.HostMonitor()
        client = Mock()
        client.status.side_effect = PortalUnavailable("HTTP 502")
        with patch.object(host, "physical_link", return_value=True), \
                patch.object(monitor, "client", return_value=client), \
                patch.object(host, "internet_available") as probe, redirect_stdout(output):
            self.assertEqual(monitor.check_once(), ("portal_unreachable", monitor.retry_interval))
        probe.assert_not_called()
        self.assertIn("等待链路/路由恢复", output.getvalue())
        self.assertNotIn("登录未完成", output.getvalue())

    def test_api_format_error_is_not_misclassified_as_transport_failure(self):
        client = SrunClient(["http://net.zju.edu.cn"])
        with patch.object(client.opener, "open", return_value=io.BytesIO(b"<html>Login</html>")):
            with self.assertRaises(PortalError) as result:
                client.status()
        self.assertNotIsInstance(result.exception, PortalUnavailable)

    def test_fixed_ip_is_added_only_for_default_portal_with_http_enabled(self):
        urls = ["https://net.zju.edu.cn/", "http://net.zju.edu.cn/srun_portal_pc"]
        client = SrunClient(urls, portal_ip="10.50.254.11")
        self.assertEqual(client.origins[-1], "http://10.50.254.11")
        self.assertIsNone(client.acid)
        self.assertNotIn("http://10.50.254.11", SrunClient(urls[:1], portal_ip="10.50.254.11").origins)
        self.assertNotIn("http://10.50.254.11", SrunClient(["http://example.com"], portal_ip="10.50.254.11").origins)

    def test_proxy_dns_error_is_classified_without_printing_body(self):
        error = HTTPError("http://portal/?password=secret", 403, "Forbidden", {}, io.BytesIO(
            b"dial tcp: lookup net.zju.edu.cn: no such host; secret"))
        reason = http_reason(error)
        self.assertIn("中间转发层 DNS", reason)
        self.assertNotIn("secret", reason)

    def test_real_http_exchange_recovers_from_proxy_403_and_discovers_region(self):
        # 使用真实 HTTP 服务验证 urllib 的 HTTPError、JSONP、页面解析及认证后查询。
        state = {"online": False, "parameters": None}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlsplit(self.path)
                if parsed.path.startswith("/proxy/"):
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(b"dial tcp: lookup net.zju.edu.cn: no such host")
                    return
                if parsed.path == "/srun_portal_pc":
                    body = b'<script>var CONFIG = { acid: "82", ip: "192.0.2.20" };</script>'
                else:
                    query = parse_qs(parsed.query)
                    if parsed.path.endswith("rad_user_info"):
                        data = ({"error": "ok", "user_name": "sample-account"}
                                if state["online"] else {"error": "not_online_error"})
                    elif parsed.path.endswith("get_challenge"):
                        data = {"error": "ok", "challenge": "0123456789abcdef", "client_ip": "192.0.2.20"}
                    else:
                        state["parameters"] = query
                        state["online"] = query.get("ac_id") == ["82"] and query.get("ip") == ["192.0.2.20"]
                        data = {"error": "ok" if state["online"] else "login_error"}
                    body = ("automatic_reconnection(" + json.dumps(data) + ");").encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        origin = f"http://127.0.0.1:{server.server_port}"
        client = SrunClient([origin])
        client.origins = [origin + "/proxy", origin]
        try:
            self.assertTrue(client.reconnect("sample-account", "dummy-password"))
            self.assertEqual(client.origin, origin)
            self.assertEqual(state["parameters"]["ac_id"], ["82"])
            self.assertNotIn("dummy-password", json.dumps(state["parameters"]))
        finally:
            server.shutdown()
            server.server_close()

    def test_missing_region_fails_instead_of_silently_using_one(self):
        client = SrunClient(["http://net.zju.edu.cn"])
        with patch.object(client.opener, "open", return_value=io.BytesIO(b"<html>proxy error</html>")):
            with self.assertRaisesRegex(PortalError, "无法自动识别认证区域"):
                client.discover_acid()

    def test_explicit_region_skips_discovery(self):
        client = SrunClient(["http://net.zju.edu.cn/?ac_id=82"], acid="9")
        self.assertEqual(client.acid, "9")

    def test_probe_rejects_captive_redirect_and_portal_200(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/portal")
                else:
                    self.send_response(204 if self.path == "/online" else 200)
                self.end_headers()
                if self.path == "/portal":
                    self.wfile.write(b'<button id="login-account">Login</button>')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        origin = f"http://127.0.0.1:{server.server_port}"
        try:
            for path, expected in (("/redirect", False), ("/portal", False), ("/online", True)):
                with patch.dict(os.environ, {"NETWORK_CHECK_URLS": origin + path}):
                    self.assertEqual(internet_available(), expected)
        finally:
            server.shutdown()
            server.server_close()

    def test_monitor_does_not_report_internet_recovery_from_authentication_alone(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"ZJU_WIFI_FAILOVER": "0"}):
            monitor = host.HostMonitor()
        client = Mock()
        client.is_online.return_value = True
        with patch.object(host, "physical_link", return_value=True), \
                patch.object(monitor, "client", return_value=client), \
                patch.object(host, "internet_available", return_value=False), redirect_stdout(output):
            self.assertEqual(monitor.check_once()[0], "authenticated_no_internet")
        self.assertIn("已认证，但外网不可达", output.getvalue())
        self.assertNotIn("网络正常", output.getvalue())


if __name__ == "__main__":
    unittest.main()
