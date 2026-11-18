"""Async proxy validation SDK."""

from .checker import check_proxies, check_proxy, score_quality
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
)

__all__ = [
    "CheckOptions",
    "CheckHistory",
    "CheckResult",
    "CheckStatus",
    "CountryInfo",
    "ErrorCode",
    "ErrorInfo",
    "ProxyConfig",
    "ProxyInfo",
    "ProxyType",
    "QualityScore",
    "check_proxies",
    "check_proxy",
    "score_quality",
]
