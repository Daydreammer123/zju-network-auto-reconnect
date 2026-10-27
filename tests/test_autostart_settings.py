import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SHELLS = [path for name in ("powershell.exe", "pwsh.exe") if (path := shutil.which(name))]


@unittest.skipUnless(SHELLS, "PowerShell is not installed")
class AutostartSettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.env = dict(os.environ)
        self.env.pop("ZJU_WIFI_FAILOVER", None)

    def evaluate(self, shell, text, value=None):
        (self.root / ".env").write_text(text, encoding="utf-8-sig")
        env = dict(self.env)
        if value is not None:
            env["ZJU_WIFI_FAILOVER"] = value
        helper = str(ROOT / "monitor_settings.ps1").replace("'", "''")
        project = str(self.root).replace("'", "''")
        script = ("[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
                  f". '{helper}'; Get-ZjuFailoverEnabled -ProjectDirectory '{project}' | ConvertTo-Json -Compress")
        return subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", script],
                              env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=20)

    def test_raw_env_and_process_precedence_match_python(self):
        for shell in SHELLS:
            for text, value, expected in (("", None, False),
                                          ("# ZJU_WIFI_FAILOVER=0\nZJU_WIFI_FAILOVER=1\n", None, True),
                                          ("ZJU_WIFI_FAILOVER=1\n", "0", False),
                                          ("ZJU_WIFI_FAILOVER=0\nZJU_WIFI_FAILOVER=1\n", None, False)):
                with self.subTest(shell=shell, text=text, value=value):
                    result = self.evaluate(shell, text, value)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIs(json.loads(result.stdout), expected)

    def test_invalid_flag_fails_without_printing_env_secrets(self):
        for shell in SHELLS:
            result = self.evaluate(shell, "ZJU_PASSWORD=super-secret\nZJU_WIFI_FAILOVER=maybe\n")
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("super-secret", result.stdout + result.stderr)

    def startup(self, shell, flag, manager_exit=0):
        for filename in ("startup_reconnection.ps1", "monitor_settings.ps1"):
            shutil.copyfile(ROOT / filename, self.root / filename)
        (self.root / ".env").write_text(f"ZJU_WIFI_FAILOVER={flag}\n", encoding="utf-8")
        # 只在临时目录运行 stub，不启动真实 Python、监控或容器。
        (self.root / "native_monitor.ps1").write_text(
            "param([string]$Action, [string]$PythonPath)\n"
            "Set-Content (Join-Path $PSScriptRoot 'native-started.txt') $Action\n"
            f"exit {manager_exit}\n", encoding="utf-8")
        marker = self.root / "native-started.txt"
        if marker.exists():
            marker.unlink()
        (self.root / "docker.cmd").write_text(
            '@echo off\necho unexpected>"%~dp0docker-used.txt"\nexit /b 99\n', encoding="ascii")
        env = dict(self.env, PATH=str(self.root))
        result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-File",
            str(self.root / "startup_reconnection.ps1"), "-PythonPath", "dummy-python.exe"],
            env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=20)
        self.assertFalse((self.root / "docker-used.txt").exists())
        return result

    def test_both_modes_start_only_native_monitor(self):
        for shell in SHELLS:
            for flag in ("0", "1"):
                with self.subTest(shell=shell, flag=flag):
                    result = self.startup(shell, flag)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual((self.root / "native-started.txt").read_text(encoding="utf-8-sig").strip(), "supervise")
                    self.assertIn("persistent supervisor", (self.root / "startup_reconnection.log").read_text(encoding="utf-8"))

    def test_manager_failure_is_not_reported_as_success(self):
        for shell in SHELLS:
            result = self.startup(shell, "1", manager_exit=7)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Startup failed", (self.root / "startup_reconnection.log").read_text(encoding="utf-8"))

    def test_invalid_mode_is_rejected_before_starting_monitor(self):
        for shell in SHELLS:
            result = self.startup(shell, "invalid")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.root / "native-started.txt").exists())

    def test_registration_has_no_container_cli_dependency(self):
        source = (ROOT / "configure_autostart.ps1").read_text(encoding="utf-8-sig").casefold()
        self.assertNotIn("dockerpath", source)
        self.assertNotIn("docker.exe", source)
        self.assertNotIn("compose", source)

    def test_powershell_scripts_parse_in_both_versions(self):
        for shell in SHELLS:
            for filename in ("monitor_settings.ps1", "startup_reconnection.ps1", "configure_autostart.ps1",
                             "native_monitor.ps1", "configure_boot.ps1"):
                with self.subTest(shell=shell, filename=filename):
                    path = str(ROOT / filename).replace("'", "''")
                    script = ("$tokens=$null; $errors=$null; "
                        f"[System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$tokens, [ref]$errors) | Out-Null; "
                        "if ($errors.Count) { $errors | ForEach-Object { $_.Message }; exit 1 }")
                    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", script],
                        capture_output=True, encoding="utf-8", errors="replace", timeout=20)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
