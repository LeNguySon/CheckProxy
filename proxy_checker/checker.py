"""Public asynchronous checks and documented quality scoring."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .history import CheckHistory
from .models import (
    CheckOptions,
    CheckResult,
    CheckStatus,
    CountryInfo,
    ErrorCode,
    ErrorInfo,
    ProxyConfig,
    ProxyInfo,
    ProxyType,
    QualityScore,
    utc_now_iso,
)
from .transport import (
    EndpointFailure,
    EndpointTlsVerificationFailure,
    ProxyFailure,
    request_through_proxy,
    validate_proxy,
    validate_url,
)

_GRADE_THRESHOLDS = ((85, "excellent"), (70, "good"), (50, "fair"))


def score_quality(
    *,
    latency_ms: float | None,
    download_mbps: float | None,
    speed_test: bool = True,
    reliability_success_rate: float | None = None,
) -> QualityScore:
    """Score only measured components; incomplete required measurements score null."""
    if not isinstance(speed_test, bool):
        raise ValueError("speed_test must be a boolean")
    for name, value in (
        ("latency_ms", latency_ms),
        ("download_mbps", download_mbps),
    ):
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(f"{name} must be finite and non-negative")
    if reliability_success_rate is not None and (
        isinstance(reliability_success_rate, bool)
        or not isinstance(reliability_success_rate, (int, float))
        or not math.isfinite(reliability_success_rate)
        or not 0 <= reliability_success_rate <= 1
    ):
        raise ValueError("reliability_success_rate must be between 0 and 1")
    factors: list[str] = []
    parts: list[float] = []
    if latency_ms is not None:
        points = 40 * max(0.0, min((2000 - latency_ms) / 1900, 1.0))
        parts.append(points)
        factors.append(f"Latency: {points:.2f}/40 points")
    else:
        factors.append("Latency was not measured")
    if speed_test:
        if download_mbps is not None:
            points = 40 * max(0.0, min(download_mbps / 20, 1.0))
            parts.append(points)
            factors.append(f"Download speed: {points:.2f}/40 points")
        else:
            factors.append("Download speed measurement is missing")
    else:
        factors.append("Speed test disabled")
    if reliability_success_rate is None:
        factors.append("Reliability history unavailable; no reliability points added")
    else:
        points = 20 * reliability_success_rate
        parts.append(points)
        factors.append(f"Reliability: {points:.2f}/20 points")
    required_present = latency_ms is not None and (
        not speed_test or download_mbps is not None
    )
    if not required_present:
        return QualityScore(None, "unavailable", factors)
    score = round(max(0.0, min(sum(parts), 100.0)), 2)
    grade = next(
        (name for minimum, name in _GRADE_THRESHOLDS if score >= minimum), "poor"
    )
    return QualityScore(score, grade, factors)


def _validate_options(options: CheckOptions) -> None:
    validate_url(options.test_url)
    if options.geoip_url is not None:
        validate_url(options.geoip_url)
    if options.speed_test and options.speed_test_url is not None:
        validate_url(options.speed_test_url)


def _result(
    proxy: ProxyConfig,
    proxy_type: ProxyType,
    status: CheckStatus,
    *,
    exit_ip: str | None = None,
    country: CountryInfo | None = None,
    latency_ms: float | None = None,
    download_mbps: float | None = None,
    quality: QualityScore | None = None,
    error: ErrorInfo | None = None,
) -> CheckResult:
    return CheckResult(
        status=status,
        checked_at=utc_now_iso(),
        proxy=ProxyInfo(host=proxy.host, port=proxy.port, type=proxy_type),
        exit_ip=exit_ip,
        country=country,
        latency_ms=latency_ms,
        download_mbps=download_mbps,
        quality=quality,
        error=error,
    )


async def check_proxy(
    proxy: ProxyConfig,
    options: CheckOptions | None = None,
    *,
    history: CheckHistory | None = None,
    _history_snapshot: float | None = None,
    _use_history_snapshot: bool = False,
) -> CheckResult:
    """Check one supplied proxy; cancellation propagates as CancelledError."""
    opts = CheckOptions() if options is None else options
    proxy_type = ProxyType.UNKNOWN
    try:
        proxy_type = validate_proxy(proxy)
        _validate_options(opts)
    except (ValueError, TypeError, AttributeError):
        return _result(
            proxy if isinstance(proxy, ProxyConfig) else ProxyConfig("", 0),
            ProxyType.UNKNOWN,
            CheckStatus.UNKNOWN,
            quality=QualityScore(None, "unavailable", ["Input validation failed"]),
            error=ErrorInfo(
                ErrorCode.INVALID_PROXY, "Proxy configuration or options are invalid"
            ),
        )

    stages = opts.auto_types if proxy_type is ProxyType.AUTO else (proxy_type,)
    request_proxy_type = stages[0]
    status = CheckStatus.UNKNOWN
    failure: ErrorInfo | None = None
    latency_ms: float | None = None
    exit_ip: str | None = None
    country: CountryInfo | None = None
    download_mbps: float | None = None
    speed_url = _speed_url(opts)
    current_stage = "probe"
    all_auto_attempts_failed = True
    saw_timeout = False
    timeout_failure: ErrorInfo | None = None

    try:
        async with asyncio.timeout(opts.timeout_ms / 1000):
            for candidate_type in stages:
                request_proxy_type = candidate_type
                probe_start = time.perf_counter()
                try:
                    candidate_timeout = (
                        opts.timeout_ms / 1000 / len(stages)
                        if proxy_type is ProxyType.AUTO
                        else opts.timeout_ms / 1000
                    )
                    async with asyncio.timeout(candidate_timeout):
                        response = await request_through_proxy(
                            proxy,
                            candidate_type,
                            opts.test_url,
                            opts.max_download_bytes,
                        )
                except ProxyFailure as exc:
                    failure = ErrorInfo(exc.code, str(exc))
                    if exc.code is ErrorCode.TIMEOUT:
                        saw_timeout = True
                        timeout_failure = failure
                        all_auto_attempts_failed = False
                        if proxy_type is ProxyType.AUTO:
                            continue
                        status = CheckStatus.UNKNOWN
                        break
                    if proxy_type is ProxyType.AUTO:
                        if exc.code in (
                            ErrorCode.DNS_ERROR,
                            ErrorCode.CONNECTION_REFUSED,
                            ErrorCode.AUTH_FAILED,
                        ):
                            status = CheckStatus.DEAD
                            break
                        continue
                    status = CheckStatus.DEAD
                    break
                except TimeoutError:
                    failure = ErrorInfo(
                        ErrorCode.TIMEOUT, "Proxy protocol check timed out"
                    )
                    saw_timeout = True
                    timeout_failure = failure
                    all_auto_attempts_failed = False
                    if proxy_type is ProxyType.AUTO:
                        continue
                    break
                except EndpointTlsVerificationFailure:
                    all_auto_attempts_failed = False
                    failure = ErrorInfo(
                        ErrorCode.TEST_ENDPOINT_ERROR,
                        "TLS certificate verification failed for the configured "
                        "test endpoint; check the local CA trust store",
                    )
                    break
                except (EndpointFailure, OSError, asyncio.IncompleteReadError):
                    all_auto_attempts_failed = False
                    failure = ErrorInfo(
                        ErrorCode.TEST_ENDPOINT_ERROR,
                        "Configured test endpoint did not return a valid response",
                    )
                    break
                all_auto_attempts_failed = False
                if 200 <= response.status < 300:
                    latency_ms = (time.perf_counter() - probe_start) * 1000
                    status = CheckStatus.ALIVE
                    failure = None
                    del response
                    break
                if proxy_type is ProxyType.AUTO and response.status in (400, 405, 501):
                    all_auto_attempts_failed = False
                    failure = ErrorInfo(
                        ErrorCode.TEST_ENDPOINT_ERROR,
                        f"Configured test endpoint returned HTTP {response.status}",
                    )
                    continue
                failure = ErrorInfo(
                    ErrorCode.TEST_ENDPOINT_ERROR,
                    f"Configured test endpoint returned HTTP {response.status}",
                )
                break

            if status is not CheckStatus.ALIVE:
                if saw_timeout and status is not CheckStatus.DEAD:
                    status = CheckStatus.UNKNOWN
                    failure = timeout_failure
                elif (
                    proxy_type is ProxyType.AUTO
                    and failure is not None
                    and all_auto_attempts_failed
                ):
                    status = CheckStatus.DEAD
                elif failure is None:
                    failure = ErrorInfo(
                        ErrorCode.TEST_ENDPOINT_ERROR,
                        "Configured test endpoint could not be checked",
                    )
                if history is not None and status is CheckStatus.DEAD:
                    history.record(proxy, alive=False)
                return _result(
                    proxy,
                    ProxyType.UNKNOWN if proxy_type is ProxyType.AUTO else proxy_type,
                    status,
                    latency_ms=latency_ms,
                    quality=QualityScore(
                        0.0 if status is CheckStatus.DEAD else None,
                        "unavailable",
                        ["Proxy connection failed"]
                        if status is CheckStatus.DEAD
                        else ["Connection status could not be determined"],
                    ),
                    error=failure,
                )

            current_stage = "geoip"
            if opts.geoip_url is not None:
                try:
                    geo_response = await request_through_proxy(
                        proxy,
                        request_proxy_type,
                        opts.geoip_url,
                        min(opts.max_download_bytes, 64 * 1024),
                    )
                    if not 200 <= geo_response.status < 300:
                        raise ValueError("provider returned a non-success status")
                    payload = json.loads(geo_response.body)
                    if not isinstance(payload, dict) or payload.get("success") is False:
                        raise ValueError("provider reported failure")
                    raw_ip = payload.get("ip")
                    if isinstance(raw_ip, str):
                        exit_ip = str(ipaddress.ip_address(raw_ip))
                    code = payload.get("country_code")
                    name = payload.get("country")
                    if not isinstance(code, str) or not re.fullmatch(
                        r"[A-Za-z]{2}", code
                    ):
                        code = None
                    else:
                        code = code.upper()
                    country = (
                        CountryInfo(
                            code=code,
                            name=name[:128] if isinstance(name, str) else None,
                        )
                        if code is not None or isinstance(name, str)
                        else None
                    )
                    if exit_ip is None:
                        raise ValueError("provider response did not contain a valid IP")
                except (
                    EndpointFailure,
                    ProxyFailure,
                    OSError,
                    ValueError,
                    UnicodeError,
                    json.JSONDecodeError,
                ):
                    failure = ErrorInfo(
                        ErrorCode.GEOIP_ERROR,
                        "IP geolocation provider failed or returned invalid data",
                    )

            if opts.speed_test and speed_url:
                current_stage = "speed"
                try:
                    speed_start = time.perf_counter()
                    speed_response = await request_through_proxy(
                        proxy, request_proxy_type, speed_url, opts.max_download_bytes
                    )
                    elapsed = time.perf_counter() - speed_start
                    if not 200 <= speed_response.status < 300:
                        raise ValueError("speed endpoint returned a non-success status")
                    if len(speed_response.body) != opts.speed_test_bytes:
                        raise ValueError(
                            "speed endpoint returned an unexpected payload size"
                        )
                    if elapsed > 0:
                        download_mbps = (
                            len(speed_response.body) * 8 / elapsed / 1_000_000
                        )
                except (EndpointFailure, ProxyFailure, OSError, ValueError):
                    if failure is None:
                        failure = ErrorInfo(
                            ErrorCode.SPEED_TEST_ERROR,
                            "Configured speed endpoint failed or exceeded its limit",
                        )
            elif opts.speed_test:
                failure = ErrorInfo(
                    ErrorCode.SPEED_TEST_ERROR,
                    "Speed test is enabled but no speed endpoint is configured",
                )
    except TimeoutError:
        if current_stage == "probe":
            status = CheckStatus.UNKNOWN
            failure = ErrorInfo(ErrorCode.TIMEOUT, "Proxy check timed out")
        elif current_stage == "geoip":
            failure = ErrorInfo(
                ErrorCode.GEOIP_ERROR, "IP geolocation provider timed out"
            )
        else:
            failure = ErrorInfo(ErrorCode.SPEED_TEST_ERROR, "Speed test timed out")

    if status is not CheckStatus.ALIVE:
        if current_stage == "probe" and saw_timeout:
            status = CheckStatus.UNKNOWN
            failure = timeout_failure
        if history is not None and status is CheckStatus.DEAD:
            history.record(proxy, alive=False)
        return _result(
            proxy,
            ProxyType.UNKNOWN if proxy_type is ProxyType.AUTO else proxy_type,
            status,
            latency_ms=latency_ms,
            quality=QualityScore(None, "unavailable", ["Connection status unknown"]),
            error=failure,
        )
    if history is None:
        reliability_success_rate = None
    elif _use_history_snapshot:
        reliability_success_rate = _history_snapshot
    else:
        reliability_success_rate = history.success_rate(proxy)
    result = _result(
        proxy,
        request_proxy_type,
        CheckStatus.ALIVE,
        exit_ip=exit_ip,
        country=country,
        latency_ms=latency_ms,
        download_mbps=download_mbps,
        quality=score_quality(
            latency_ms=latency_ms,
            download_mbps=download_mbps,
            speed_test=opts.speed_test,
            reliability_success_rate=reliability_success_rate,
        ),
        error=failure,
    )
    if history is not None:
        history.record(proxy, alive=True)
    return result


def _speed_url(options: CheckOptions) -> str | None:
    if not options.speed_test:
        return None
    url = options.speed_test_url
    if url is None:
        return None
    parsed = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parsed.query) if key != "bytes"]
    query.append(("bytes", str(options.speed_test_bytes)))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


async def check_proxies(
    proxies: list[ProxyConfig] | tuple[ProxyConfig, ...],
    options: CheckOptions | None = None,
    *,
    history: CheckHistory | None = None,
) -> list[CheckResult]:
    """Check each proxy with bounded workers while preserving input order."""
    if not isinstance(proxies, (list, tuple)):
        raise TypeError("proxies must be a list or tuple")
    opts = CheckOptions() if options is None else options
    if not isinstance(opts, CheckOptions):
        return await asyncio.gather(*(check_proxy(proxy, opts) for proxy in proxies))
    results: list[CheckResult | None] = [None] * len(proxies)
    pending = iter(enumerate(proxies))
    history_rates = history._snapshot(proxies) if history is not None else None

    async def worker() -> None:
        while True:
            try:
                index, proxy = next(pending)
            except StopIteration:
                return
            results[index] = await check_proxy(
                proxy,
                opts,
                history=history,
                _history_snapshot=history_rates[index] if history_rates else None,
                _use_history_snapshot=history_rates is not None,
            )

    worker_count = min(opts.concurrency, len(proxies))
    await asyncio.gather(*(worker() for _ in range(worker_count)))
    return [result for result in results if result is not None]
