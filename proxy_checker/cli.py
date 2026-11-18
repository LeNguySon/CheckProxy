"""Command-line entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from urllib.parse import unquote, urlsplit

from .checker import check_proxies
from .models import CheckOptions, ProxyConfig, ProxyType


def _parse_proxy(value: str) -> ProxyConfig:
    if "://" not in value:
        value = "auto://" + value
    try:
        parsed = urlsplit(value)
        proxy_type = ProxyType(parsed.scheme)
        if proxy_type not in (
            ProxyType.HTTP,
            ProxyType.HTTPS,
            ProxyType.SOCKS4,
            ProxyType.SOCKS5,
            ProxyType.AUTO,
        ):
            raise ValueError
        host = parsed.hostname
        port = parsed.port
        if (
            not host
            or port is None
            or parsed.path not in ("", "/")
            or parsed.query
            or "#" in value
        ):
            raise ValueError
        username = unquote(parsed.username) if parsed.username is not None else None
        password = unquote(parsed.password) if parsed.password is not None else None
        return ProxyConfig(host, port, proxy_type, username, password)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError(
            "proxy must be [type://][user:password@]host:port"
        ) from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="proxy-checker",
        description="Check only explicitly supplied, authorized proxy endpoints.",
    )
    parser.add_argument("proxies", nargs="+", type=_parse_proxy)
    parser.add_argument("--timeout-ms", type=int, default=10_000)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--test-url", default="https://example.com/")
    parser.add_argument("--geoip-url", default="https://ipwho.is/")
    parser.add_argument("--no-geoip", action="store_true", help="skip IP geolocation")
    parser.add_argument("--speed-url", default="https://speed.cloudflare.com/__down")
    parser.add_argument("--speed-bytes", type=int, default=1024 * 1024)
    parser.add_argument("--no-speed-test", action="store_true")
    parser.add_argument("--json", action="store_true", help="emit JSON results")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        options = CheckOptions(
            timeout_ms=args.timeout_ms,
            concurrency=args.concurrency,
            test_url=args.test_url,
            geoip_url=None if args.no_geoip else args.geoip_url,
            speed_test_url=args.speed_url,
            speed_test_bytes=args.speed_bytes,
            speed_test=not args.no_speed_test,
        )
    except ValueError as exc:
        parser.error(str(exc))
    results = asyncio.run(check_proxies(args.proxies, options))
    if args.json:
        print(json.dumps([result.to_dict() for result in results], ensure_ascii=False))
    else:
        for result in results:
            error = (
                f" — {result.error.code.value}: {result.error.message}"
                if result.error
                else ""
            )
            quality = (
                f" — score {result.quality.score} ({result.quality.grade})"
                if result.quality
                else ""
            )
            print(
                f"{result.proxy.host}:{result.proxy.port} "
                f"[{result.proxy.type.value}] {result.status.value}{quality}{error}"
            )
    return 0 if all(result.status.value == "alive" for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
