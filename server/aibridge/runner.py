from __future__ import annotations

import asyncio
import logging
from logging.handlers import RotatingFileHandler

import uvicorn

from .settings import settings


LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def configure_logging() -> None:
    settings.log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(LOG_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    log_file = RotatingFileHandler(
        settings.log_path,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    log_file.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    root.addHandler(console)
    root.addHandler(log_file)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True


async def main() -> None:
    from .app import admin_app, gateway_app

    gateway = uvicorn.Server(uvicorn.Config(gateway_app, host=settings.gateway_host, port=settings.gateway_port, log_level="info", access_log=False, log_config=None))
    admin = uvicorn.Server(uvicorn.Config(admin_app, host=settings.admin_host, port=settings.admin_port, log_level="info", access_log=False, log_config=None))
    await asyncio.gather(gateway.serve(), admin.serve())


if __name__ == "__main__":
    configure_logging()
    logger = logging.getLogger(__name__)
    logger.info("AiBridge 正在启动，日志文件：%s", settings.log_path)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("AiBridge 收到中断信号，正在停止")
    except BaseException:
        logger.exception("AiBridge 因未处理异常退出")
        raise
    else:
        logger.info("AiBridge 已正常停止")
