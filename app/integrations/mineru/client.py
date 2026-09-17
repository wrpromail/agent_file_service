"""MinerU 精准解析 API 客户端。

出站 JSON 请求与重试见 ``app.integrations.http``；本模块只负责 MinerU 批处理协议。
"""
from __future__ import annotations

import json
import time
import urllib.parse
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

from app.config import Settings, get_settings
from app.integrations.http import retry_policy_from_settings
from app.integrations.http.requests_json import request_json as requests_request_json


@dataclass
class MinerUBatchResult:
    batch_id: str
    records: list[dict[str, Any]]
    manifest_path: Path | None = None


class MinerUClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "file-service/0.1"})
        self._sleep = sleep

    @property
    def base_url(self) -> str:
        return self._settings.mineru_base_url.rstrip("/")

    def _auth_headers(self) -> dict[str, str]:
        token = self._settings.mineru_api_token
        if not token:
            raise RuntimeError("MINERU_API_TOKEN is not configured")
        return {"Authorization": f"Bearer {token}"}

    def _retry_policy(self):
        return retry_policy_from_settings(self._settings, service="mineru")

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 120,
    ) -> dict[str, Any]:
        req_headers = {"Accept": "application/json", **(headers or {})}
        return requests_request_json(
            self._session,
            method,
            url,
            headers=req_headers,
            json=payload,
            timeout=timeout,
            policy=self._retry_policy(),
            error_prefix="MinerU",
            sleep=self._sleep,
        )

    def _upload_file(self, upload_url: str, file_path: Path) -> int:
        # 预签名 URL 通常未将 Content-Type 纳入签名；带 Content-Type 会触发 OSS SignatureDoesNotMatch
        with file_path.open("rb") as fh:
            resp = self._session.put(
                upload_url,
                data=fh,
                headers={},
                timeout=600,
            )
        if not (200 <= resp.status_code < 300):
            raise RuntimeError(f"MinerU upload failed HTTP {resp.status_code}: {resp.text[:500]}")
        return resp.status_code

    def run_batch(
        self,
        file_path: Path,
        *,
        doc_id: str,
        output_root: Path | None = None,
        poll_interval: int | None = None,
        timeout: int | None = None,
        download_results: bool = True,
    ) -> MinerUBatchResult:
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        poll_interval = poll_interval or self._settings.mineru_poll_interval
        timeout = timeout or self._settings.mineru_timeout
        data_id = f"{doc_id}.{file_path.suffix.lstrip('.')}"

        create_response = self._request_json(
            "POST",
            f"{self.base_url}/file-urls/batch",
            payload={
                "files": [{"name": file_path.name, "data_id": data_id}],
                "model_version": "vlm",
                "enable_table": True,
                "enable_formula": True,
                "language": "ch",
            },
            headers=self._auth_headers(),
        )
        if create_response.get("code") != 0:
            raise RuntimeError(
                f"MinerU batch create failed: {json.dumps(create_response, ensure_ascii=False)}"
            )

        batch_id = create_response["data"]["batch_id"]
        upload_urls = create_response["data"]["file_urls"]
        if not upload_urls:
            raise RuntimeError("MinerU returned no upload URLs")

        self._upload_file(upload_urls[0], file_path)

        run_dir: Path | None = None
        if download_results and output_root is not None:
            run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            run_dir = output_root / run_id
            (run_dir / "results").mkdir(parents=True, exist_ok=True)

        deadline = time.time() + timeout
        last_response: dict[str, Any] | None = None
        while time.time() < deadline:
            last_response = self._request_json(
                "GET",
                f"{self.base_url}/extract-results/batch/{urllib.parse.quote(batch_id)}",
                headers=self._auth_headers(),
            )
            extract_result = last_response.get("data", {}).get("extract_result", [])
            states = {item.get("state") for item in extract_result}
            if extract_result and states.issubset({"done", "failed"}):
                break
            time.sleep(poll_interval)

        if last_response is None:
            raise RuntimeError("MinerU polling returned no response")

        records: list[dict[str, Any]] = []
        for item in last_response.get("data", {}).get("extract_result", []):
            record = {
                "doc_id": doc_id,
                "data_id": item.get("data_id"),
                "file_name": item.get("file_name"),
                "state": item.get("state"),
                "err_msg": item.get("err_msg", ""),
                "full_zip_url": item.get("full_zip_url"),
            }
            if download_results and item.get("state") == "done" and item.get("full_zip_url") and run_dir:
                zip_path = run_dir / "results" / f"{doc_id}.zip"
                with self._session.get(item["full_zip_url"], timeout=300, stream=True) as response:
                    response.raise_for_status()
                    with zip_path.open("wb") as handle:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if chunk:
                                handle.write(chunk)
                record["downloaded_zip_path"] = str(zip_path.resolve())
                record.update(self._inspect_zip(zip_path))
            records.append(record)

        manifest_path = None
        if run_dir is not None:
            manifest_path = run_dir / "run_manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "batch_id": batch_id,
                        "records": records,
                        "create_response": create_response,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

        return MinerUBatchResult(batch_id=batch_id, records=records, manifest_path=manifest_path)

    @staticmethod
    def _inspect_zip(zip_path: Path) -> dict[str, Any]:
        with zipfile.ZipFile(zip_path) as archive:
            names = archive.namelist()
            full_md_path = next((name for name in names if name.endswith("full.md")), None)
            return {
                "zip_entries_preview": names[:20],
                "has_full_md": full_md_path is not None,
                "full_md_path": full_md_path,
            }


_client: MinerUClient | None = None


def get_mineru_client() -> MinerUClient:
    global _client
    if _client is None:
        _client = MinerUClient()
    return _client


def set_mineru_client(client: MinerUClient | None) -> None:
    global _client
    _client = client
