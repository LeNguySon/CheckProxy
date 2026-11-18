"""Small bounded HTTP transport supporting HTTP and SOCKS proxy protocols."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import re
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit

from .models import ErrorCode, ProxyConfig, ProxyType

_HEADER_LIMIT = 64 * 1024
_MAX_CREDENTIAL_LENGTH = 255


class ProxyFailure(Exception):
    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.code = code


class EndpointFailure(Exception):
    pass


class InvalidHttpResponse(EndpointFailure):
    pass


class EndpointTlsVerificationFailure(EndpointFailure):
    pass


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


def validate_proxy(proxy: ProxyConfig) -> ProxyType:
    try:
        proxy_type = ProxyType(proxy.type)
    except (ValueError, TypeError):
        raise ValueError("unsupported proxy type") from None
    if proxy_type is ProxyType.UNKNOWN:
        raise ValueError("proxy type must be a supported protocol")
    validate_host(proxy.host)
    if isinstance(proxy.port, bool) or not isinstance(proxy.port, int):
        raise ValueError("proxy port must be an integer")
    if not 1 <= proxy.port <= 65535:
        raise ValueError("proxy port must be between 1 and 65535")
    for credential in (proxy.username, proxy.password):
        if credential is None:
            continue
        if not isinstance(credential, str):
            raise ValueError("proxy credentials are invalid")
        try:
            credential_bytes = credential.encode("utf-8")
        except UnicodeError:
            raise ValueError("proxy credentials are invalid") from None
        if len(credential_bytes) > _MAX_CREDENTIAL_LENGTH or any(
            ord(char) < 32 or ord(char) == 127 for char in credential
        ):
            raise ValueError("proxy credentials are invalid")
    return proxy_type


def validate_host(host: str) -> str:
    if not isinstance(host, str) or not host or len(host) > 253:
        raise ValueError("host is invalid")
    if any(char.isspace() or ord(char) < 33 or char in "/\\@?#" for char in host):
        raise ValueError("host is invalid")
    candidate = host.rstrip(".")
    try:
        return ipaddress.ip_address(candidate).compressed
    except ValueError:
        try:
            ascii_host = candidate.encode("idna").decode("ascii")
        except UnicodeError:
            raise ValueError("host is invalid") from None
        if not ascii_host or any(
            not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in ascii_host.split(".")
        ):
            raise ValueError("host is invalid") from None
        return ascii_host


def validate_url(url: str) -> None:
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("endpoint URL is invalid")
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise ValueError("endpoint URL is invalid")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ValueError("endpoint URL is invalid") from None
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or "#" in url
    ):
        raise ValueError(
            "endpoint URL must be an HTTP(S) URL without credentials or fragment"
        )
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("endpoint URL port is invalid")
    validate_host(parsed.hostname)


async def request_through_proxy(
    proxy: ProxyConfig,
    proxy_type: ProxyType,
    url: str,
    max_bytes: int,
) -> HttpResponse:
    parsed = urlsplit(url)
    host = validate_host(parsed.hostname or "")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    if parsed.query:
        path += "?" + quote(parsed.query, safe="/?%:@!$&'()*+,;=-._~")
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    proxy_ssl = ssl.create_default_context() if proxy_type is ProxyType.HTTPS else None
    try:
        reader, writer = await asyncio.open_connection(
            proxy.host,
            proxy.port,
            ssl=proxy_ssl,
            server_hostname=proxy.host if proxy_ssl else None,
            limit=_HEADER_LIMIT,
        )
    except socket.gaierror as exc:
        raise ProxyFailure(ErrorCode.DNS_ERROR, "Could not resolve proxy host") from exc
    except ConnectionRefusedError as exc:
        raise ProxyFailure(
            ErrorCode.CONNECTION_REFUSED, "Proxy connection refused"
        ) from exc
    except TimeoutError as exc:
        raise ProxyFailure(ErrorCode.TIMEOUT, "Timed out connecting to proxy") from exc
    except ssl.SSLError as exc:
        raise ProxyFailure(
            ErrorCode.PROTOCOL_ERROR, "TLS negotiation with proxy failed"
        ) from exc
    except OSError as exc:
        raise ProxyFailure(
            ErrorCode.CONNECTION_REFUSED, "Could not connect to proxy"
        ) from exc

    try:
        if proxy_type in (ProxyType.SOCKS4, ProxyType.SOCKS5):
            try:
                await _negotiate_socks(reader, writer, proxy, proxy_type, host, port)
            except ProxyFailure:
                raise
            except (OSError, asyncio.IncompleteReadError) as exc:
                raise ProxyFailure(
                    ErrorCode.PROTOCOL_ERROR, "Proxy protocol negotiation failed"
                ) from exc
        elif parsed.scheme == "https":
            try:
                await _http_connect(reader, writer, proxy, host, port)
            except InvalidHttpResponse as exc:
                raise ProxyFailure(
                    ErrorCode.PROTOCOL_ERROR, "Proxy protocol negotiation failed"
                ) from exc
        elif proxy_type in (ProxyType.HTTP, ProxyType.HTTPS):
            try:
                return await _http_forward_request(
                    reader, writer, proxy, url, host, path, max_bytes
                )
            except InvalidHttpResponse as exc:
                raise ProxyFailure(
                    ErrorCode.PROTOCOL_ERROR, "Proxy returned an invalid response"
                ) from exc

        if parsed.scheme == "https":
            try:
                await writer.start_tls(
                    ssl.create_default_context(),
                    server_hostname=host,
                    ssl_handshake_timeout=10,
                )
            except ssl.SSLCertVerificationError as exc:
                raise EndpointTlsVerificationFailure(
                    "TLS certificate verification failed for the configured endpoint"
                ) from exc
        return await _origin_request(reader, writer, host, port, path, max_bytes)
    except (ProxyFailure, EndpointFailure):
        raise
    except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
        raise EndpointFailure("Request to configured endpoint failed") from exc
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


async def _http_forward_request(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    proxy: ProxyConfig,
    url: str,
    host: str,
    path: str,
    max_bytes: int,
) -> HttpResponse:
    parsed = urlsplit(url)
    request_url = urlunsplit(
        (
            parsed.scheme,
            _authority(host, parsed.port or 80),
            quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~"),
            quote(parsed.query, safe="/?%:@!$&'()*+,;=-._~"),
            "",
        )
    )
    authority = _authority(host, parsed.port or 80)
    headers = [
        f"GET {request_url} HTTP/1.1",
        f"Host: {authority}",
        "User-Agent: proxy-checker/0.1",
        "Accept-Encoding: identity",
        "Connection: close",
    ]
    auth = _proxy_authorization(proxy)
    if auth:
        headers.append(f"Proxy-Authorization: {auth}")
    writer.write(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
    await writer.drain()
    response = await _read_response(reader, max_bytes)
    if response.status == 407:
        raise ProxyFailure(ErrorCode.AUTH_FAILED, "Proxy authentication failed")
    return response


async def _http_connect(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    proxy: ProxyConfig,
    host: str,
    port: int,
) -> None:
    authority = _authority(host, port)
    headers = [
        f"CONNECT {authority} HTTP/1.1",
        f"Host: {authority}",
        "Proxy-Connection: Keep-Alive",
    ]
    auth = _proxy_authorization(proxy)
    if auth:
        headers.append(f"Proxy-Authorization: {auth}")
    writer.write(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
    await writer.drain()
    status, _ = await _read_headers(reader)
    if status == 407:
        raise ProxyFailure(ErrorCode.AUTH_FAILED, "Proxy authentication failed")
    if status != 200:
        raise EndpointFailure("Proxy could not connect to the configured endpoint")


async def _negotiate_socks(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    proxy: ProxyConfig,
    proxy_type: ProxyType,
    host: str,
    port: int,
) -> None:
    if proxy_type is ProxyType.SOCKS5:
        methods = b"\x00\x02" if proxy.username is not None else b"\x00"
        writer.write(b"\x05" + bytes((len(methods),)) + methods)
        await writer.drain()
        response = await reader.readexactly(2)
        if response[0] != 5 or response[1] == 0xFF:
            raise ProxyFailure(ErrorCode.AUTH_FAILED, "SOCKS5 authentication rejected")
        if response[1] == 2:
            if proxy.username is None:
                raise ProxyFailure(
                    ErrorCode.PROTOCOL_ERROR,
                    "SOCKS5 selected an authentication method that was not offered",
                )
            user = proxy.username.encode("utf-8")
            password = (proxy.password or "").encode("utf-8")
            writer.write(
                b"\x01"
                + bytes((len(user),))
                + user
                + bytes((len(password),))
                + password
            )
            await writer.drain()
            auth_response = await reader.readexactly(2)
            if auth_response != b"\x01\x00":
                raise ProxyFailure(
                    ErrorCode.AUTH_FAILED, "SOCKS5 authentication failed"
                ) from None
        elif response[1] != 0:
            raise ProxyFailure(
                ErrorCode.PROTOCOL_ERROR, "Unsupported SOCKS5 auth method"
            ) from None
        try:
            destination_ip = ipaddress.ip_address(host)
        except ValueError:
            address = host.encode("idna")
            if len(address) > 255:
                raise ProxyFailure(
                    ErrorCode.PROTOCOL_ERROR, "Destination host is too long"
                ) from None
            target = b"\x03" + bytes((len(address),)) + address
        else:
            target = bytes((1 if destination_ip.version == 4 else 4,)) + (
                destination_ip.packed
            )
        writer.write(b"\x05\x01\x00" + target + port.to_bytes(2, "big"))
        await writer.drain()
        reply = await reader.readexactly(4)
        if reply[0] != 5:
            raise ProxyFailure(ErrorCode.PROTOCOL_ERROR, "Invalid SOCKS5 response")
        if reply[1] != 0:
            raise EndpointFailure("SOCKS5 proxy could not connect to the endpoint")
        if reply[3] == 1:
            await reader.readexactly(4)
        elif reply[3] == 3:
            length = (await reader.readexactly(1))[0]
            await reader.readexactly(length)
        elif reply[3] == 4:
            await reader.readexactly(16)
        else:
            raise ProxyFailure(
                ErrorCode.PROTOCOL_ERROR, "Invalid SOCKS5 address response"
            )
        await reader.readexactly(2)
        return

    address = host.encode("idna")
    try:
        packed = ipaddress.ip_address(host).packed
        if len(packed) == 16:
            raise ProxyFailure(
                ErrorCode.PROTOCOL_ERROR, "SOCKS4 does not support IPv6 destinations"
            )
        address_request = b"\x01" + packed
        domain_request = b""
    except ProxyFailure:
        raise
    except ValueError:
        if len(address) > 255:
            raise ProxyFailure(
                ErrorCode.PROTOCOL_ERROR, "Destination host is too long"
            ) from None
        address_request = b"\x00\x00\x00\x01"
        domain_request = address + b"\x00"
    user = (proxy.username or "").encode("utf-8")
    writer.write(
        b"\x04\x01"
        + port.to_bytes(2, "big")
        + address_request
        + user
        + b"\x00"
        + domain_request
    )
    await writer.drain()
    reply = await reader.readexactly(8)
    if reply[0] != 0:
        raise ProxyFailure(ErrorCode.PROTOCOL_ERROR, "Invalid SOCKS4 response")
    if reply[1] == 0x5D:
        raise ProxyFailure(
            ErrorCode.AUTH_FAILED, "SOCKS4 user ID authentication failed"
        )
    if reply[1] != 0x5A:
        raise EndpointFailure("SOCKS4 proxy could not connect to the endpoint")


async def _origin_request(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    host: str,
    port: int,
    path: str,
    max_bytes: int,
) -> HttpResponse:
    headers = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {_authority(host, port)}\r\n"
        "User-Agent: proxy-checker/0.1\r\n"
        "Accept-Encoding: identity\r\n"
        "Connection: close\r\n\r\n"
    )
    writer.write(headers.encode("ascii"))
    await writer.drain()
    return await _read_response(reader, max_bytes)


async def _read_headers(reader: asyncio.StreamReader) -> tuple[int, dict[str, str]]:
    try:
        raw = await reader.readuntil(b"\r\n\r\n")
    except asyncio.LimitOverrunError as exc:
        raise InvalidHttpResponse("Response headers exceeded the limit") from exc
    except asyncio.IncompleteReadError as exc:
        raise InvalidHttpResponse("Incomplete HTTP response headers") from exc
    if len(raw) > _HEADER_LIMIT:
        raise InvalidHttpResponse("Response headers exceeded the limit")
    lines = raw.decode("iso-8859-1").split("\r\n")
    match = re.fullmatch(r"HTTP/1\.[01] ([0-9]{3})(?: .*)?", lines[0])
    if not match:
        raise InvalidHttpResponse("Invalid HTTP response")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        if ":" not in line:
            raise InvalidHttpResponse("Invalid HTTP response header")
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    return int(match.group(1)), headers


async def _read_response(reader: asyncio.StreamReader, max_bytes: int) -> HttpResponse:
    status, headers = await _read_headers(reader)
    if 300 <= status < 400:
        return HttpResponse(status, headers, b"")
    if "content-length" in headers:
        try:
            length = int(headers["content-length"])
        except ValueError as exc:
            raise EndpointFailure("Invalid response content length") from exc
        if length < 0 or length > max_bytes:
            raise EndpointFailure("Response exceeded the configured download limit")
        body = await reader.readexactly(length)
    elif headers.get("transfer-encoding", "").lower() == "chunked":
        body = await _read_chunked(reader, max_bytes)
    else:
        body = await reader.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise EndpointFailure("Response exceeded the configured download limit")
    return HttpResponse(status, headers, body)


async def _read_chunked(reader: asyncio.StreamReader, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        line = await reader.readline()
        try:
            size = int(line.split(b";", 1)[0].strip(), 16)
        except ValueError as exc:
            raise EndpointFailure("Invalid chunked response") from exc
        if size < 0 or total + size > max_bytes:
            raise EndpointFailure("Response exceeded the configured download limit")
        if size == 0:
            trailer_bytes = 0
            while True:
                trailer = await reader.readline()
                trailer_bytes += len(trailer)
                if trailer_bytes > _HEADER_LIMIT:
                    raise EndpointFailure(
                        "Chunked response trailers exceeded the limit"
                    )
                if trailer == b"\r\n":
                    break
                if not trailer:
                    raise EndpointFailure("Incomplete chunked response trailers")
            return b"".join(chunks)
        chunk = await reader.readexactly(size)
        if await reader.readexactly(2) != b"\r\n":
            raise EndpointFailure("Invalid chunked response")
        chunks.append(chunk)
        total += size


def _proxy_authorization(proxy: ProxyConfig) -> str | None:
    if proxy.username is None:
        return None
    credentials = f"{proxy.username}:{proxy.password or ''}".encode()
    return "Basic " + base64.b64encode(credentials).decode("ascii")


def _authority(host: str, port: int) -> str:
    try:
        if ipaddress.ip_address(host).version == 6:
            host = f"[{host}]"
    except ValueError:
        pass
    return f"{host}:{port}"
