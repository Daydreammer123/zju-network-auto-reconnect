import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import host_reconnect as host
from srun_client import PortalUnavailable


class HostReconnectTests(unittest.TestCase):
    def test_disconnected_system_does_not_submit_authentication(self):
        monitor = host.HostMonitor()
        with patch.object(host, "physical_link", return_value=False), \
                patch.object(host, "internet_available") as internet, \
                patch.object(monitor, "client") as client, patch.object(monitor, "log"):
            self.assertEqual(monitor.check_once(), ("link_down", monitor.retry_interval))
        internet.assert_not_called()
        client.assert_not_called()

    def test_saved_profile_connection_is_requested_before_authentication(self):
        monitor = host.HostMonitor(wifi_profile="ZJUWLAN", interface="WLAN")
        with patch.object(host, "physical_link", return_value=False), \
                patch.object(host, "connect_wifi", return_value=True) as wifi, \
                patch.object(monitor, "client") as client, patch.object(monitor, "log"):
            self.assertEqual(monitor.check_once()[0], "link_down")
        wifi.assert_called_once_with("ZJUWLAN", "WLAN")
        client.assert_not_called()

    def test_wifi_restoration_then_real_authentication_state_machine(self):
        monitor = host.HostMonitor(wifi_profile="ZJUWLAN")
        monitor.username, monitor.password = "sample-account", "dummy-password"
        client = Mock()
        status = {"error": "not_online_error"}
        client.status.return_value = status
        client.is_online.return_value = False
        client.reconnect.return_value = True
        with patch.object(host, "physical_link", side_effect=[False, True]), \
                patch.object(host, "connect_wifi", return_value=True), \
                patch.object(host, "internet_available", return_value=True), \
                patch.object(monitor, "client", return_value=client), patch.object(monitor, "log"):
            self.assertEqual(monitor.check_once()[0], "link_down")
            client.reconnect.assert_not_called()
            self.assertEqual(monitor.check_once(), ("online", monitor.interval))
        client.reconnect.assert_called_once_with("sample-account", "dummy-password", initial_status=status)

    def test_gateway_failure_is_not_treated_as_password_failure(self):
        monitor = host.HostMonitor()
        client = Mock()
        client.status.side_effect = PortalUnavailable("HTTP 502")
        with patch.object(host, "physical_link", return_value=True), \
                patch.object(host, "internet_available", return_value=False), \
                patch.object(monitor, "client", return_value=client), patch.object(monitor, "log"):
            self.assertEqual(monitor.check_once(), ("portal_unreachable", monitor.retry_interval))
        client.reconnect.assert_not_called()

    def test_unknown_link_status_still_allows_recovery(self):
        monitor = host.HostMonitor()
        client = Mock()
        client.is_online.return_value = True
        with patch.object(host, "physical_link", return_value=None), \
                patch.object(host, "internet_available", return_value=True), \
                patch.object(monitor, "client", return_value=client), patch.object(monitor, "log"):
            self.assertEqual(monitor.check_once()[0], "online")

    def test_expired_authentication_is_renewed_even_when_an_external_probe_would_pass(self):
        monitor = host.HostMonitor()
        monitor.username, monitor.password = "sample-account", "dummy-password"
        client = Mock()
        status = {"error": "not_online_error"}
        client.status.return_value = status
        client.is_online.return_value = False
        events = []
        client.status.side_effect = lambda: events.append("status") or status
        client.reconnect.side_effect = lambda *args, **kwargs: events.append("login") or True
        with patch.object(host, "physical_link", return_value=True), \
                patch.object(host, "internet_available", side_effect=lambda *args: events.append("internet") or True), \
                patch.object(monitor, "client", return_value=client), patch.object(monitor, "log"):
            self.assertEqual(monitor.check_once()[0], "online")
        self.assertEqual(events, ["status", "login", "internet"])
        client.reconnect.assert_called_once_with("sample-account", "dummy-password", initial_status=status)

    def test_existing_authentication_is_preserved(self):
        monitor = host.HostMonitor()
        client = Mock()
        client.is_online.return_value = True
        with patch.object(host, "physical_link", return_value=True), \
                patch.object(host, "internet_available", return_value=True), \
                patch.object(monitor, "client", return_value=client), patch.object(monitor, "log"):
            self.assertEqual(monitor.check_once()[0], "online")
        client.status.assert_called_once()
        client.reconnect.assert_not_called()

    def test_missing_credentials_are_reported_without_login(self):
        monitor = host.HostMonitor()
        monitor.username = monitor.password = ""
        client = Mock()
        client.is_online.return_value = False
        with patch.object(host, "physical_link", return_value=True), \
                patch.object(host, "internet_available") as internet, \
                patch.object(monitor, "client", return_value=client), patch.object(monitor, "log"):
            self.assertEqual(monitor.check_once()[0], "need_credentials")
        client.reconnect.assert_not_called()
        internet.assert_not_called()

    def test_raw_credentials_are_preserved_and_environment_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("ZJU_USERNAME=file-account\nZJU_PASSWORD=raw$#' value \n", encoding="utf-8")
            with patch.dict(os.environ, {"ZJU_USERNAME": "session-account"}, clear=True):
                host.load_env(path)
                self.assertEqual(os.environ["ZJU_USERNAME"], "session-account")
                self.assertEqual(os.environ["ZJU_PASSWORD"], "raw$#' value ")

    def test_windows_link_detection_requires_address_gateway_and_requested_interface(self):
        rows = [
            {"name": "WLAN", "up": False, "gateway": True, "ipv4": ["192.0.2.20"]},
            {"name": "Ethernet", "up": True, "gateway": True, "ipv4": ["198.51.100.2"]},
        ]
        result = subprocess.CompletedProcess([], 0, json.dumps(rows), "")
        with patch.object(host.sys, "platform", "win32"), \
                patch.object(host.subprocess, "CREATE_NO_WINDOW", 0, create=True), \
                patch.object(host.subprocess, "run", return_value=result):
            self.assertFalse(host.physical_link("WLAN"))
            self.assertTrue(host.physical_link())
            rows[1]["ipv4"] = ["169.254.1.2"]
            result.stdout = json.dumps(rows)
            self.assertFalse(host.physical_link())

    def test_netsh_profile_is_passed_as_single_argument_without_shell(self):
        profile = "saved profile & literal"
        result = subprocess.CompletedProcess([], 0)
        with patch.object(host.sys, "platform", "win32"), \
                patch.object(host.subprocess, "CREATE_NO_WINDOW", 0, create=True), \
                patch.object(host.subprocess, "run", return_value=result) as run:
            self.assertTrue(host.connect_wifi(profile, "WLAN"))
        self.assertEqual(run.call_args.args[0], ["netsh.exe", "wlan", "connect", "name=" + profile, "interface=WLAN"])
        self.assertNotIn("shell", run.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
