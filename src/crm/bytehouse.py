"""ByteHouse (ClickHouse 21.8 兼容) 只读连接。"""

from __future__ import annotations

import warnings
from clickhouse_driver import Client

from src import config

# urllib3 的自签证书警告静音
warnings.filterwarnings("ignore", message="Unverified HTTPS")


_client: Client | None = None


def client() -> Client:
    global _client
    if _client is None:
        if not config.BH_PASSWORD:
            raise RuntimeError(
                "BH_PASSWORD 未配置。请在 .env 里填写 BH_PASSWORD。"
            )
        _client = Client(
            host=config.BH_HOST,
            port=config.BH_PORT,
            user=config.BH_USER,
            password=config.BH_PASSWORD,
            secure=True,
            verify=False,
            settings={"virtual_warehouse": config.BH_WAREHOUSE},
            send_receive_timeout=60,
            connect_timeout=10,
        )
    return _client


def ping() -> str:
    """返回 CH 版本号，确认连通性。"""
    row = client().execute("SELECT version()")[0]
    return row[0]
