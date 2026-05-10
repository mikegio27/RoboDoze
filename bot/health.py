import aiohttp.web

from utils.logging import logger


async def _healthz(_request: aiohttp.web.Request) -> aiohttp.web.Response:
    return aiohttp.web.Response(
        text='{"status":"ok"}',
        content_type="application/json",
    )


async def _readyz(bot, request: aiohttp.web.Request) -> aiohttp.web.Response:
    if bot.is_ready():
        return aiohttp.web.Response(text='{"status":"ready"}', content_type="application/json")
    return aiohttp.web.Response(status=503, text='{"status":"not ready"}', content_type="application/json")


async def start_health_server(bot, host: str = "0.0.0.0", port: int = 8080) -> aiohttp.web.AppRunner:
    app = aiohttp.web.Application()
    app.router.add_get("/healthz", _healthz)
    app.router.add_get("/readyz", lambda r: _readyz(bot, r))

    runner = aiohttp.web.AppRunner(app, access_log=None)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, host, port)
    await site.start()
    logger.info(f"Health server listening on {host}:{port}")
    return runner
