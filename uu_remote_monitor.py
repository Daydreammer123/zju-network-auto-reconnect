"""网易 UU 远程守护：当前登录会话进程检测、升级避让与自动启动（仅标准库）。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def discover_executable():
    """优先使用显式路径，再从卸载注册表找到安装根目录的启动入口。"""
    configured = os.environ.get("UU_REMOTE_EXE", "").strip().strip('"')
    if configured:
        return Path(os.path.expandvars(configured))
    if sys.platform != "win32":
        return None
    import winreg

    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
                                    0, winreg.KEY_READ | view) as uninstall:
                    for index in range(winreg.QueryInfoKey(uninstall)[0]):
                        try:
                            with winreg.OpenKey(uninstall, winreg.EnumKey(uninstall, index)) as key:
                                icon = str(winreg.QueryValueEx(key, "DisplayIcon")[0])
                                filename, separator, index_text = icon.rpartition(",")
                                if separator and index_text.strip().lstrip("-").isdigit():
                                    icon = filename
                                icon = icon.strip().strip('"')
                                path = Path(os.path.expandvars(icon))
                                if path.name.casefold() == "gameviewer.exe":
                                    return path.parent.parent / path.name if path.parent.name.casefold() == "bin" else path
                        except OSError:
                            continue
            except OSError:
                continue
    return None


def process_snapshot():
    """同时读取客户端、升级器及父子关系；读取失败不能当作进程不存在。"""
    script = """
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
    $ErrorActionPreference = 'Stop'
    $session = (Get-Process -Id $PID).SessionId
    $rows = @(Get-CimInstance Win32_Process | Select-Object `
        Name, ProcessId, ParentProcessId, SessionId, ExecutablePath, CommandLine)
    ConvertTo-Json -InputObject @{session = $session; processes = $rows} -Compress -Depth 4
    """
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, encoding="utf-8", errors="replace", timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise OSError("无法读取 Windows 进程状态")
    data = json.loads(result.stdout)
    if not isinstance(data.get("processes"), list) or not isinstance(data.get("session"), int):
        raise ValueError("Windows 进程状态格式异常")
    return data


def installation_root(executable):
    parent = executable.parent
    return parent.parent if parent.name.casefold() == "bin" else parent


def belongs_to_installation(value, root):
    if not value:
        return False
    try:
        Path(value).relative_to(root)
        return True
    except ValueError:
        return False


def classify_processes(snapshot, executable):
    root = installation_root(executable) if executable else None
    rows = snapshot["processes"]
    related = {row["ProcessId"] for row in rows
               if root and belongs_to_installation(row.get("ExecutablePath"), root)}
    related.update(row["ProcessId"] for row in rows
                   if row["Name"].casefold() == "gameviewerservice.exe"
                   and not row.get("ExecutablePath"))
    # 安装器可能从临时目录运行；保留与安装目录的命令行或父子进程关联。
    if root:
        root_text = str(root).casefold()
        related.update(row["ProcessId"] for row in rows
                       if root_text in (row.get("CommandLine") or "").casefold())
    while True:
        children = {row["ProcessId"] for row in rows if row.get("ParentProcessId") in related}
        if children <= related:
            break
        related.update(children)
    running = updating = False
    for row in rows:
        name = row["Name"].casefold()
        path = row.get("ExecutablePath")
        if name == "gameviewer.exe" and row.get("SessionId") == snapshot["session"]:
            # 同会话下不可读的进程路径也视为已运行，避免反复启动高权限客户端。
            if not root or not path or belongs_to_installation(path, root):
                running = True
        updater_name = (name in {"upgrade.exe", "updater.exe"}
                        or (name.endswith(".exe") and any(word in name for word in
                            ("setup", "install", "upgrade", "update"))))
        product_name = any(word in name for word in ("gameviewer", "uuremote", "uuyc", "uu远程"))
        if updater_name and (row["ProcessId"] in related or product_name):
            updating = True
    return running, updating


class UURemoteMonitor:
    def __init__(self, log):
        self.log = log
        self.enabled = sys.platform == "win32" and os.environ.get("UU_REMOTE_MONITOR", "1") == "1"
        self.interval = max(1, int(os.environ.get("UU_REMOTE_CHECK_INTERVAL", "5")))
        self.grace = max(1, int(os.environ.get("UU_REMOTE_UPDATE_GRACE", "30")))
        self.retry = max(self.grace, int(os.environ.get("UU_REMOTE_START_RETRY", "60")))
        self.executable = None
        self.quiet_since = None
        self.signature = None
        self.last_start = None
        self.state = None
        self._launcher = None

    def report(self, state, message):
        if self.state != state:
            self.log(f"网易UU远程：{message}")
            self.state = state
        return state

    def inspect(self):
        if self.executable is None:
            self.executable = discover_executable()
        return classify_processes(process_snapshot(), self.executable)

    def program_files(self):
        root = installation_root(self.executable)
        return tuple(dict.fromkeys((self.executable, root / "bin" / "GameViewer.exe")))

    def file_signature(self):
        # 根目录入口和 bin 主程序都参与稳定性检查；升级替换任一文件均重新等待。
        signature = []
        for path in self.program_files():
            if path.is_file():
                stat = path.stat()
                signature.append((str(path), stat.st_size, stat.st_mtime_ns))
            else:
                signature.append((str(path), None, None))
        return tuple(signature)

    def check_once(self):
        if not self.enabled:
            return "disabled"
        try:
            running, updating = self.inspect()
            now = time.monotonic()
            if updating:
                self.quiet_since = now
                self.signature = None
                return self.report("updating", "检测到升级/安装进程，等待升级完成。")
            if running:
                self.quiet_since = None
                self.signature = None
                return self.report("running", "已在当前会话后台运行。")
            if not self.executable:
                self.quiet_since = None
                return self.report("not_found", "未找到安装入口，请设置 UU_REMOTE_EXE；将继续检测。")
            signature = self.file_signature()
            if self.quiet_since is None or self.signature != signature:
                self.quiet_since = now
                self.signature = signature
            if any(item[1] is None for item in signature):
                return self.report("missing", "启动入口或主程序暂不可用，等待安装/升级恢复文件。")
            if now - self.quiet_since < self.grace:
                return self.report("waiting", f"主进程未运行，等待文件稳定 {self.grace} 秒后启动。")
            if self.last_start is not None and now - self.last_start < self.retry:
                return self.report("starting", "已请求启动，等待主进程出现；稍后失败自动重试。")
            self.last_start = now
            # 使用安装入口和正确工作目录，让软件自行完成启动/升级分发。
            process = subprocess.Popen([str(self.executable)], cwd=str(self.executable.parent),
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
            # 不等待 GUI 退出；Popen 对象保留至下轮以便回收短暂的启动入口。
            self._launcher = process
            return self.report("starting", "未检测到主进程，已请求自动启动。")
        except (OSError, ValueError, subprocess.SubprocessError):
            self.quiet_since = None
            self.signature = None
            return self.report("error", "进程检查或启动失败，下一周期重试。")

    def run(self, stop_event):
        self.log(f"网易UU远程守护已启用：每 {self.interval} 秒检测，升级结束后稳定等待 {self.grace} 秒。")
        while not stop_event.is_set():
            try:
                launcher = self._launcher
                if launcher is not None and launcher.poll() is not None:
                    self._launcher = None
                self.check_once()
            except Exception as error:
                self.quiet_since = None
                self.report("error", f"守护异常（{type(error).__name__}），下一周期重试。")
            stop_event.wait(self.interval)


def service_status(start=False):
    """开机模式只使用官方已安装服务，绝不在 Session 0 启动桌面客户端。"""
    script = """
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
    $ErrorActionPreference = 'Stop'
    $service = Get-Service -Name 'GameViewerService'
    if (%s -and $service.Status -eq 'Stopped' -and $service.StartType -ne 'Disabled') {
        Start-Service -Name 'GameViewerService'
        $service.Refresh()
    }
    [pscustomobject]@{status = [string]$service.Status; disabled = ($service.StartType -eq 'Disabled')} |
        ConvertTo-Json -Compress
    """ % ("$true" if start else "$false")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, encoding="utf-8", errors="replace", timeout=20,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise OSError("UU service query/start failed")
    data = json.loads(result.stdout)
    if not isinstance(data, dict) or not isinstance(data.get("status"), str) or not isinstance(data.get("disabled"), bool):
        raise ValueError("Invalid UU service status")
    return data


class UUServiceMonitor:
    """复用升级避让、文件稳定窗口和重试限速，仅恢复已安装且未禁用的服务。"""
    def __init__(self, log):
        self.log = log
        self.enabled = os.environ.get("UU_REMOTE_MONITOR", "1") == "1"
        self.interval = 10
        self.client = UURemoteMonitor(log)
        self.stopped_since = None
        self.last_start = None
        self.state = None

    def check_once(self):
        status = service_status()
        now = time.monotonic()
        if status["status"] == "Running":
            self.stopped_since = None
            state = "running"
        elif status["disabled"]:
            self.stopped_since = None
            state = "disabled"
        elif status["status"] != "Stopped":
            self.stopped_since = None
            state = "pending"
        else:
            _, updating = self.client.inspect()
            if updating:
                self.stopped_since = None
                state = "updating"
            else:
                signature = self.client.file_signature() if self.client.executable else ()
                if not signature or any(item[1] is None for item in signature):
                    self.stopped_since = None
                    state = "missing"
                else:
                    if self.stopped_since is None or self.client.signature != signature:
                        self.stopped_since = now
                        self.client.signature = signature
                    if now - self.stopped_since < self.client.grace:
                        state = "waiting"
                    elif self.last_start is not None and now - self.last_start < self.client.retry:
                        state = "pending"
                    else:
                        self.last_start = now
                        result = service_status(start=True)
                        state = "running" if result["status"] == "Running" else "pending"
        if state != self.state:
            self.log(f"UU 开机服务：{state}；仅守护 GameViewerService，不更改软件设置。")
            self.state = state
        return state

    def run(self, stop_event):
        while not stop_event.is_set():
            try:
                self.check_once()
            except Exception as error:
                self.stopped_since = None
                if self.state != "error":
                    self.log(f"UU 开机服务：状态读取/启动失败（{type(error).__name__}），稍后重试；不会重启正在运行的服务。")
                    self.state = "error"
            stop_event.wait(self.interval)
