import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import host_reconnect as host
import uu_remote_monitor as uu


def process(name, pid=1, path=None, session=2, parent=0, command=""):
    return {"Name": name, "ProcessId": pid, "ParentProcessId": parent,
            "SessionId": session, "ExecutablePath": path, "CommandLine": command}


class ProcessClassificationTests(unittest.TestCase):
    def setUp(self):
        self.exe = Path("C:/UUremote/GameViewer/GameViewer.exe")

    def classify(self, *rows):
        return uu.classify_processes({"session": 2, "processes": list(rows)}, self.exe)

    def test_only_main_client_in_current_session_counts_as_running(self):
        root = self.exe.parent
        self.assertEqual(self.classify(
            process("GameViewerService.exe", path=str(root / "GameViewerService.exe")),
            process("crashpad_handler.exe", pid=2, path=str(root / "bin/crashpad_handler.exe")),
            process("GameViewer.exe", pid=3, path=str(root / "bin/GameViewer.exe"), session=3),
        ), (False, False))
        self.assertEqual(self.classify(process("GameViewer.exe", path=str(root / "bin/GameViewer.exe"))),
                         (True, False))
        self.assertEqual(self.classify(process("GameViewer.exe")), (True, False))

    def test_unrelated_upgrade_and_installation_are_ignored(self):
        self.assertEqual(self.classify(
            process("Upgrade.exe", path="C:/Other/Upgrade.exe"),
            process("GameViewer.exe", pid=2, path="C:/Other/GameViewer.exe"),
        ), (False, False))

    def test_upgrade_detected_even_when_main_process_is_still_running(self):
        self.assertEqual(self.classify(
            process("GameViewer.exe", path=str(self.exe)),
            process("Upgrade.exe", pid=2, path=str(self.exe.parent / "bin/Upgrade.exe")),
        ), (True, True))

    def test_temporary_installer_related_by_command_line_or_parent(self):
        self.assertEqual(self.classify(process("setup.exe", path="C:/Temp/setup.exe",
                                             command=f'--install-dir "{self.exe.parent}"')), (False, True))
        self.assertEqual(self.classify(
            process("Upgrade.exe", path=str(self.exe.parent / "bin/Upgrade.exe")),
            process("helper.exe", pid=2, parent=1, path="C:/Temp/helper.exe"),
            process("setup.exe", pid=3, parent=2, path="C:/Temp/setup.exe"),
        ), (False, True))
        self.assertEqual(self.classify(process("GameViewerSetup-4.42.exe", path="C:/Temp/setup.exe")),
                         (False, True))
        self.assertEqual(self.classify(
            process("GameViewerService.exe"),
            process("Upgrade.exe", pid=2, parent=1),
        ), (False, True))


class UURemoteMonitorTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {
            "UU_REMOTE_MONITOR": "1", "UU_REMOTE_UPDATE_GRACE": "30",
            "UU_REMOTE_START_RETRY": "60", "UU_REMOTE_CHECK_INTERVAL": "5",
        })
        self.environment.start()
        self.platform = patch.object(uu.sys, "platform", "win32")
        self.platform.start()
        self.directory = tempfile.TemporaryDirectory()
        self.exe = Path(self.directory.name) / "GameViewer.exe"
        self.exe.write_bytes(b"launcher")
        (self.exe.parent / "bin").mkdir()
        (self.exe.parent / "bin/GameViewer.exe").write_bytes(b"client")
        self.monitor = uu.UURemoteMonitor(Mock())
        self.monitor.executable = self.exe
        self.snapshot = patch.object(self.monitor, "inspect", return_value=(False, False)).start()
        self.clock = patch.object(uu.time, "monotonic", return_value=0).start()
        self.launch = patch.object(uu.subprocess, "Popen").start()

    def tearDown(self):
        patch.stopall()
        self.directory.cleanup()

    def check(self, now, status=(False, False)):
        self.clock.return_value = now
        self.snapshot.return_value = status
        return self.monitor.check_once()

    def test_absent_client_starts_once_after_grace_and_retries_with_cooldown(self):
        self.assertEqual(self.check(0), "waiting")
        self.assertEqual(self.check(29), "waiting")
        self.launch.assert_not_called()
        self.assertEqual(self.check(30), "starting")
        self.launch.assert_called_once_with([str(self.exe)], cwd=str(self.exe.parent),
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL)
        self.check(60)
        self.assertEqual(self.launch.call_count, 1)
        self.check(90)
        self.assertEqual(self.launch.call_count, 2)
        self.assertEqual(self.check(95, (True, False)), "running")
        self.assertEqual(self.launch.call_count, 2)

    def test_upgrade_waits_without_timeout_then_stability_window_before_launch(self):
        self.check(0)
        self.assertEqual(self.check(20, (True, True)), "updating")
        self.assertEqual(self.check(900, (False, True)), "updating")
        self.assertEqual(self.check(905), "waiting")
        self.assertEqual(self.check(934), "waiting")
        self.launch.assert_not_called()
        self.assertEqual(self.check(935), "starting")
        self.launch.assert_called_once()

    def test_upgrade_auto_restart_prevents_duplicate_launch(self):
        self.check(0, (False, True))
        self.check(10)
        self.assertEqual(self.check(15, (True, False)), "running")
        self.check(100, (True, False))
        self.launch.assert_not_called()

    def test_file_replacement_and_missing_file_restart_stability_timer(self):
        self.check(0)
        self.exe.unlink()
        self.assertEqual(self.check(40), "missing")
        self.exe.write_bytes(b"new launcher")
        self.assertEqual(self.check(50), "waiting")
        # bin 主程序更新同样需要重新等待。
        (self.exe.parent / "bin/GameViewer.exe").write_bytes(b"new client")
        self.assertEqual(self.check(75), "waiting")
        self.check(104)
        self.launch.assert_not_called()
        self.assertEqual(self.check(105), "starting")

    def test_missing_bin_client_blocks_launch_even_if_launcher_is_stable(self):
        (self.exe.parent / "bin/GameViewer.exe").unlink()
        self.assertEqual(self.check(0), "missing")
        self.assertEqual(self.check(300), "missing")
        self.launch.assert_not_called()

    def test_snapshot_failure_never_treated_as_missing_and_resets_grace(self):
        self.check(0)
        self.snapshot.side_effect = subprocess.TimeoutExpired("powershell.exe", 15)
        self.clock.return_value = 40
        self.assertEqual(self.monitor.check_once(), "error")
        self.snapshot.side_effect = None
        self.check(50)
        self.check(79)
        self.launch.assert_not_called()
        self.assertEqual(self.check(80), "starting")

    def test_launch_failure_is_retried_after_cooldown(self):
        self.check(0)
        self.launch.side_effect = PermissionError("file locked during update")
        self.assertEqual(self.check(30), "error")
        self.launch.side_effect = None
        self.check(35)
        self.check(65)
        self.assertEqual(self.launch.call_count, 1)
        self.assertEqual(self.check(90), "starting")
        self.assertEqual(self.launch.call_count, 2)

    def test_disabled_monitor_does_not_inspect_or_launch(self):
        self.monitor.enabled = False
        self.assertEqual(self.check(0), "disabled")
        self.snapshot.assert_not_called()
        self.launch.assert_not_called()

    def test_unknown_installation_is_retried(self):
        self.monitor.executable = None
        with patch.object(uu, "discover_executable", side_effect=[None, self.exe]), \
                patch.object(uu, "process_snapshot", return_value={"session": 2, "processes": []}):
            # 使用真实 inspect，确认自动识别在未找到路径后仍会再次尝试。
            self.snapshot.side_effect = lambda: uu.UURemoteMonitor.inspect(self.monitor)
            self.assertEqual(self.check(0), "not_found")
            self.assertEqual(self.check(5), "waiting")
            self.assertEqual(self.check(35), "starting")


class HostIntegrationTests(unittest.TestCase):
    def test_uu_worker_operates_while_network_check_is_blocked(self):
        active = threading.Event()
        remote = Mock(enabled=True)
        remote.run.side_effect = lambda stop: active.set()
        monitor = host.HostMonitor()

        def blocked_network():
            self.assertTrue(active.wait(2), "UU 守护应独立于网络恢复运行")
            raise KeyboardInterrupt

        with patch.object(host, "UURemoteMonitor", return_value=remote), \
                patch.object(monitor, "check_once", side_effect=blocked_network), \
                patch.object(monitor, "log"):
            monitor.run()
        self.assertTrue(remote.run.call_args.args[0].is_set())


class UUServiceTests(unittest.TestCase):
    def setUp(self):
        self.real_service_status = uu.service_status
        self.monitor = uu.UUServiceMonitor(Mock())
        self.monitor.client = Mock(grace=30, retry=60, executable=Path("C:/UU/GameViewer.exe"), signature=None)
        self.monitor.client.inspect.return_value = (False, False)
        self.monitor.client.file_signature.return_value = (("launcher", 123, 0),)
        self.clock = patch.object(uu.time, "monotonic", return_value=0).start()
        self.service = patch.object(uu, "service_status", return_value={"status": "Stopped", "disabled": False}).start()
        self.launch = patch.object(uu.subprocess, "Popen").start()
        self.addCleanup(patch.stopall)

    def test_running_service_is_never_restarted_or_replaced_by_gui(self):
        self.service.return_value = {"status": "Running", "disabled": False}
        self.assertEqual(self.monitor.check_once(), "running")
        self.service.assert_called_once_with()
        self.launch.assert_not_called()

    def test_stopped_service_waits_and_starts_only_after_stable_window(self):
        self.assertEqual(self.monitor.check_once(), "waiting")
        self.clock.return_value = 30
        self.monitor.check_once()
        self.assertEqual(self.service.call_args_list[-1].kwargs, {"start": True})
        self.clock.return_value = 40
        self.monitor.check_once()
        self.assertEqual(sum(bool(call.kwargs.get("start")) for call in self.service.call_args_list), 1)
        self.launch.assert_not_called()

    def test_disabled_or_upgrading_service_is_not_started(self):
        self.service.return_value = {"status": "Stopped", "disabled": True}
        self.assertEqual(self.monitor.check_once(), "disabled")
        self.service.return_value = {"status": "Stopped", "disabled": False}
        self.monitor.client.inspect.return_value = (False, True)
        self.clock.return_value = 100
        self.assertEqual(self.monitor.check_once(), "updating")
        self.assertFalse(any(call.kwargs.get("start") for call in self.service.call_args_list))

    def test_service_start_failure_is_rate_limited(self):
        self.monitor.check_once()
        self.clock.return_value = 30
        self.service.side_effect = lambda **kwargs: (_ for _ in ()).throw(OSError("service locked")) if kwargs else {"status": "Stopped", "disabled": False}
        with self.assertRaises(OSError):
            self.monitor.check_once()
        self.clock.return_value = 40
        self.assertEqual(self.monitor.check_once(), "pending")

    def test_service_command_never_changes_startup_type_or_stops_service(self):
        completed = subprocess.CompletedProcess([], 0, '{"status":"Running","disabled":false}')
        with patch.object(uu.subprocess, "run", return_value=completed) as run:
            self.assertEqual(self.real_service_status(start=True)["status"], "Running")
        script = run.call_args.args[0][-1]
        self.assertIn("Start-Service", script)
        self.assertNotIn("Stop-Service", script)
        self.assertNotIn("Set-Service", script)


if __name__ == "__main__":
    unittest.main()
