"""Public models and bounded options for proxy checks."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class ProxyType(StrEnum):
    HTTP = "http"
    HTTPS = "https"
    SOCKS4 = "socks4"
    SOCKS5 = "socks5"
    AUTO = "auto"
    UNKNOWN = "unknown"


class CheckStatus(StrEnum):
    ALIVE = "alive"
    DEAD = "dead"
    UNKNOWN = "unknown"


class ErrorCode(StrEnum):
    INVALID_PROXY = "INVALID_PROXY"
    DNS_ERROR = "DNS_ERROR"
    CONNECTION_REFUSED = "CONNECTION_REFUSED"
    AUTH_FAILED = "AUTH_FAILED"
    TIMEOUT = "TIMEOUT"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"
    TEST_ENDPOINT_ERROR = "TEST_ENDPOINT_ERROR"
    GEOIP_ERROR = "GEOIP_ERROR"
    SPEED_TEST_ERROR = "SPEED_TEST_ERROR"
    ABORTED = "ABORTED"


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    host: str
    port: int
    type: ProxyType | str = ProxyType.AUTO
    username: str | None = None
    password: str | None = None


@dataclass(frozen=True, slots=True)
class CheckOptions:
    timeout_ms: int = 10_000
    speed_test: bool = True
    speed_test_bytes: int = 1024 * 1024
    concurrency: int = 20
    test_url: str = "https://example.com/"
    geoip_url: str | None = "https://ipwho.is/"
    speed_test_url: str | None = "https://speed.cloudflare.com/__down"
    max_download_bytes: int = 8 * 1024 * 1024
    auto_types: tuple[ProxyType, ...] = (
        ProxyType.HTTP,
        ProxyType.HTTPS,
        ProxyType.SOCKS4,
        ProxyType.SOCKS5,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.speed_test, bool):
            raise ValueError("speed_test must be a boolean")
        if isinstance(self.timeout_ms, bool) or not isinstance(self.timeout_ms, int):
            raise ValueError("timeout_ms must be an integer")
        if not 100 <= self.timeout_ms <= 60_000:
            raise ValueError("timeout_ms must be between 100 and 60000")
        if isinstance(self.concurrency, bool) or not isinstance(self.concurrency, int):
            raise ValueError("concurrency must be an integer")
        if not 1 <= self.concurrency <= 32:
            raise ValueError("concurrency must be between 1 and 32")
        if isinstance(self.speed_test_bytes, bool) or not isinstance(
            self.speed_test_bytes, int
        ):
            raise ValueError("speed_test_bytes must be an integer")
        if not 1 <= self.speed_test_bytes <= 8 * 1024 * 1024:
            raise ValueError("speed_test_bytes must be between 1 and 8388608")
        if isinstance(self.max_download_bytes, bool) or not isinstance(
            self.max_download_bytes, int
        ):
            raise ValueError("max_download_bytes must be an integer")
        if not 1 <= self.max_download_bytes <= 8 * 1024 * 1024:
            raise ValueError("max_download_bytes must be between 1 and 8388608")
        if self.speed_test and self.speed_test_bytes > self.max_download_bytes:
            raise ValueError("speed_test_bytes cannot exceed max_download_bytes")
        if not isinstance(self.auto_types, tuple) or not 1 <= len(self.auto_types) <= 4:
            raise ValueError("auto_types must contain one to four enabled protocols")
        try:
            auto_types = tuple(
                dict.fromkeys(ProxyType(item) for item in self.auto_types)
            )
        except (ValueError, TypeError):
            raise ValueError(
                "auto_types must contain enabled proxy protocols"
            ) from None
        if not auto_types or any(
            item
            not in (ProxyType.HTTP, ProxyType.HTTPS, ProxyType.SOCKS4, ProxyType.SOCKS5)
            for item in auto_types
        ):
            raise ValueError("auto_types must contain enabled proxy protocols")
        object.__setattr__(self, "auto_types", auto_types)


@dataclass(frozen=True, slots=True)
class ProxyInfo:
    host: str
    port: int
    type: ProxyType


@dataclass(frozen=True, slots=True)
class CountryInfo:
    code: str | None = None
    name: str | None = None


@dataclass(frozen=True, slots=True)
class QualityScore:
    score: float | None
    grade: str
    factors: list[str]


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    code: ErrorCode
    message: str


@dataclass(frozen=True, slots=True)
class CheckResult:
    status: CheckStatus
    checked_at: str
    proxy: ProxyInfo
    exit_ip: str | None = None
    country: CountryInfo | None = None
    latency_ms: float | None = None
    download_mbps: float | None = None
    quality: QualityScore | None = None
    error: ErrorInfo | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the documented JSON-compatible result shape."""
        return asdict(self)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()
