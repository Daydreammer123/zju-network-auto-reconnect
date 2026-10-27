"""浏览器监控和 Windows 原生监控共用的标准库网络检测。"""
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
import http.client
import ipaddress
import re
import socket
import ssl
import sys
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener


class ProbeState(Enum):
    ONLINE = "online"
    FAILED = "failed"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ProxyEndpoint:
    host: str
    port: int


@dataclass(frozen=True)
class ProbeResult:
    state: ProbeState
    reason: str


DEFAULT_PROXY_URLS = ("https://www.google.com/generate_204", "https://www.wikipedia.org/")


def parse_proxy_server(server):
    """仅支持本地 HTTP/Mixed 代理，不接受凭据、远端代理或 PAC。"""
    if ";" in server or "=" in server:
        mappings = dict(part.strip().split("=", 1) for part in server.split(";") if "=" in part)
        server = mappings.get("https", "")
    parsed = urlsplit(server if "://" in server else "http://" + server)
    if (parsed.scheme != "http" or parsed.username or parsed.password or
            parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("Unsupported system proxy")
    host = parsed.hostname
    if host != "localhost" and (not host or not ipaddress.ip_address(host).is_loopback):
        raise ValueError("System proxy must be loopback")
    port = parsed.port
    if not port or not 1 <= port <= 65535:
        raise ValueError("Invalid system proxy port")
    return ProxyEndpoint(host, port)


def system_proxy():
    """只读 HKCU；不使用或设置 HTTP_PROXY/HTTPS_PROXY 环境变量。"""
    if sys.platform != "win32":
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            def value(name, default=None):
                try:
                    return winreg.QueryValueEx(key, name)[0]
                except FileNotFoundError:
                    return default
            if value("ProxyEnable", 0) != 1 or value("AutoConfigURL", ""):
                return None
            return parse_proxy_server(value("ProxyServer", ""))
    except (OSError, ValueError, TypeError):
        return None


def validate_proxy_urls(urls):
    urls = tuple(urls)
    hosts = set()
    if not 2 <= len(urls) <= 4:
        raise ValueError("PROXY_CHECK_URLS 必须提供 2 至 4 个 HTTPS 地址")
    for url in urls:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or parsed.port not in (None, 443)):
            raise ValueError("PROXY_CHECK_URLS 仅支持无凭据的标准 HTTPS 地址")
        hosts.add(parsed.hostname.casefold())
    if len(hosts) < 2:
        raise ValueError("PROXY_CHECK_URLS 至少需要两个不同主机")
    return urls


def _proxy_target(endpoint, url, timeout):
    parsed = urlsplit(url)
    # 直接建立 CONNECT 隧道；不受环境变量、Windows bypass 列表或 urllib 代理回退影响。
    connection = http.client.HTTPSConnection(endpoint.host, endpoint.port, timeout=timeout,
                                              context=ssl.create_default_context())
    connection.set_tunnel(parsed.hostname, 443)
    try:
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        connection.request("GET", path, headers={"User-Agent": "ZJU-Network-Monitor/1.0"})
        with connection.getresponse() as response:
            if response.status not in (200, 204):
                return ProbeResult(ProbeState.UNKNOWN, f"HTTP {response.status}")
            body = response.read(4096).lower() if response.status == 200 else b""
            if any(marker in body for marker in (b"srun_portal", b"login-account", b"net.zju.edu.cn")):
                return ProbeResult(ProbeState.UNKNOWN, "疑似认证页")
            return ProbeResult(ProbeState.ONLINE, "HTTPS 请求成功")
    except ssl.SSLCertVerificationError:
        return ProbeResult(ProbeState.UNKNOWN, "证书验证失败，需人工检查")
    except OSError as error:
        # CONNECT 的拒绝响应以 OSError 抛出；403/407 等不能算校园网故障。
        match = re.match(r"Tunnel connection failed: (\d{3})", str(error))
        if match and int(match[1]) not in (502, 503, 504):
            return ProbeResult(ProbeState.UNKNOWN, f"代理 CONNECT HTTP {match[1]}")
        return ProbeResult(ProbeState.FAILED, "代理上游连接失败或超时")
    except http.client.HTTPException:
        return ProbeResult(ProbeState.UNKNOWN, "HTTP 协议响应异常")
    finally:
        connection.close()


def proxy_available(urls=DEFAULT_PROXY_URLS, timeout=8):
    urls = validate_proxy_urls(urls)
    endpoint = system_proxy()
    if endpoint is None:
        return ProbeResult(ProbeState.UNAVAILABLE, "系统代理未开启或配置不受支持")
    try:
        with socket.create_connection((endpoint.host, endpoint.port), timeout=1):
            pass
    except OSError:
        return ProbeResult(ProbeState.UNAVAILABLE, "本地代理端口未就绪")
    with ThreadPoolExecutor(max_workers=len(urls), thread_name_prefix="proxy-probe") as pool:
        results = list(pool.map(lambda url: _proxy_target(endpoint, url, timeout), urls))
    if any(result.state == ProbeState.ONLINE for result in results):
        return ProbeResult(ProbeState.ONLINE, "代理 HTTPS 探测正常")
    if all(result.state == ProbeState.FAILED for result in results):
        # 用户可能在探测过程中关闭/重配 Clash，不能据旧端口结果切换 Wi-Fi。
        if system_proxy() != endpoint:
            return ProbeResult(ProbeState.UNAVAILABLE, "探测期间系统代理配置变化")
        try:
            with socket.create_connection((endpoint.host, endpoint.port), timeout=1):
                pass
        except OSError:
            return ProbeResult(ProbeState.UNAVAILABLE, "探测期间本地代理退出")
        return ProbeResult(ProbeState.FAILED, "所有代理 HTTPS 目标连接失败")
    return ProbeResult(ProbeState.UNKNOWN, "；".join(result.reason for result in results))


def portal_urls(url):
    urls = [url]
    primary = urlsplit(url)
    fallback = os.environ.get("ZJU_HTTP_FALLBACK_URL")
    if fallback is None and primary.scheme == "https" and primary.hostname == "net.zju.edu.cn":
        path = primary.path if primary.path not in ("", "/") else "/srun_portal_pc"
        fallback = urlunsplit(("http", primary.hostname, path, primary.query or "theme=zju", ""))
    if fallback and fallback != url:
        parsed = urlsplit(fallback)
        if parsed.scheme != "http" or parsed.hostname != primary.hostname:
            raise ValueError("ZJU_HTTP_FALLBACK_URL 必须是同一认证域名的 HTTP 地址")
        urls.append(fallback)
    return urls


class NoProbeRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


def internet_available(timeout=5):
    urls = [url.strip() for url in os.environ.get(
        "NETWORK_CHECK_URLS", "https://www.baidu.com"
    ).split(",") if url.strip()]
    opener = build_opener(ProxyHandler({}), NoProbeRedirect())
    for url in urls:
        try:
            with opener.open(url, timeout=timeout) as response:
                status = getattr(response, "status", None) or response.getcode()
                if status in (200, 204):
                    body = response.read(4096).lower() if status == 200 else b""
                    if any(marker in body for marker in (b"srun_portal", b"login-account", b"net.zju.edu.cn")):
                        continue
                    return True
        except Exception:
            continue
    return False
