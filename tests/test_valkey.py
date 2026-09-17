import os

import pytest

VALKEY_URL = os.getenv("VALKEY_URL", "redis://localhost:6379/15")


def _valkey_client():
    import redis

    client = redis.from_url(VALKEY_URL, decode_responses=True)
    client.ping()
    return client


@pytest.fixture(scope="module")
def valkey_client():
    try:
        client = _valkey_client()
    except Exception as exc:
        pytest.skip(f"Valkey not available at {VALKEY_URL}: {exc}")
    yield client
    client.flushdb()


def test_valkey_ping(valkey_client):
    assert valkey_client.ping() is True


def test_valkey_set_get_roundtrip(valkey_client):
    valkey_client.set("file-service:test", "ok", ex=30)
    assert valkey_client.get("file-service:test") == "ok"


def test_celery_broker_url_uses_valkey(monkeypatch):
    monkeypatch.setenv("VALKEY_URL", VALKEY_URL)
    monkeypatch.setenv("CELERY_BROKER_URL", VALKEY_URL)
    from app.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    assert settings.valkey_url == VALKEY_URL
    assert settings.celery_broker_url == VALKEY_URL
    get_settings.cache_clear()