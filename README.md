# Proxy Checker SDK

Async Python library and CLI for checking **only proxy endpoints supplied by
you**. Use it only with proxies and test services you are authorized to use.
It does not discover proxies, scan address ranges, or bypass access controls.

## Install

Python 3.11 or newer is supported.

```shell
python -m pip install .
```

For development:

```shell
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check .
python -m mypy
```

## Async SDK

```python
import asyncio

from proxy_checker import (
    CheckHistory,
    CheckOptions,
    ProxyConfig,
    check_proxies,
    check_proxy,
)


async def main():
    proxy = ProxyConfig(
        host="127.0.0.1",
        port=8080,
        type="http",  # "https", "socks4", "socks5", or "auto"
    )
    options = CheckOptions(
        timeout_ms=10_000,
        speed_test=True,
        speed_test_bytes=1024 * 1024,
        concurrency=10,
    )
    history = CheckHistory()
    result = await check_proxy(proxy, options, history=history)
    print(result.to_dict())

    batch = await check_proxies([proxy], options, history=history)
    print(batch[0].status)


asyncio.run(main())
```

`check_proxy` returns a typed `CheckResult`; `to_dict()` provides JSON-compatible
data. Invalid proxy/options produce an `unknown` result with `INVALID_PROXY`.
Cancellation raises and propagates `asyncio.CancelledError`. Batch results
preserve input order; an individual proxy failure does not discard other
results. Each check has a total timeout, and batch concurrency is capped at 32.
There are no automatic retries.

## CLI

```shell
proxy-checker http://127.0.0.1:8080 --json
proxy-checker socks5://127.0.0.1:1080 --no-speed-test
python -m proxy_checker --help
```

Proxy URI schemes are `auto`, `http`, `https`, `socks4`, and `socks5`. Human
output and JSON never include proxy usernames or passwords. Credentials in
command-line arguments can be visible to local process-inspection tools; prefer
the SDK when that matters.

Options include `--timeout-ms` (100–60000), `--concurrency` (1–32),
`--test-url`, `--geoip-url`, `--no-geoip`, `--speed-url`, `--speed-bytes` (up
to 8 MiB), and `--no-speed-test`. Exit code is 0 only if every proxy is alive.

## Endpoints and privacy

The defaults contact external services **through the supplied proxy**:

* `https://example.com/` is the connectivity/latency test endpoint.
* `https://ipwho.is/` is the IP/country provider. It can observe the proxy exit
  IP and the lookup request.
* `https://speed.cloudflare.com/__down?bytes=...` serves the bounded speed-test
  payload and can observe the request and proxy exit IP.

Configure these endpoints to use services you trust, or set `geoip_url=None`
to skip GeoIP (`--no-geoip` in the CLI). Set `speed_test=False` to skip the
speed request. Do not use untrusted test endpoints: they receive requests from
the proxy and might return data to be read by the SDK. Only HTTP(S) URLs
without embedded credentials or fragments are accepted. Redirects are not
followed. Each response is capped; the default maximum is 8 MiB and the default
speed payload is 1 MiB.

HTTP proxies use absolute-form requests for HTTP endpoints and CONNECT for
HTTPS endpoints. HTTPS proxy means TLS to the proxy plus CONNECT. SOCKS4 uses
SOCKS4a for hostnames and its username is sent as the SOCKS4 user ID (SOCKS4
has no password authentication); SOCKS5 resolves hostnames through the proxy.
The destination TLS certificate is verified with Python's default trust store.
If every HTTPS check reports `TEST_ENDPOINT_ERROR` with a TLS certificate
verification message, the local trust store may be missing a root CA (seen on
Windows, where Python does not fetch roots on demand). Point `SSL_CERT_FILE` at
a CA bundle you trust, for example the one from `certifi`, rather than
disabling verification.

## Results and limitations

`status` is `alive`, `dead`, or `unknown`. A failed configured connectivity
endpoint is `unknown`; a definitive connection-refused, protocol, or
authentication failure at the proxy is `dead`. An ambiguous timeout is
`unknown`. GeoIP or speed-provider failures do not retroactively mark a
successfully checked proxy dead. GeoIP-derived exit IP/country are observations
from the selected provider, not the physical location of the proxy.

Quality uses the formulas in `docs/README.md`. `CheckHistory` is optional and
memory-only. Reuse an instance across calls or pass one to `check_proxies` to
include the 20-point recent success-rate component. It has bounded proxy/sample
counts, keeps only definitive alive/dead connection outcomes, and clears on
process exit; it is capped at 10,000 proxies and 100 samples per proxy, and
unknown endpoint results are not counted. Credentials are represented in
history keys by a per-instance keyed fingerprint and are not retained in
plaintext. Without a history object, scoring remains
backward-compatible and adds no reliability points. The CLI does not persist
history between invocations. With speed enabled, latency and speed are still
required for a numeric score. Throughput is a single bounded measurement, not a
performance guarantee.

## Development

Tests use local fake HTTP and proxy endpoints and do not make public network
requests:

```shell
python -m pytest
python -m ruff check .
python -m mypy
```
