import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_build_redis_client_defaults_to_resp2(monkeypatch):
    import redis
    import bigqmt_signal_trader.adapters.redis_common as redis_common

    captured = {}

    class FakeRedis:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(redis, "Redis", FakeRedis)

    redis_common.build_redis_client({"host": "127.0.0.1", "port": 6379, "db": 0})

    assert captured["protocol"] == 2
