"""Standard container log line shape for file-service API + worker."""

from __future__ import annotations

import logging

from app.log_config import (
    ACCESS_FORMAT,
    LOG_DATEFMT,
    LOG_FORMAT,
    QuietHealthAccessFilter,
    logging_dict,
)


def test_log_format_matches_control_plane() -> None:
    assert LOG_FORMAT.startswith("%(asctime)s %(levelname)s [%(name)s]")
    assert LOG_DATEFMT == "%Y-%m-%dT%H:%M:%S"
    assert "%(request_line)s" in ACCESS_FORMAT
    assert "%(status_code)s" in ACCESS_FORMAT


def test_logging_dict_api_vs_worker(monkeypatch) -> None:
    monkeypatch.setenv("SERVICE_ROLE", "api")
    api = logging_dict(level="INFO")
    assert api["formatters"]["standard"]["format"] == LOG_FORMAT
    assert "process=" not in api["formatters"]["standard"]["format"]
    assert api["filters"]["quiet_health"]["()"] == "app.log_config.QuietHealthAccessFilter"
    assert "quiet_health" in api["handlers"]["access"]["filters"]
    assert api["loggers"]["httpx"]["level"] == "WARNING"
    assert api["loggers"]["onnxruntime"]["level"] == "ERROR"

    monkeypatch.setenv("SERVICE_ROLE", "worker")
    worker = logging_dict(level="INFO")
    assert "process=%(processName)s" in worker["formatters"]["standard"]["format"]


def test_quiet_health_and_metrics_access() -> None:
    filt = QuietHealthAccessFilter()
    quiet_health = _access_record("/health")
    quiet_metrics = _access_record("/metrics")
    keep = _access_record("/v1/files")
    assert filt.filter(quiet_health) is False
    assert filt.filter(quiet_metrics) is False
    assert filt.filter(keep) is True


def _access_record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", path, "1.1", 200),
        None,
    )
