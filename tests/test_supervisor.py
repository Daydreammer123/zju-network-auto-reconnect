import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SHELL = shutil.which("powershell.exe")


@unittest.skipUnless(sys.platform == "win32" and SHELL, "Windows PowerShell required")
class SupervisorIntegrationTests(unittest.TestCase):
    def test_worker_crash_respawns_and_stop_does_not_kill_diagnostic_or_respawn(self):
        with tempfile.TemporaryDirectory(prefix="zju supervisor ") as directory:
            root = Path(directory)
            shutil.copyfile(ROOT / "native_monitor.ps1", root / "native_monitor.ps1")
            (root / "host_reconnect.py").write_text(
                "import os, pathlib, sys, time\n"
                "if '--diagnose' not in sys.argv:\n"
                "    pathlib.Path(__file__).with_name('worker.json').write_text(str(os.getpid()))\n"
                "time.sleep(3600)\n", encoding="utf-8")
            manager = root / "native_monitor.ps1"

            def invoke(action):
                result = subprocess.run([SHELL, "-NoProfile", "-NonInteractive", "-File", str(manager),
                                         action, "-PythonPath", sys.executable],
                                        capture_output=True, text=True, errors="replace", timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result

            diagnostic = subprocess.Popen([sys.executable, str(root / "host_reconnect.py"), "--diagnose"])
            try:
                invoke("start")
                first = int((root / "worker.json").read_text())
                invoke("start")  # Must adopt the existing supervised worker.
                self.assertEqual(int((root / "worker.json").read_text()), first)
                subprocess.run(["taskkill.exe", "/PID", str(first), "/F"], capture_output=True, check=True, timeout=10)
                deadline = time.monotonic() + 25
                second = first
                while time.monotonic() < deadline:
                    if (root / "worker.json").exists():
                        second = int((root / "worker.json").read_text())
                    if second != first:
                        break
                    time.sleep(0.25)
                self.assertNotEqual(second, first, "Supervisor must replace the exited stub worker")
                invoke("stop")
                self.assertIsNone(diagnostic.poll(), "Read-only diagnostics must not be stopped")
                time.sleep(11)
                status = subprocess.run([SHELL, "-NoProfile", "-NonInteractive", "-File", str(manager), "status"],
                                        capture_output=True, text=True, timeout=20)
                self.assertEqual(status.returncode, 1, "Stopped stub must not respawn")
            finally:
                invoke("stop")
                diagnostic.terminate()
                diagnostic.wait(timeout=10)


class BootInstallerPolicyTests(unittest.TestCase):
    def test_boot_task_is_protected_system_startup_not_interactive_logon(self):
        script = (ROOT / "configure_boot.ps1").read_text(encoding="utf-8")
        for required in ("-AtStartup", "-LogonType ServiceAccount", "S-1-5-18", "Get-AuthenticodeSignature",
                         "Set-ProtectedAcl $config $true ''", "Get-FileHash", "Assert-NoReparse", "--boot-network"):
            self.assertIn(required, script)
        for forbidden in ("-AtLogOn", "connectionmode", "AutoAdminLogon", "-ExecutionPolicy Bypass"):
            self.assertNotIn(forbidden.casefold(), script.casefold())

    def test_task_read_permission_excludes_execution_write_delete_and_acl_changes(self):
        read_mask = 0x120089
        # File/task rights mapping: execute=0x20, write data/append=0x2/0x4,
        # delete=0x10000, write DACL=0x40000, write owner=0x80000.
        self.assertEqual(read_mask & (0x20 | 0x2 | 0x4 | 0x10000 | 0x40000 | 0x80000), 0)
        self.assertIn("AccessAllowed, 0x120089", (ROOT / "configure_boot.ps1").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
