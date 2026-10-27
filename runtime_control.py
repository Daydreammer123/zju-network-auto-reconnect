"""进程级/跨会话互斥及 Windows 登录会话检测；仅使用标准库。"""
import ctypes
from ctypes import wintypes
import errno
import os
from pathlib import Path


INSTALL_NAME = "ZJU-Automatic-Reconnection"


def network_lock_path():
    shared = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / INSTALL_NAME / "state" / "network.lock"
    return shared if shared.is_file() else Path(__file__).with_name(".network.lock")


class FileLease:
    """锁随进程退出自动释放；不删除锁文件，避免路径替换造成双实例。"""
    def __init__(self, path):
        self.path = Path(path)
        self.file = None
        self.acquired = False

    def __enter__(self):
        import msvcrt
        try:
            self.file = self.path.open("r+b")
        except FileNotFoundError:
            self.file = self.path.open("a+b")
        try:
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            self.acquired = True
        except OSError as error:
            self.file.close()
            self.file = None
            if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
        return self

    def __exit__(self, *args):
        import msvcrt
        if self.file:
            try:
                if self.acquired:
                    self.file.seek(0)
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                self.file.close()
                self.file = None
                self.acquired = False


class SessionInfo(ctypes.Structure):
    _fields_ = [("session_id", wintypes.DWORD), ("station", wintypes.LPWSTR), ("state", ctypes.c_int)]


def user_session_present():
    """锁屏/断开远程仍算已登录；查询失败抛异常，开机组件不得据此抢占网络。"""
    dll = ctypes.WinDLL("wtsapi32.dll", use_last_error=True)
    dll.WTSEnumerateSessionsW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                        ctypes.POINTER(ctypes.POINTER(SessionInfo)), ctypes.POINTER(wintypes.DWORD)]
    dll.WTSEnumerateSessionsW.restype = wintypes.BOOL
    dll.WTSQuerySessionInformationW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
                                              ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    dll.WTSQuerySessionInformationW.restype = wintypes.BOOL
    dll.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    sessions = ctypes.POINTER(SessionInfo)()
    count = wintypes.DWORD()
    if not dll.WTSEnumerateSessionsW(None, 0, 1, ctypes.byref(sessions), ctypes.byref(count)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        for row in sessions[:count.value]:
            if row.session_id == 0 or row.state not in (0, 1, 4):
                continue
            name = ctypes.c_void_p()
            size = wintypes.DWORD()
            try:
                if not dll.WTSQuerySessionInformationW(None, row.session_id, 5, ctypes.byref(name), ctypes.byref(size)):
                    raise ctypes.WinError(ctypes.get_last_error())
                if name.value and ctypes.wstring_at(name).strip():
                    return True
            finally:
                if name.value:
                    dll.WTSFreeMemory(name)
        return False
    finally:
        dll.WTSFreeMemory(sessions)
