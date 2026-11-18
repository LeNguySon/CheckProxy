"""Bounded, process-local history for recent proxy connection outcomes."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from collections import OrderedDict, deque

from .models import ProxyConfig
from .transport import validate_host, validate_proxy

_MAX_TRACKED_PROXIES = 10_000
_MAX_SAMPLES_PER_PROXY = 100


class CheckHistory:
    """Keep recent definitive connection outcomes in memory only.

    Proxy credentials are represented in history keys by a per-instance HMAC;
    plaintext credentials are never retained by this class.
    """

    def __init__(self, max_proxies: int = 1000, max_samples: int = 20) -> None:
        if isinstance(max_proxies, bool) or not isinstance(max_proxies, int):
            raise ValueError("max_proxies must be an integer")
        if isinstance(max_samples, bool) or not isinstance(max_samples, int):
            raise ValueError("max_samples must be an integer")
        if max_proxies < 1:
            raise ValueError("max_proxies must be at least 1")
        if max_samples < 1:
            raise ValueError("max_samples must be at least 1")
        if max_proxies > _MAX_TRACKED_PROXIES:
            raise ValueError(f"max_proxies cannot exceed {_MAX_TRACKED_PROXIES}")
        if max_samples > _MAX_SAMPLES_PER_PROXY:
            raise ValueError(f"max_samples cannot exceed {_MAX_SAMPLES_PER_PROXY}")

        self._max_proxies = max_proxies
        self._max_samples = max_samples
        self._secret = secrets.token_bytes(32)
        self._outcomes: OrderedDict[tuple[str, int, bytes], deque[bool]] = OrderedDict()
        self._lock = threading.Lock()

    def record(self, proxy: ProxyConfig, *, alive: bool) -> None:
        """Record one definitive connection result for a proxy."""
        if not isinstance(alive, bool):
            raise ValueError("alive must be a boolean")
        key = self._key(proxy)
        with self._lock:
            outcomes = self._outcomes.get(key)
            if outcomes is None:
                outcomes = deque(maxlen=self._max_samples)
                self._outcomes[key] = outcomes
            outcomes.append(alive)
            self._outcomes.move_to_end(key)
            while len(self._outcomes) > self._max_proxies:
                self._outcomes.popitem(last=False)

    def success_rate(self, proxy: ProxyConfig) -> float | None:
        """Return the recent success fraction, or None without history."""
        key = self._key(proxy)
        with self._lock:
            outcomes = self._outcomes.get(key)
            if outcomes is None:
                return None
            self._outcomes.move_to_end(key)
            return sum(outcomes) / len(outcomes)

    def clear(self) -> None:
        """Remove all recorded outcomes from memory."""
        with self._lock:
            self._outcomes.clear()

    def _snapshot(
        self, proxies: list[ProxyConfig] | tuple[ProxyConfig, ...]
    ) -> list[float | None]:
        keys: list[tuple[str, int, bytes] | None] = []
        for proxy in proxies:
            try:
                keys.append(self._key(proxy))
            except (AttributeError, TypeError, ValueError):
                keys.append(None)
        with self._lock:
            rates: list[float | None] = []
            for key in keys:
                if key is None:
                    rates.append(None)
                    continue
                outcomes = self._outcomes.get(key)
                if outcomes is None:
                    rates.append(None)
                    continue
                self._outcomes.move_to_end(key)
                rates.append(sum(outcomes) / len(outcomes))
            return rates

    def _key(self, proxy: ProxyConfig) -> tuple[str, int, bytes]:
        validate_proxy(proxy)
        credentials = b"\x00".join(
            value.encode("utf-8") if value is not None else b"\xff"
            for value in (proxy.username, proxy.password)
        )
        fingerprint = hmac.new(self._secret, credentials, hashlib.sha256).digest()
        return validate_host(proxy.host).lower(), proxy.port, fingerprint

    def __repr__(self) -> str:
        with self._lock:
            return (
                f"CheckHistory(max_proxies={self._max_proxies}, "
                f"max_samples={self._max_samples}, "
                f"tracked_proxies={len(self._outcomes)})"
            )
