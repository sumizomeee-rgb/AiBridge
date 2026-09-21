from __future__ import annotations

import asyncio

import uvicorn

from .app import admin_app, gateway_app
from .settings import settings


async def main() -> None:
    gateway = uvicorn.Server(uvicorn.Config(gateway_app, host=settings.gateway_host, port=settings.gateway_port, log_level="info", access_log=False))
    admin = uvicorn.Server(uvicorn.Config(admin_app, host=settings.admin_host, port=settings.admin_port, log_level="info", access_log=False))
    await asyncio.gather(gateway.serve(), admin.serve())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
