import asyncio

from proxy_checker import CheckOptions, ProxyConfig, check_proxy


async def main() -> None:
    result = await check_proxy(
        ProxyConfig("127.0.0.1", 8080, type="http"),
        CheckOptions(speed_test=False),
    )
    print(result.to_dict())


if __name__ == "__main__":
    asyncio.run(main())
