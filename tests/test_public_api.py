from __future__ import annotations

import argparse
import asyncio
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import pytest

from proxy_checker import (
    CheckHistory,
    CheckOptions,
    CheckResult,
    CheckStatus,
    ErrorCode,
    ErrorInfo,
    ProxyConfig,
    ProxyInfo,
    ProxyType,
    QualityScore,
    check_proxies,
    check_proxy,
    score_quality,
)


@asynccontextmanager
async def local_http_server(
    handler,
) -> AsyncIterator[str]:
    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            first_line = request.split(b"\r\n", 1)[0].decode("ascii")
            method, path, _ = first_line.split(" ", 2)
            body, status, content_type = handler(method, path)
            writer.write(
                f"HTTP/1.1 {status}\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Content-Type: {content_type}\r\n"
                "Connection: close\r\n\r\n".encode("ascii")
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


@asynccontextmanager
async def local_forward_proxy(
    expected_auth: bytes | None = None,
    connect_status: int | None = None,
) -> AsyncIterator[tuple[str, list[str]]]:
    requests: list[str] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            lines = request.decode("iso-8859-1").split("\r\n")
            method, target, version = lines[0].split(" ", 2)
            requests.append(target)
            if expected_auth is not None and (
                f"Proxy-Authorization: {expected_auth.decode('ascii')}".lower()
                not in (line.lower() for line in lines[1:])
            ):
                writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n")
                writer.write(b"Content-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            if method == "CONNECT":
                if connect_status is not None:
                    writer.write(
                        f"HTTP/1.1 {connect_status} Refused\r\n"
                        "Content-Length: 0\r\nConnection: close\r\n\r\n".encode("ascii")
                    )
                    await writer.drain()
                    return
                host, port = target.rsplit(":", 1)
                upstream_reader, upstream_writer = await asyncio.open_connection(
                    host.strip("[]"), int(port)
                )
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()

                async def relay(source, destination):
                    while chunk := await source.read(65536):
                        destination.write(chunk)
                        await destination.drain()

                await asyncio.gather(
                    relay(reader, upstream_writer), relay(upstream_reader, writer)
                )
                return

            parsed = urlsplit(target)
            upstream_reader, upstream_writer = await asyncio.open_connection(
                parsed.hostname, parsed.port or 80
            )
            path = parsed.path or "/"
            if parsed.query:
                path += f"?{parsed.query}"
            upstream_writer.write(
                f"{method} {path} {version}\r\n".encode("ascii")
                + b"\r\n".join(line.encode("iso-8859-1") for line in lines[1:-2])
                + b"\r\nConnection: close\r\n\r\n"
            )
            await upstream_writer.drain()
            while chunk := await upstream_reader.read(65536):
                writer.write(chunk)
                await writer.drain()
            upstream_writer.close()
            await upstream_writer.wait_closed()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", requests
    finally:
        server.close()
        await server.wait_closed()


@asynccontextmanager
async def local_socks5_proxy(
    require_auth: bool = False, reject_connection: bool = False
) -> AsyncIterator[str]:
    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            version, method_count = await reader.readexactly(2)
            methods = await reader.readexactly(method_count)
            selected_method = 2 if require_auth else 0
            assert version == 5 and selected_method in methods
            writer.write(bytes((5, selected_method)))
            await writer.drain()
            if selected_method == 2:
                auth_version, username_size = await reader.readexactly(2)
                username = await reader.readexactly(username_size)
                password_size = (await reader.readexactly(1))[0]
                password = await reader.readexactly(password_size)
                assert auth_version == 1
                assert username == b"alice" and password == b"correct"
                writer.write(b"\x01\x00")
                await writer.drain()
            version, command, _, address_type = await reader.readexactly(4)
            assert version == 5 and command == 1
            if address_type == 1:
                host = ".".join(str(part) for part in await reader.readexactly(4))
            elif address_type == 3:
                size = (await reader.readexactly(1))[0]
                host = (await reader.readexactly(size)).decode("ascii")
            else:
                raise AssertionError("Unexpected address type")
            port = int.from_bytes(await reader.readexactly(2), "big")
            if reject_connection:
                writer.write(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")
                await writer.drain()
                return
            upstream_reader, upstream_writer = await asyncio.open_connection(host, port)
            writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
            await writer.drain()

            async def relay(source, destination):
                while chunk := await source.read(65536):
                    destination.write(chunk)
                    await destination.drain()

            await asyncio.gather(
                relay(reader, upstream_writer), relay(upstream_reader, writer)
            )
            upstream_writer.close()
            await upstream_writer.wait_closed()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"socks5://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


@asynccontextmanager
async def local_socks4_proxy() -> AsyncIterator[str]:
    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request = await reader.readexactly(8)
            version, command = request[:2]
            port = int.from_bytes(request[2:4], "big")
            address = request[4:8]
            await reader.readuntil(b"\x00")  # SOCKS4 user ID
            if address == b"\x00\x00\x00\x01":
                host = (await reader.readuntil(b"\x00"))[:-1].decode("ascii")
            else:
                host = ".".join(str(part) for part in address)
            assert version == 4 and command == 1
            upstream_reader, upstream_writer = await asyncio.open_connection(
                "127.0.0.1" if host == "test.local" else host, port
            )
            writer.write(b"\x00\x5a" + request[2:4] + address)
            await writer.drain()

            async def relay(source, destination):
                while chunk := await source.read(65536):
                    destination.write(chunk)
                    await destination.drain()

            await asyncio.gather(
                relay(reader, upstream_writer), relay(upstream_reader, writer)
            )
            upstream_writer.close()
            await upstream_writer.wait_closed()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"socks4://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


@asynccontextmanager
async def local_stalling_proxy() -> AsyncIterator[str]:
    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            await reader.readuntil(b"\r\n\r\n")
            await reader.read()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


def test_quality_scoring_matches_specification() -> None:
    score = score_quality(latency_ms=100, download_mbps=10, speed_test=True)
    assert score.score == 60
    assert score.grade == "fair"
    assert score.factors

    incomplete = score_quality(latency_ms=100, download_mbps=None, speed_test=True)
    assert incomplete.score is None
    assert incomplete.grade == "unavailable"

    no_speed = score_quality(latency_ms=100, download_mbps=None, speed_test=False)
    assert no_speed.score == 40


def test_quality_scoring_includes_optional_reliability_history() -> None:
    score = score_quality(
        latency_ms=2000,
        download_mbps=20,
        speed_test=True,
        reliability_success_rate=0.5,
    )
    assert score.score == 50
    assert score.grade == "fair"
    assert any("Reliability: 10.00/20" in factor for factor in score.factors)

    no_speed = score_quality(
        latency_ms=100,
        download_mbps=None,
        speed_test=False,
        reliability_success_rate=1.0,
    )
    assert no_speed.score == 60


@pytest.mark.parametrize("rate", [-0.1, 1.1, float("nan"), True])
def test_quality_scoring_rejects_invalid_reliability_rates(rate: float) -> None:
    with pytest.raises(ValueError, match="reliability_success_rate"):
        score_quality(
            latency_ms=100,
            download_mbps=None,
            speed_test=False,
            reliability_success_rate=rate,
        )


@pytest.mark.parametrize(
    "measurements",
    [
        {"latency_ms": -1, "download_mbps": 1},
        {"latency_ms": 1, "download_mbps": -1},
        {"latency_ms": float("nan"), "download_mbps": 1},
        {"latency_ms": 1, "download_mbps": float("inf")},
    ],
)
def test_quality_scoring_rejects_invalid_measurements(
    measurements: dict[str, float],
) -> None:
    with pytest.raises(ValueError, match="finite and non-negative"):
        score_quality(**measurements)


@pytest.mark.parametrize("speed_test", ["false", 1, None])
def test_quality_scoring_rejects_non_boolean_speed_test(speed_test: object) -> None:
    with pytest.raises(ValueError, match="speed_test must be a boolean"):
        score_quality(
            latency_ms=100,
            download_mbps=1,
            speed_test=speed_test,  # type: ignore[arg-type]
        )


def test_history_tracks_bounded_recent_results_and_evicts_proxy_keys() -> None:
    history = CheckHistory(max_proxies=1, max_samples=2)
    first = ProxyConfig("proxy-one.example", 8080, type="http")
    second = ProxyConfig("proxy-two.example", 8080, type="http")

    assert history.success_rate(first) is None
    history.record(first, alive=True)
    history.record(first, alive=False)
    history.record(first, alive=True)
    assert history.success_rate(first) == 0.5

    history.record(second, alive=False)
    assert history.success_rate(first) is None
    assert history.success_rate(second) == 0


def test_history_keeps_credentials_isolated_without_exposing_them() -> None:
    history = CheckHistory()
    authenticated = ProxyConfig(
        "proxy.example", 8080, type="http", username="alice", password="secret"
    )
    other_credentials = ProxyConfig(
        "proxy.example", 8080, type="http", username="alice", password="different"
    )

    history.record(authenticated, alive=True)
    assert history.success_rate(authenticated) == 1
    assert history.success_rate(other_credentials) is None
    assert "secret" not in repr(history)


def test_history_groups_auto_and_explicit_transport_for_same_proxy() -> None:
    history = CheckHistory()
    automatic = ProxyConfig("proxy.example", 8080, type="auto")
    explicit = ProxyConfig("proxy.example", 8080, type="http")

    history.record(automatic, alive=True)
    assert history.success_rate(explicit) == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_proxies": 0},
        {"max_samples": 0},
        {"max_proxies": True},
        {"max_samples": 1.5},
        {"max_proxies": 10_001},
        {"max_samples": 101},
    ],
)
def test_history_rejects_invalid_bounds(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        CheckHistory(**kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_history_adds_reliability_points_and_skips_unknown_results():
    async with local_http_server(
        lambda _method, path: (b"ok", 200 if path == "/success" else 503, "text/plain")
    ) as endpoint:
        async with local_forward_proxy() as (proxy_url, _):
            parts = urlsplit(proxy_url)
            proxy = ProxyConfig(parts.hostname, parts.port, type=ProxyType.HTTP)
            history = CheckHistory()
            options = CheckOptions(
                timeout_ms=1000,
                speed_test=False,
                test_url=f"{endpoint}/success",
                geoip_url=None,
            )
            first = await check_proxy(proxy, options, history=history)
            second = await check_proxy(proxy, options, history=history)
            unknown = await check_proxy(
                proxy,
                CheckOptions(
                    timeout_ms=1000,
                    speed_test=False,
                    test_url=f"{endpoint}/unknown",
                    geoip_url=None,
                ),
                history=history,
            )

    assert first.status is CheckStatus.ALIVE
    assert first.quality is not None and first.quality.score == 40
    assert second.status is CheckStatus.ALIVE
    assert second.quality is not None and second.quality.score == 60
    assert unknown.status is CheckStatus.UNKNOWN
    assert history.success_rate(proxy) == 1


@pytest.mark.asyncio
async def test_proxy_connection_timeout_is_unknown_and_not_recorded(monkeypatch):
    import proxy_checker.checker as checker
    from proxy_checker.transport import ProxyFailure

    async def timeout(*_args):
        raise ProxyFailure(ErrorCode.TIMEOUT, "Timed out connecting to proxy")

    monkeypatch.setattr(checker, "request_through_proxy", timeout)
    proxy = ProxyConfig("proxy.example", 8080, type=ProxyType.HTTP)
    history = CheckHistory()
    result = await check_proxy(
        proxy,
        CheckOptions(speed_test=False, geoip_url=None),
        history=history,
    )

    assert result.status is CheckStatus.UNKNOWN
    assert result.error is not None
    assert result.error.code is ErrorCode.TIMEOUT
    assert history.success_rate(proxy) is None


@pytest.mark.asyncio
async def test_auto_timeout_followed_by_definitive_proxy_failure_is_dead(monkeypatch):
    import proxy_checker.checker as checker
    from proxy_checker.transport import ProxyFailure

    async def fail_by_protocol(_proxy, proxy_type, *_args):
        if proxy_type is ProxyType.HTTP:
            raise ProxyFailure(ErrorCode.TIMEOUT, "HTTP probe timed out")
        raise ProxyFailure(ErrorCode.CONNECTION_REFUSED, "HTTPS refused")

    monkeypatch.setattr(checker, "request_through_proxy", fail_by_protocol)
    proxy = ProxyConfig("proxy.example", 8080, type=ProxyType.AUTO)
    history = CheckHistory()
    result = await check_proxy(
        proxy,
        CheckOptions(speed_test=False, geoip_url=None),
        history=history,
    )

    assert result.status is CheckStatus.DEAD
    assert result.error is not None
    assert result.error.code is ErrorCode.CONNECTION_REFUSED
    assert history.success_rate(proxy) == 0


def test_options_reject_unbounded_resource_values():
    with pytest.raises(ValueError):
        CheckOptions(timeout_ms=0)
    with pytest.raises(ValueError):
        CheckOptions(concurrency=101)
    with pytest.raises(ValueError):
        CheckOptions(speed_test_bytes=9 * 1024 * 1024)
    with pytest.raises(ValueError):
        CheckOptions(auto_types=["http"])  # type: ignore[arg-type]


def test_socks5_unoffered_auth_method_returns_protocol_error(monkeypatch):
    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        await reader.readexactly(2)
        await reader.readexactly(1)
        writer.write(b"\x05\x02")
        await writer.drain()
        writer.close()

    async def run():
        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            result = await check_proxy(
                ProxyConfig("127.0.0.1", port, type=ProxyType.SOCKS5),
                CheckOptions(
                    speed_test=False,
                    geoip_url=None,
                    test_url="http://127.0.0.1:8000/",
                ),
            )
        finally:
            server.close()
            await server.wait_closed()
        return result

    result = asyncio.run(run())
    assert result.status is CheckStatus.DEAD
    assert result.error is not None
    assert result.error.code is ErrorCode.PROTOCOL_ERROR


def test_https_proxy_tls_failure_is_not_connection_refused(monkeypatch):
    import proxy_checker.transport as transport

    async def fail_tls(*args, **kwargs):
        raise ssl.SSLError("certificate validation failed")

    monkeypatch.setattr(transport.asyncio, "open_connection", fail_tls)
    result = asyncio.run(
        check_proxy(
            ProxyConfig("proxy.example", 8443, type=ProxyType.HTTPS),
            CheckOptions(speed_test=False, geoip_url=None),
        )
    )
    assert result.status is CheckStatus.DEAD
    assert result.error is not None
    assert result.error.code is ErrorCode.PROTOCOL_ERROR


@pytest.mark.parametrize(
    "value",
    [
        "http://127.0.0.1:8080#fragment",
        "http://127.0.0.1:8080#",
    ],
)
def test_cli_rejects_proxy_uri_fragment(value: str):
    from proxy_checker.cli import _parse_proxy

    with pytest.raises(argparse.ArgumentTypeError, match="proxy must be"):
        _parse_proxy(value)


@pytest.mark.parametrize(
    "config",
    [
        ProxyConfig(host="bad host", port=8080),
        ProxyConfig(host="localhost", port=0),
        ProxyConfig(host="localhost", port=8080, username="user\r\nInjected: yes"),
        ProxyConfig(host="localhost", port=8080, password="\ud800"),
    ],
)
def test_invalid_proxy_input_returns_sanitized_result(config: ProxyConfig) -> None:
    result = asyncio.run(check_proxy(config))
    assert result.status is CheckStatus.UNKNOWN
    assert result.error is not None
    assert result.error.code is ErrorCode.INVALID_PROXY
    assert "Injected" not in result.error.message


@pytest.mark.asyncio
async def test_http_proxy_checks_local_endpoints_and_keeps_geo_errors_independent():
    async with local_http_server(
        lambda method, path: (
            (
                b'{"ip":"203.0.113.9","country_code":"VN","country":"Vietnam"}',
                200,
                "application/json",
            )
            if path == "/geo"
            else (b"x" * 1024, 200, "application/octet-stream")
            if path.startswith("/speed")
            else (b"ok", 200, "text/plain")
        )
    ) as endpoint:
        async with local_forward_proxy() as (proxy_url, requests):
            proxy_parts = urlsplit(proxy_url)
            result = await check_proxy(
                ProxyConfig(
                    proxy_parts.hostname, proxy_parts.port, type=ProxyType.HTTP
                ),
                CheckOptions(
                    timeout_ms=1000,
                    speed_test_bytes=1024,
                    test_url=f"{endpoint}/check",
                    geoip_url=f"{endpoint}/geo",
                    speed_test_url=f"{endpoint}/speed",
                ),
            )

    assert result.status is CheckStatus.ALIVE
    assert result.proxy.type is ProxyType.HTTP
    assert result.exit_ip == "203.0.113.9"
    assert result.country is not None and result.country.code == "VN"
    assert result.latency_ms is not None and result.latency_ms >= 0
    assert result.download_mbps is not None and result.download_mbps > 0
    assert any("/geo" in target for target in requests)


@pytest.mark.asyncio
async def test_http_proxy_auth_does_not_leak_into_result():
    async with local_http_server(lambda *_: (b"ok", 200, "text/plain")) as endpoint:
        async with local_forward_proxy(b"Basic dXNlcjpwYXNz") as (proxy_url, _):
            parts = urlsplit(proxy_url)
            result = await check_proxy(
                ProxyConfig(
                    parts.hostname,
                    parts.port,
                    type=ProxyType.HTTP,
                    username="user",
                    password="pass",
                ),
                CheckOptions(
                    timeout_ms=1000,
                    speed_test=False,
                    test_url=f"{endpoint}/check",
                    geoip_url=None,
                ),
            )
    assert result.status is CheckStatus.ALIVE
    assert "pass" not in repr(result.to_dict())


@pytest.mark.asyncio
async def test_bad_http_proxy_auth_is_dead_without_exposing_credentials():
    async with local_http_server(lambda *_: (b"ok", 200, "text/plain")) as endpoint:
        async with local_forward_proxy(b"Basic dXNlcjpwYXNz") as (proxy_url, _):
            parts = urlsplit(proxy_url)
            result = await check_proxy(
                ProxyConfig(
                    parts.hostname,
                    parts.port,
                    type=ProxyType.HTTP,
                    username="user",
                    password="wrong",
                ),
                CheckOptions(
                    timeout_ms=1000,
                    speed_test=False,
                    test_url=f"{endpoint}/check",
                    geoip_url=None,
                ),
            )
    assert result.status is CheckStatus.DEAD
    assert result.error is not None
    assert result.error.code is ErrorCode.AUTH_FAILED
    assert "wrong" not in repr(result.to_dict())


@pytest.mark.asyncio
async def test_geoip_failure_does_not_change_alive_status():
    async with local_http_server(lambda *_: (b"ok", 200, "text/plain")) as endpoint:
        async with local_forward_proxy() as (proxy_url, _):
            parts = urlsplit(proxy_url)
            result = await check_proxy(
                ProxyConfig(parts.hostname, parts.port),
                CheckOptions(
                    timeout_ms=1000,
                    speed_test=False,
                    test_url=f"{endpoint}/check",
                    geoip_url=f"{endpoint}/missing",
                ),
            )

    assert result.status is CheckStatus.ALIVE
    assert result.exit_ip is None
    assert result.error is not None
    assert result.error.code is ErrorCode.GEOIP_ERROR


@pytest.mark.asyncio
async def test_socks5_proxy_tunnels_to_local_test_endpoint():
    async with local_http_server(lambda *_: (b"ok", 200, "text/plain")) as endpoint:
        async with local_socks5_proxy(require_auth=True) as proxy_url:
            parts = urlsplit(proxy_url)
            result = await check_proxy(
                ProxyConfig(
                    parts.hostname,
                    parts.port,
                    type=ProxyType.SOCKS5,
                    username="alice",
                    password="correct",
                ),
                CheckOptions(
                    timeout_ms=1000,
                    speed_test=False,
                    test_url=f"{endpoint}/check",
                    geoip_url=None,
                ),
            )
    assert result.status is CheckStatus.ALIVE
    assert result.proxy.type is ProxyType.SOCKS5
    assert "correct" not in repr(result.to_dict())


@pytest.mark.asyncio
async def test_auto_can_select_enabled_socks5_protocol():
    async with local_http_server(lambda *_: (b"ok", 200, "text/plain")) as endpoint:
        async with local_socks5_proxy() as proxy_url:
            parts = urlsplit(proxy_url)
            result = await check_proxy(
                ProxyConfig(parts.hostname, parts.port),
                CheckOptions(
                    timeout_ms=1000,
                    speed_test=False,
                    test_url=f"{endpoint}/check",
                    geoip_url=None,
                    auto_types=(ProxyType.SOCKS5,),
                ),
            )
    assert result.status is CheckStatus.ALIVE
    assert result.proxy.type is ProxyType.SOCKS5


@pytest.mark.asyncio
async def test_socks4a_proxy_tunnels_to_local_test_endpoint():
    async with local_http_server(lambda *_: (b"ok", 200, "text/plain")) as endpoint:
        async with local_socks4_proxy() as proxy_url:
            parts = urlsplit(proxy_url)
            result = await check_proxy(
                ProxyConfig(
                    parts.hostname,
                    parts.port,
                    type=ProxyType.SOCKS4,
                    username="userid",
                ),
                CheckOptions(
                    timeout_ms=1000,
                    speed_test=False,
                    test_url=f"{endpoint.replace('127.0.0.1', 'test.local')}/check",
                    geoip_url=None,
                ),
            )
    assert result.status is CheckStatus.ALIVE
    assert result.proxy.type is ProxyType.SOCKS4


@pytest.mark.asyncio
async def test_proxy_endpoint_refusal_is_unknown_not_dead():
    async with local_forward_proxy(connect_status=502) as (proxy_url, _):
        parts = urlsplit(proxy_url)
        result = await check_proxy(
            ProxyConfig(parts.hostname, parts.port, type=ProxyType.HTTP),
            CheckOptions(
                timeout_ms=1000,
                speed_test=False,
                test_url="https://127.0.0.1:8000/check",
                geoip_url=None,
            ),
        )
    assert result.status is CheckStatus.UNKNOWN
    assert result.error is not None
    assert result.error.code is ErrorCode.TEST_ENDPOINT_ERROR


@pytest.mark.asyncio
async def test_endpoint_tls_verification_failure_is_reported_specifically(
    monkeypatch,
):
    async def fail_verification(*_args, **_kwargs):
        raise ssl.SSLCertVerificationError("unable to get local issuer certificate")

    async def close_immediately(_reader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    monkeypatch.setattr(asyncio.StreamWriter, "start_tls", fail_verification)
    endpoint = await asyncio.start_server(close_immediately, "127.0.0.1", 0)
    endpoint_port = endpoint.sockets[0].getsockname()[1]
    try:
        async with local_forward_proxy() as (proxy_url, _):
            parts = urlsplit(proxy_url)
            result = await check_proxy(
                ProxyConfig(parts.hostname, parts.port, type=ProxyType.HTTP),
                CheckOptions(
                    timeout_ms=1000,
                    speed_test=False,
                    test_url=f"https://127.0.0.1:{endpoint_port}/check",
                    geoip_url=None,
                ),
            )
    finally:
        endpoint.close()
        await endpoint.wait_closed()
    assert result.status is CheckStatus.UNKNOWN
    assert result.error is not None
    assert result.error.code is ErrorCode.TEST_ENDPOINT_ERROR
    assert "certificate verification" in result.error.message


@pytest.mark.asyncio
async def test_socks5_destination_refusal_is_unknown_not_dead():
    async with local_socks5_proxy(reject_connection=True) as proxy_url:
        parts = urlsplit(proxy_url)
        result = await check_proxy(
            ProxyConfig(parts.hostname, parts.port, type=ProxyType.SOCKS5),
            CheckOptions(
                timeout_ms=1000,
                speed_test=False,
                test_url="http://127.0.0.1:8000/check",
                geoip_url=None,
            ),
        )
    assert result.status is CheckStatus.UNKNOWN
    assert result.error is not None
    assert result.error.code is ErrorCode.TEST_ENDPOINT_ERROR


@pytest.mark.asyncio
async def test_geo_provider_failure_field_keeps_connection_alive():
    async with local_http_server(
        lambda *_: (
            b'{"success":false,"message":"private response"}',
            200,
            "application/json",
        )
    ) as endpoint:
        async with local_forward_proxy() as (proxy_url, _):
            parts = urlsplit(proxy_url)
            result = await check_proxy(
                ProxyConfig(parts.hostname, parts.port),
                CheckOptions(
                    timeout_ms=1000,
                    speed_test=False,
                    test_url=f"{endpoint}/check",
                    geoip_url=f"{endpoint}/geo",
                ),
            )
    assert result.status is CheckStatus.ALIVE
    assert result.error is not None
    assert result.error.code is ErrorCode.GEOIP_ERROR
    assert "private response" not in result.error.message


@pytest.mark.asyncio
async def test_redirect_is_not_followed_and_is_not_reported_as_alive():
    async with local_http_server(lambda *_: (b"", 302, "text/plain")) as endpoint:
        async with local_forward_proxy() as (proxy_url, _):
            parts = urlsplit(proxy_url)
            result = await check_proxy(
                ProxyConfig(parts.hostname, parts.port),
                CheckOptions(
                    timeout_ms=1000,
                    speed_test=False,
                    test_url=f"{endpoint}/redirect",
                    geoip_url=None,
                ),
            )
    assert result.status is CheckStatus.UNKNOWN
    assert result.error is not None
    assert result.error.code is ErrorCode.TEST_ENDPOINT_ERROR


@pytest.mark.asyncio
async def test_refused_proxy_connection_is_dead():
    server = await asyncio.start_server(lambda *_: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    proxy = ProxyConfig("127.0.0.1", port, type=ProxyType.HTTP)
    history = CheckHistory()
    result = await check_proxy(
        proxy,
        CheckOptions(speed_test=False, geoip_url=None),
        history=history,
    )
    assert result.status is CheckStatus.DEAD
    assert result.error is not None
    assert result.error.code is ErrorCode.CONNECTION_REFUSED
    assert history.success_rate(proxy) == 0


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/check",
        "http://user:secret@example.com/check",
        "http://example.com/check#fragment",
        "http://example.com/check#",
    ],
)
def test_urls_with_unsafe_schemes_or_credentials_are_rejected(url: str) -> None:
    result = asyncio.run(
        check_proxy(
            ProxyConfig("127.0.0.1", 8080),
            CheckOptions(test_url=url),
        )
    )
    assert result.status is CheckStatus.UNKNOWN
    assert result.error is not None
    assert result.error.code is ErrorCode.INVALID_PROXY
    assert "secret" not in result.error.message


@pytest.mark.asyncio
async def test_endpoint_response_body_is_capped():
    async with local_http_server(
        lambda *_: (b"x" * 100, 200, "application/octet-stream")
    ) as endpoint:
        async with local_forward_proxy() as (proxy_url, _):
            parts = urlsplit(proxy_url)
            result = await check_proxy(
                ProxyConfig(parts.hostname, parts.port),
                CheckOptions(
                    timeout_ms=1000,
                    speed_test=False,
                    max_download_bytes=32,
                    test_url=f"{endpoint}/large",
                    geoip_url=None,
                ),
            )
    assert result.status is CheckStatus.UNKNOWN
    assert result.error is not None
    assert result.error.code is ErrorCode.TEST_ENDPOINT_ERROR


@pytest.mark.asyncio
async def test_batch_is_bounded_and_preserves_each_result():
    async with local_http_server(lambda *_: (b"ok", 200, "text/plain")) as endpoint:
        async with local_forward_proxy() as (proxy_url, _):
            parts = urlsplit(proxy_url)
            proxies = [ProxyConfig(parts.hostname, parts.port) for _ in range(3)]
            results = await check_proxies(
                proxies,
                CheckOptions(
                    timeout_ms=1000,
                    speed_test=False,
                    test_url=f"{endpoint}/",
                    geoip_url=None,
                    concurrency=1,
                ),
            )
    assert len(results) == 3
    assert all(result.status is CheckStatus.ALIVE for result in results)


@pytest.mark.asyncio
async def test_batch_invalid_options_return_sanitized_results():
    (result,) = await check_proxies(
        [ProxyConfig("127.0.0.1", 8080)],
        options=object(),  # type: ignore[arg-type]
    )
    assert result.status is CheckStatus.UNKNOWN
    assert result.error is not None
    assert result.error.code is ErrorCode.INVALID_PROXY


@pytest.mark.asyncio
async def test_batch_rejects_non_sequence_proxy_input():
    with pytest.raises(TypeError, match="proxies must be a list or tuple"):
        await check_proxies(None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_batch_uses_shared_history_for_reliability_scores():
    async with local_http_server(lambda *_: (b"ok", 200, "text/plain")) as endpoint:
        async with local_forward_proxy() as (proxy_url, _):
            parts = urlsplit(proxy_url)
            proxy = ProxyConfig(parts.hostname, parts.port)
            history = CheckHistory()
            history.record(proxy, alive=True)
            results = await check_proxies(
                [proxy, proxy],
                CheckOptions(
                    timeout_ms=1000,
                    speed_test=False,
                    test_url=f"{endpoint}/",
                    geoip_url=None,
                    concurrency=2,
                ),
                history=history,
            )
    assert all(result.quality is not None for result in results)
    assert all(result.quality.score == 60 for result in results if result.quality)


@pytest.mark.asyncio
async def test_batch_uses_one_history_snapshot_for_duplicate_proxies(monkeypatch):
    import proxy_checker.checker as checker

    history = CheckHistory()
    proxy = ProxyConfig("proxy.example", 8080, type="http")
    snapshots: list[float | None] = []

    async def record_then_return(
        _proxy,
        _options,
        *,
        history,
        _history_snapshot,
        _use_history_snapshot,
    ):
        snapshots.append(_history_snapshot)
        history.record(proxy, alive=True)
        await asyncio.sleep(0)
        return _history_snapshot is None

    monkeypatch.setattr(checker, "check_proxy", record_then_return)
    results = await checker.check_proxies(
        [proxy, proxy],
        history=history,
    )
    assert results == [True, True]
    assert snapshots == [None, None]


@pytest.mark.asyncio
async def test_cancellation_is_propagated():
    async with local_stalling_proxy() as proxy_url:
        parts = urlsplit(proxy_url)
        task = asyncio.create_task(
            check_proxy(
                ProxyConfig(parts.hostname, parts.port),
                CheckOptions(
                    timeout_ms=10_000,
                    test_url="http://127.0.0.1:8000/",
                    geoip_url=None,
                    speed_test=False,
                ),
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_cli_exposes_json_and_help():
    from proxy_checker.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


@pytest.mark.parametrize("as_json", [True, False])
def test_cli_displays_per_proxy_error_without_credentials(
    as_json: bool, monkeypatch, capsys
):
    import proxy_checker.cli as cli

    error_result = CheckResult(
        status=CheckStatus.DEAD,
        checked_at="2026-01-01T00:00:00+00:00",
        proxy=ProxyInfo("proxy.local", 8080, ProxyType.HTTP),
        quality=QualityScore(0.0, "unavailable", ["Proxy connection failed"]),
        error=ErrorInfo(ErrorCode.AUTH_FAILED, "Proxy authentication failed"),
    )

    async def unused_check(*_args):
        return [error_result]

    def fake_run(coroutine):
        coroutine.close()
        return [error_result]

    monkeypatch.setattr(cli, "check_proxies", unused_check)
    monkeypatch.setattr(cli.asyncio, "run", fake_run)
    args = ["http://user:secret@127.0.0.1:8080"]
    if as_json:
        args.append("--json")
    assert cli.main(args) == 1
    output = capsys.readouterr().out
    assert "AUTH_FAILED" in output
    assert "secret" not in output
    if as_json:
        assert '"status": "dead"' in output
