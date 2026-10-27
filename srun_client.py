"""浙江大学门户原生 SRun 接口兜底，仅依赖 Python 标准库。"""
import base64
import hashlib
import hmac
import ipaddress
import json
import re
import socket
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from urllib.request import ProxyHandler, Request, build_opener


STANDARD_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
SRUN_ALPHABET = "LVoJPiCN2R8G90yg+hmFHuacZ1OWMnrsSTXkYpUq/3dlbfKwv6xztjI7DeBE45QA"
MASK = 0xFFFFFFFF


class PortalError(Exception):
    pass


class PortalUnavailable(PortalError):
    """认证服务器不可达，与页面渲染失败或账号认证失败不同。"""


def connection_reason(error):
    """只分类连接原因，避免 urllib 异常中的认证 URL 泄露。"""
    reason = getattr(error, "reason", error)
    if isinstance(reason, socket.gaierror):
        return "DNS 解析失败"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "连接超时"
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "HTTPS 证书校验失败"
    if isinstance(reason, ssl.SSLError):
        return "TLS 握手失败"
    if isinstance(reason, ConnectionRefusedError):
        return "连接被拒绝"
    if isinstance(reason, (ConnectionResetError, ConnectionAbortedError)):
        return "连接被中断"
    return "连接失败"


def http_reason(error):
    # 中间转发层可能以 403 返回自身的 DNS 失败，不是门户拒绝账号。
    try:
        body = error.read(4096).decode("utf-8", errors="replace").lower()
    except OSError:
        body = ""
    if "lookup " in body and any(word in body for word in ("no such host", "server misbehaving")):
        return f"HTTP {error.code}（中间转发层 DNS 解析失败）"
    if error.code in (502, 503, 504):
        if any(word in body for word in ("network is unreachable", "no route to host", "unreachable network")):
            return f"HTTP {error.code}（上游网络/路由不可达）"
        if any(word in body for word in ("connection refused", "actively refused")):
            return f"HTTP {error.code}（上游连接被拒绝）"
        if any(word in body for word in ("timed out", "timeout")):
            return f"HTTP {error.code}（上游连接超时）"
        return f"HTTP {error.code}（网关/转发层未能访问上游，无法仅凭状态码判断具体原因）"
    return f"HTTP {error.code}"


def _words(text, include_length=False):
    # 与门户 JS charCodeAt 一致，按 UTF-16 code unit 分组，而不是按 UTF-8 字节分组。
    raw = text.encode("utf-16-le", errors="surrogatepass")
    units = [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]
    words = []
    for index in range(0, len(units), 4):
        value = 0
        for offset, unit in enumerate(units[index:index + 4]):
            value |= unit << (8 * offset)
        words.append(value & MASK)
    if include_length:
        words.append(len(units))
    return words


def encode_info(info, token):
    """复现门户 Portal.js 的 SRBX1 编码和自定义 Base64 字母表。"""
    text = json.dumps(info, ensure_ascii=False, separators=(",", ":"))
    values = _words(text, True)
    key = _words(token)
    key.extend([0] * max(0, 4 - len(key)))
    last = len(values) - 1
    z = values[last]
    total = 0
    for _ in range(6 + 52 // len(values)):
        total = (total + 0x9E3779B9) & MASK
        e = (total >> 2) & 3
        for p in range(len(values)):
            y = values[p + 1] if p < last else values[0]
            mixed = (z >> 5) ^ (y << 2)
            mixed += (y >> 3) ^ (z << 4) ^ (total ^ y)
            mixed += key[(p & 3) ^ e] ^ z
            values[p] = (values[p] + mixed) & MASK
            z = values[p]
    encoded = base64.b64encode(b"".join(value.to_bytes(4, "little") for value in values)).decode("ascii")
    return "{SRBX1}" + encoded.translate(str.maketrans(STANDARD_ALPHABET, SRUN_ALPHABET))


def login_parameters(username, password, ip, acid, token):
    info = encode_info({
        "username": username, "password": password, "ip": ip,
        "acid": str(acid), "enc_ver": "srun_bx1",
    }, token)
    digest = hmac.new(token.encode("utf-8"), password.encode("utf-8"), hashlib.md5).hexdigest()
    checksum = "".join(token + part for part in (username, digest, str(acid), ip, "200", "1", info))
    return {
        "action": "login", "username": username, "password": "{MD5}" + digest,
        "info": info, "chksum": hashlib.sha1(checksum.encode("utf-8")).hexdigest(),
        "ac_id": str(acid), "ip": ip, "n": "200", "type": "1",
        "os": "Linux", "name": "Linux", "double_stack": "0",
    }


class SrunClient:
    def __init__(self, urls, timeout=8, acid=None, portal_ip=None):
        self.origins = []
        self.ip_origin = None
        self.timeout = timeout
        self.acid = acid
        for url in urls:
            parsed = urlsplit(url)
            origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
            if origin not in self.origins:
                self.origins.append(origin)
            if self.acid is None:
                self.acid = parse_qs(parsed.query).get("ac_id", [None])[0]
        if portal_ip and any(urlsplit(url).hostname == "net.zju.edu.cn" for url in urls):
            address = str(ipaddress.IPv4Address(portal_ip))
            # 仅已有 HTTP 回退时增加固定 IP 路径。Host 使用 IP，让透明转发层也不再解析域名。
            # HTTPS 仍用域名校验证书；不访问带 IP 的 HTTPS，也不关闭证书校验。
            if any(urlsplit(url).scheme == "http" for url in urls):
                self.ip_origin = "http://" + address
                if self.ip_origin not in self.origins:
                    self.origins.append(self.ip_origin)
        self.acid = str(self.acid) if self.acid else None
        self.origin = None
        self.reported_ip_fallback = False
        # 校园网认证流量直连，不使用外网代理。
        self.opener = build_opener(ProxyHandler({}))

    def discover_acid(self):
        """读取服务器按当前校园网 IP 生成的 CONFIG.acid，无需执行页面 JS。"""
        origins = ([self.origin] + [item for item in self.origins if item != self.origin]
                   if self.origin else self.origins)
        errors = []
        unavailable = []
        for candidate in origins:
            try:
                with self.opener.open(candidate + "/srun_portal_pc", timeout=self.timeout) as response:
                    text = response.read(1024 * 1024).decode("utf-8")
                config = re.search(r"\b(?:var|let|const)\s+CONFIG\s*=\s*\{", text)
                match = re.search(r"\bacid\s*:\s*[\"']?(\d+)[\"']?\s*[,}]",
                                  text[config.end():] if config else "")
                if not match:
                    errors.append("页面缺少 CONFIG.acid")
                    unavailable.append(False)
                    continue
                self.acid = match.group(1)
                self.origin = candidate
                print(f"认证区域已从门户自动识别：ac_id={self.acid}")
                return self.acid
            except HTTPError as error:
                reason = http_reason(error)
                errors.append(reason)
                unavailable.append(error.code in (502, 503, 504) or "转发层 DNS" in reason)
            except (URLError, OSError) as error:
                errors.append(connection_reason(error))
                unavailable.append(True)
            except ValueError:
                errors.append("认证页面格式无效")
                unavailable.append(False)
        error_type = PortalUnavailable if unavailable and all(unavailable) else PortalError
        raise error_type("无法自动识别认证区域：" + "; ".join(errors) + "；可配置 ZJU_AC_ID")

    def request(self, endpoint, params=None, origin=None):
        query = dict(params or {})
        query.update(callback="automatic_reconnection", _=str(int(time.time() * 1000)))
        origins = ([origin] + [item for item in self.origins if item != origin]
                   if origin else self.origins)
        errors = []
        unavailable = []
        for candidate in origins:
            url = candidate + "/cgi-bin/" + endpoint + "?" + urlencode(query)
            try:
                request = Request(url, headers={"User-Agent": "Automatic-reconnection/1.0"})
                with self.opener.open(request, timeout=self.timeout) as response:
                    text = response.read(1024 * 1024).decode("utf-8").strip()
                # 固定回调名，不执行服务端返回的 JavaScript。
                prefix = "automatic_reconnection("
                if text.startswith(prefix):
                    text = text[len(prefix):].removesuffix(";").removesuffix(")")
                data = json.loads(text)
                if not isinstance(data, dict):
                    raise ValueError("Unexpected response")
                self.origin = candidate
                if candidate == self.ip_origin and not self.reported_ip_fallback:
                    print("认证接口已切换到固定 IP，绕过域名解析/中间转发层 DNS。")
                    self.reported_ip_fallback = True
                return data
            except HTTPError as error:
                reason = http_reason(error)
                errors.append(f"{candidate} {reason}")
                unavailable.append(error.code in (502, 503, 504) or "转发层 DNS" in reason)
            except (URLError, OSError, ValueError) as error:
                if (isinstance(error, ssl.SSLCertVerificationError)
                        or isinstance(getattr(error, "reason", None), ssl.SSLCertVerificationError)):
                    raise PortalError("认证接口 HTTPS 证书校验失败") from None
                # 不输出包含账号、摘要或 info 的请求 URL。
                reason = "响应不是有效 JSON/JSONP" if isinstance(error, ValueError) else connection_reason(error)
                errors.append(f"{candidate} {reason}")
                unavailable.append(not isinstance(error, ValueError))
        error_type = PortalUnavailable if unavailable and all(unavailable) else PortalError
        raise error_type(f"{endpoint} 接口不可用：" + "; ".join(errors))

    def status(self):
        return self.request("rad_user_info", origin=self.origin)

    @staticmethod
    def is_online(data):
        return data.get("error") == "ok" and bool(data.get("user_name") or data.get("username"))

    def reconnect(self, username, password, initial_status=None):
        status = self.status() if initial_status is None else initial_status
        if self.is_online(status):
            print("原生认证接口确认当前连接已在线。")
            return True
        if not self.acid:
            self.discover_acid()
        challenge = self.request("get_challenge", {"username": username}, origin=self.origin)
        token = challenge.get("challenge")
        ip = challenge.get("client_ip")
        if challenge.get("error") != "ok" or not token or not ip:
            raise PortalError("无法获取认证 challenge 或客户端 IP")
        try:
            ipaddress.IPv4Address(ip)
        except ipaddress.AddressValueError:
            raise PortalError("认证服务器未返回有效客户端 IPv4 地址") from None
        # 使用服务器识别的校园网 IP，不使用容器的 172.x 地址。
        params = login_parameters(username, password, ip, self.acid, token)
        result = self.request("srun_portal", params, origin=self.origin)
        if result.get("error") not in ("ok", "ip_already_online_error"):
            code = result.get("ecode") or result.get("error") or "unknown_error"
            raise PortalError(f"认证服务器拒绝登录（{code}）")
        for _ in range(5):
            status = self.status()
            if self.is_online(status):
                account = status.get("user_name") or status.get("username")
                domain = status.get("domain") or ""
                if username not in (account, account + domain):
                    raise PortalError("认证后在线账号与配置账号不一致")
                print(">>> 原生接口认证成功，已通过服务器在线状态校验。")
                return True
            time.sleep(1)
        raise PortalError("认证请求已提交，但服务器未确认上线")
