import http.client
import os
import ssl
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

import network_checks as checks
from network_checks import ProbeResult, ProbeState, ProxyEndpoint


class ProxyConfigurationTests(unittest.TestCase):
    def test_local_mixed_proxy_formats(self):
        for value in ("127.0.0.1:7897", "http://127.0.0.1:7897", "http=127.0.0.1:7890;https=127.0.0.1:7897"):
            self.assertEqual(checks.parse_proxy_server(value), ProxyEndpoint("127.0.0.1", 7897))
        self.assertEqual(checks.parse_proxy_server("[::1]:7897"), ProxyEndpoint("::1", 7897))

    def test_reject_remote_proxy_credentials_missing_https_and_invalid_port(self):
        for value in ("example.com:7897", "192.168.1.1:7897", "127.0.0.1:0", "127.0.0.1:65536",
                      "http=127.0.0.1:7897", "socks5://127.0.0.1:7897", "user:pass@127.0.0.1:7897", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                checks.parse_proxy_server(value)

    def test_target_validation(self):
        for urls in (("https://one.example/",), ("http://one.example/", "https://two.example/"),
                     ("https://one.example/a", "https://one.example/b"),
                     ("https://user:pass@one.example/", "https://two.example/"),
                     ("https://one.example:444/", "https://two.example/")):
            with self.subTest(urls=urls), self.assertRaises(ValueError):
                checks.validate_proxy_urls(urls)

    def test_system_proxy_registry_is_read_only_and_handles_disabled_pac_and_bad_config(self):
        registry = types.SimpleNamespace(HKEY_CURRENT_USER=object(), OpenKey=MagicMock(), QueryValueEx=MagicMock())
        for values, expected in (({"ProxyEnable": 1, "ProxyServer": "127.0.0.1:7897"}, ProxyEndpoint("127.0.0.1", 7897)),
                                 ({"ProxyEnable": 0, "ProxyServer": "127.0.0.1:7897"}, None),
                                 ({"ProxyEnable": 1, "AutoConfigURL": "https://pac.example/", "ProxyServer": "127.0.0.1:7897"}, None),
                                 ({"ProxyEnable": 1, "ProxyServer": "not-a-valid-proxy"}, None),
                                 ({}, None)):
            def query(key, name):
                if name not in values:
                    raise FileNotFoundError(name)
                return values[name], 1

            with self.subTest(values=values), patch.dict(checks.sys.modules, {"winreg": registry}), \
                    patch.object(checks.sys, "platform", "win32"):
                registry.QueryValueEx.side_effect = query
                self.assertEqual(checks.system_proxy(), expected)


class TargetProbeTests(unittest.TestCase):
    def setUp(self):
        self.connection = MagicMock()
        self.response = self.connection.getresponse.return_value.__enter__.return_value
        self.response.status = 204
        self.factory = patch.object(checks.http.client, "HTTPSConnection", return_value=self.connection).start()
        self.addCleanup(patch.stopall)
        self.endpoint = ProxyEndpoint("127.0.0.1", 7897)

    def probe(self):
        return checks._proxy_target(self.endpoint, "https://target.example/check?a=1", 8)

    def test_explicit_connect_tunnel_and_tls_verification_ignore_proxy_environment(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://wrong.example:1234", "NO_PROXY": "*"}):
            self.assertEqual(self.probe().state, ProbeState.ONLINE)
        self.connection.set_tunnel.assert_called_once_with("target.example", 443)
        self.connection.request.assert_called_once_with("GET", "/check?a=1", headers={"User-Agent": "ZJU-Network-Monitor/1.0"})
        self.assertEqual(self.factory.call_args.args, ("127.0.0.1", 7897))
        self.assertTrue(self.factory.call_args.kwargs["context"].check_hostname)
        self.assertEqual(self.factory.call_args.kwargs["context"].verify_mode, ssl.CERT_REQUIRED)
        self.connection.close.assert_called_once()

    def test_http_statuses_are_not_network_failures(self):
        for status in (301, 302, 403, 407, 429, 500, 502, 503):
            with self.subTest(status=status):
                self.response.status = status
                self.assertEqual(self.probe().state, ProbeState.UNKNOWN)

    def test_captive_portal_body_is_not_success(self):
        self.response.status = 200
        self.response.read.return_value = b'<button id="login-account">'
        self.assertEqual(self.probe().state, ProbeState.UNKNOWN)

    def test_transport_errors_are_failures_and_no_secrets_are_logged(self):
        for error in (TimeoutError("secret-url"), ConnectionResetError("secret-url"),
                      ssl.SSLError("secret-url"), OSError("Tunnel connection failed: 502 Bad Gateway")):
            with self.subTest(error=type(error)):
                self.connection.request.side_effect = error
                result = self.probe()
                self.assertEqual(result.state, ProbeState.FAILED)
                self.assertNotIn("secret", result.reason)

    def test_proxy_refusal_and_certificate_error_are_unknown(self):
        for error in (OSError("Tunnel connection failed: 403 Forbidden"),
                      OSError("Tunnel connection failed: 407 Auth Required"),
                      ssl.SSLCertVerificationError("bad certificate"), http.client.BadStatusLine("bad")):
            with self.subTest(error=type(error)):
                self.connection.request.side_effect = error
                self.assertEqual(self.probe().state, ProbeState.UNKNOWN)


class AggregateProbeTests(unittest.TestCase):
    def setUp(self):
        self.proxy = patch.object(checks, "system_proxy", return_value=ProxyEndpoint("127.0.0.1", 7897)).start()
        self.local = patch.object(checks.socket, "create_connection").start()
        self.target = patch.object(checks, "_proxy_target").start()
        self.addCleanup(patch.stopall)

    def test_one_success_is_sufficient_but_all_must_fail_to_count_failure(self):
        for states, expected in (((ProbeState.ONLINE, ProbeState.FAILED), ProbeState.ONLINE),
                                 ((ProbeState.FAILED, ProbeState.FAILED), ProbeState.FAILED),
                                 ((ProbeState.FAILED, ProbeState.UNKNOWN), ProbeState.UNKNOWN)):
            with self.subTest(states=states):
                self.target.side_effect = [ProbeResult(state, "test") for state in states]
                self.assertEqual(checks.proxy_available().state, expected)

    def test_missing_system_proxy_does_not_connect_or_request(self):
        self.proxy.return_value = None
        self.assertEqual(checks.proxy_available().state, ProbeState.UNAVAILABLE)
        self.local.assert_not_called()
        self.target.assert_not_called()

    def test_absent_local_listener_does_not_probe_targets(self):
        self.local.side_effect = ConnectionRefusedError()
        self.assertEqual(checks.proxy_available().state, ProbeState.UNAVAILABLE)
        self.target.assert_not_called()

    def test_two_targets_execute_concurrently(self):
        barrier = threading.Barrier(2)

        def target(*args):
            barrier.wait(timeout=2)
            return ProbeResult(ProbeState.ONLINE, "ok")

        self.target.side_effect = target
        self.assertEqual(checks.proxy_available().state, ProbeState.ONLINE)

    def test_clash_exits_during_failed_probes_is_not_network_failure(self):
        self.local.side_effect = [MagicMock(), ConnectionRefusedError()]
        self.target.return_value = ProbeResult(ProbeState.FAILED, "timeout")
        self.assertEqual(checks.proxy_available().state, ProbeState.UNAVAILABLE)

    def test_system_proxy_changed_during_failed_probes_is_not_network_failure(self):
        self.proxy.side_effect = [ProxyEndpoint("127.0.0.1", 7897), None]
        self.target.return_value = ProbeResult(ProbeState.FAILED, "timeout")
        self.assertEqual(checks.proxy_available().state, ProbeState.UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
