"""Windows WLAN 的只读快照及已保存配置连接；不读取密钥、不修改无线电。"""
import ctypes
from dataclasses import dataclass
import json
import subprocess
import sys
import threading
import uuid
from ctypes import wintypes


DWORD = ctypes.c_uint32


@dataclass(frozen=True)
class WifiSnapshot:
    profile: str = ""
    connected: bool = False
    radio_on: bool | None = None
    link_ready: bool = False
    default_route: bool = False
    profiles: frozenset[str] = frozenset()
    ip_known: bool = True
    connecting: bool = False


_diagnostic = threading.local()


def last_snapshot_error():
    """仅返回阶段和错误类型/数字码，不泄漏命令输出、身份或凭据。"""
    return getattr(_diagnostic, "error", "")


def _read_error(stage, error):
    code = getattr(error, "winerror", None) or getattr(error, "errno", None)
    _diagnostic.error = f"{stage}: {type(error).__name__}" + (f" ({code})" if code else "")


class GUID(ctypes.Structure):
    _fields_ = [("data", ctypes.c_ubyte * 16)]


class InterfaceInfo(ctypes.Structure):
    _fields_ = [("guid", GUID), ("description", wintypes.WCHAR * 256),
                ("state", DWORD)]


class ConnectionPrefix(ctypes.Structure):
    _fields_ = [("state", DWORD), ("mode", DWORD),
                ("profile", wintypes.WCHAR * 256)]


class ProfileInfo(ctypes.Structure):
    _fields_ = [("name", wintypes.WCHAR * 256), ("flags", DWORD)]


class PhyRadio(ctypes.Structure):
    _fields_ = [("index", DWORD), ("software", DWORD), ("hardware", DWORD)]


def _interface_alias(guid):
    """接口名称不依赖 DHCP、默认路由或 PowerShell 的网络配置查询。"""
    dll = ctypes.WinDLL("iphlpapi.dll")
    dll.ConvertInterfaceGuidToLuid.argtypes = [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_uint64)]
    dll.ConvertInterfaceGuidToLuid.restype = DWORD
    dll.ConvertInterfaceLuidToAlias.argtypes = [ctypes.POINTER(ctypes.c_uint64), wintypes.LPWSTR, ctypes.c_size_t]
    dll.ConvertInterfaceLuidToAlias.restype = DWORD
    luid = ctypes.c_uint64()
    WlanReader._check(dll.ConvertInterfaceGuidToLuid(ctypes.byref(guid), ctypes.byref(luid)))
    name = ctypes.create_unicode_buffer(257)
    WlanReader._check(dll.ConvertInterfaceLuidToAlias(ctypes.byref(luid), name, len(name)))
    return name.value


def _list_items(pointer, item_type):
    count = ctypes.cast(pointer, ctypes.POINTER(DWORD))[0]
    if count > 1024:
        raise ValueError("Invalid WLAN list")
    return (item_type * count).from_address(pointer.value + 8)


class WlanReader:
    """WLAN 内存均由 WlanFreeMemory 释放；句柄即使异常也关闭。"""

    def __enter__(self):
        self.dll = ctypes.WinDLL("wlanapi.dll")
        pvoid = ctypes.POINTER(ctypes.c_void_p)
        self.dll.WlanOpenHandle.argtypes = [DWORD, ctypes.c_void_p,
                                           ctypes.POINTER(DWORD), pvoid]
        self.dll.WlanCloseHandle.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.dll.WlanEnumInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p, pvoid]
        self.dll.WlanQueryInterface.argtypes = [ctypes.c_void_p, ctypes.POINTER(GUID),
            DWORD, ctypes.c_void_p, ctypes.POINTER(DWORD), pvoid, ctypes.POINTER(DWORD)]
        self.dll.WlanGetProfileList.argtypes = [ctypes.c_void_p, ctypes.POINTER(GUID),
                                              ctypes.c_void_p, pvoid]
        self.dll.WlanFreeMemory.argtypes = [ctypes.c_void_p]
        self.dll.WlanFreeMemory.restype = None
        self.handle = ctypes.c_void_p()
        version = DWORD()
        self._check(self.dll.WlanOpenHandle(2, None, ctypes.byref(version),
                                           ctypes.byref(self.handle)))
        return self

    @staticmethod
    def _check(code):
        if code:
            raise OSError(code, "WLAN read failed")

    def __exit__(self, *args):
        self.dll.WlanCloseHandle(self.handle, None)

    def interfaces(self):
        pointer = ctypes.c_void_p()
        try:
            self._check(self.dll.WlanEnumInterfaces(self.handle, None, ctypes.byref(pointer)))
            # 必须复制，不能把指向已释放内存的 ctypes 对象返回给调用方。
            return [InterfaceInfo.from_buffer_copy(item) for item in _list_items(pointer, InterfaceInfo)]
        finally:
            if pointer.value:
                self.dll.WlanFreeMemory(pointer)

    def query(self, guid, opcode):
        pointer = ctypes.c_void_p()
        size, kind = DWORD(), DWORD()
        try:
            self._check(self.dll.WlanQueryInterface(self.handle, ctypes.byref(guid), opcode,
                None, ctypes.byref(size), ctypes.byref(pointer), ctypes.byref(kind)))
            return ctypes.string_at(pointer, size.value)
        finally:
            if pointer.value:
                self.dll.WlanFreeMemory(pointer)

    def profiles(self, guid):
        pointer = ctypes.c_void_p()
        try:
            self._check(self.dll.WlanGetProfileList(self.handle, ctypes.byref(guid), None,
                                                   ctypes.byref(pointer)))
            return frozenset(item.name for item in _list_items(pointer, ProfileInfo))
        finally:
            if pointer.value:
                self.dll.WlanFreeMemory(pointer)


def _ip_state(guid):
    guid = str(uuid.UUID(guid))
    # guid 来自 WLAN API，不插入任何用户提供的接口名或配置名称。
    script = """
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
    $ErrorActionPreference = 'Stop'
    $adapter = @(Get-NetAdapter -IncludeHidden | Where-Object { [guid]$_.InterfaceGuid -eq [guid]'%s' })
    if ($adapter.Count -ne 1) { throw 'Adapter not found' }
    $config = Get-NetIPConfiguration -InterfaceIndex $adapter[0].ifIndex
    $routes = @(Get-NetRoute -AddressFamily IPv4 -PolicyStore ActiveStore |
        Where-Object { $_.DestinationPrefix -eq '0.0.0.0/0' -and $_.State -ne 'Dead' } | ForEach-Object {
            $ipif = Get-NetIPInterface -InterfaceIndex $_.InterfaceIndex -AddressFamily IPv4
            if ($ipif.ConnectionState -eq 'Connected') {
                [pscustomobject]@{ index = $_.InterfaceIndex; metric = ($_.RouteMetric + $ipif.InterfaceMetric) }
            }
        } | Sort-Object metric)
    $best = @()
    if ($routes.Count) { $best = @($routes | Where-Object { $_.metric -eq $routes[0].metric }) }
    [pscustomobject]@{
        name = $adapter[0].Name
        ready = ($adapter[0].Status -eq 'Up' -and [bool]$config.IPv4DefaultGateway -and
            @($config.IPv4Address.IPAddress | Where-Object { $_ -and $_ -notlike '169.254.*' }).Count -gt 0)
        default_route = ($adapter[0].ifIndex -in @($best.index))
    } | ConvertTo-Json -Compress
    """ % guid
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, encoding="utf-8", errors="replace", timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise OSError("Cannot read adapter routing")
    return json.loads(result.stdout)


def _radio_on(data):
    if len(data) < 4:
        return None
    count = int.from_bytes(data[:4], "little")
    if not 1 <= count <= 64 or len(data) < 4 + count * ctypes.sizeof(PhyRadio):
        return None
    rows = (PhyRadio * count).from_buffer_copy(data[4:4 + count * ctypes.sizeof(PhyRadio)])
    if any(row.software == 1 and row.hardware == 1 for row in rows):
        return True
    if all(row.software == 2 or row.hardware == 2 for row in rows):
        return False
    return None


def wifi_snapshot(interface):
    """失败返回 None；不能读到状态时绝不推测掉线或发起连接。"""
    _diagnostic.error = ""
    if sys.platform != "win32" or not interface:
        return None
    try:
        with WlanReader() as wlan:
            for info in wlan.interfaces():
                try:
                    alias = _interface_alias(info.guid)
                except (OSError, ValueError) as error:
                    # 另一块无线网卡可能正被移除；不能影响指定接口的查询。
                    _read_error("接口名称", error)
                    continue
                if alias.casefold() != interface.casefold():
                    continue
                radio = _radio_on(wlan.query(info.guid, 4))
                if radio is None:
                    _diagnostic.error = "WLAN 无线电状态未返回有效值"
                profile = ""
                connected = info.state == 1
                if connected:
                    data = wlan.query(info.guid, 7)
                    connection = ConnectionPrefix.from_buffer_copy(data)
                    if connection.state != 1 or not connection.profile:
                        _diagnostic.error = "读取期间 WLAN 连接发生变化"
                        return None  # 查询过程中连接状态变化，下一轮重新读取。
                    profile = connection.profile
                profiles = wlan.profiles(info.guid)
                if not connected:
                    # 没有 IP/路由正是断线的正常表现，不能成为读取 WLAN 的前置条件。
                    return WifiSnapshot("", False, radio, False, False, profiles,
                                        connecting=info.state in (5, 6, 7))
                guid = str(uuid.UUID(bytes_le=bytes(info.guid.data)))
                try:
                    ip = _ip_state(guid)
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    _read_error("IP/路由", error)
                    return WifiSnapshot(profile, True, radio, False, False, profiles, ip_known=False)
                return WifiSnapshot(profile, connected, radio, bool(ip["ready"]),
                                    bool(ip["default_route"]), profiles)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        _read_error("WLAN 状态", error)
        return None
    if not last_snapshot_error():
        _diagnostic.error = "指定 WLAN 接口未找到"
    return None


def connect_wifi(profile, interface=""):
    """只请求连接已有配置；netsh 成功不代表连接已经完成。"""
    if sys.platform != "win32" or not profile:
        return False
    command = ["netsh.exe", "wlan", "connect", "name=" + profile]
    if interface:
        command.append("interface=" + interface)
    try:
        result = subprocess.run(command, capture_output=True, timeout=10,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
