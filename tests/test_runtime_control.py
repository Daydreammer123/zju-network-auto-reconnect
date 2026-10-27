import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import host_reconnect as host
import runtime_control as runtime


@unittest.skipUnless(sys.platform == "win32", "Windows byte locks required")
class LeaseTests(unittest.TestCase):
    def test_cross_process_lock_releases_after_process_exit_without_deleting_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "network.lock"
            with runtime.FileLease(path) as first:
                self.assertTrue(first.acquired)
                with runtime.FileLease(path) as second:
                    self.assertFalse(second.acquired)
                code = "from runtime_control import FileLease; import sys\nwith FileLease(sys.argv[1]) as lock: print(lock.acquired)"
                result = subprocess.run([sys.executable, "-S", "-c", code, str(path)],
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "False")
            with runtime.FileLease(path) as again:
                self.assertTrue(again.acquired)
            self.assertTrue(path.exists())

    def test_network_lock_uses_protected_installation_when_present(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"ProgramData": directory}):
            shared = Path(directory) / runtime.INSTALL_NAME / "state/network.lock"
            self.assertNotEqual(runtime.network_lock_path(), shared)
            shared.parent.mkdir(parents=True)
            shared.write_bytes(b"0")
            self.assertEqual(runtime.network_lock_path(), shared)


class BootCoordinationTests(unittest.TestCase):
    def setUp(self):
        patch.dict(os.environ, {"ZJU_WIFI_FAILOVER": "0", "UU_REMOTE_MONITOR": "0"}, clear=True).start()
        self.monitor = host.HostMonitor(interface="WLAN", boot_network=True)
        self.monitor.log = Mock()
        self.addCleanup(patch.stopall)

    def test_boot_forces_wlan_recovery_but_disables_user_proxy_checks(self):
        self.assertIsNotNone(self.monitor.failover)
        self.assertFalse(self.monitor.failover.probe_enabled)

    def test_boot_configuration_does_not_depend_on_proxy_settings(self):
        with patch.dict(os.environ, {"PROXY_CHECK_URLS": "invalid", "PROXY_FAILURE_THRESHOLD": "invalid",
                                    "PROXY_CHECK_TIMEOUT": "invalid", "PROXY_CHECK_INTERVAL": "invalid"}):
            monitor = host.HostMonitor(interface="WLAN", boot_network=True)
        self.assertFalse(monitor.failover.probe_enabled)

    def test_logged_on_or_locked_user_yields_before_any_network_call(self):
        with patch.object(host, "user_session_present", return_value=True), \
                patch.object(self.monitor.failover, "check_once") as network:
            self.assertEqual(self.monitor.check_once(), ("user_session", 5))
            self.assertEqual(self.monitor.check_once(), ("user_session", 5))
        network.assert_not_called()
        self.monitor.log.assert_called_once()

    def test_session_query_error_never_guesses_that_no_user_is_logged_on(self):
        with patch.object(host, "user_session_present", side_effect=OSError("denied")), \
                patch.object(self.monitor.failover, "check_once") as network:
            with self.assertRaises(OSError):
                self.monitor.check_once()
        network.assert_not_called()

    def test_logged_out_boot_component_reacquires_network_management(self):
        with patch.object(host, "user_session_present", side_effect=[True, False]), \
                patch.object(self.monitor.failover, "check_once", return_value=("boot_online", 60)) as network:
            self.monitor.check_once()
            self.assertEqual(self.monitor.check_once(), ("boot_online", 60))
        network.assert_called_once()

    def test_busy_network_lock_prevents_state_changes(self):
        with patch.object(host, "FileLease") as lease, patch.object(self.monitor, "check_once") as check:
            lease.return_value.__enter__.return_value.acquired = False
            self.assertEqual(self.monitor.coordinated_check()[0], "network_busy")
        check.assert_not_called()

    def test_boot_diagnosis_does_not_read_proxy_or_start_gui(self):
        with patch.object(host, "user_session_present", return_value=True), \
                patch("uu_remote_monitor.service_status", return_value={"status": "Running"}), \
                patch("network_checks.system_proxy") as proxy, \
                patch("wifi_control.wifi_snapshot", return_value=None), \
                patch.object(host.UURemoteMonitor, "inspect") as gui:
            self.monitor.diagnose()
        proxy.assert_not_called()
        gui.assert_not_called()


if __name__ == "__main__":
    unittest.main()
