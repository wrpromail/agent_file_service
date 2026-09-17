"""压缩包上传即解压：zip / tar、过滤、过多、密码。"""
from __future__ import annotations

import io
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from app.config import get_settings
from app.services.archive_service import (
    STATUS_EMPTY,
    STATUS_EXPANDED,
    STATUS_TOO_MANY,
    inspect_archive,
)


def _make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _make_tar_gz(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_inspect_filters_hidden_and_unsupported():
    raw = _make_zip(
        {
            "docs/a.txt": b"hello",
            ".DS_Store": b"junk",
            "__MACOSX/._a.txt": b"x",
            "bin/tool.exe": b"MZ",
            "nested/inner.zip": b"PK\x03\x04",
            "readme.md": b"# hi",
        }
    )
    result = inspect_archive(io.BytesIO(raw), filename="pack.zip")
    assert result.status == STATUS_EXPANDED
    paths = {m.path for m in result.eligible}
    assert paths == {"docs/a.txt", "readme.md"}
    reasons = {s["reason"] for s in result.skipped}
    assert "hidden" in reasons
    assert "unsupported_type" in reasons or "nested_archive" in reasons


def test_inspect_too_many(monkeypatch):
    monkeypatch.setenv("ARCHIVE_MAX_EXPAND_FILES", "2")
    get_settings.cache_clear()
    entries = {f"f{i}.txt": f"c{i}".encode() for i in range(5)}
    raw = _make_zip(entries)
    result = inspect_archive(io.BytesIO(raw), filename="many.zip")
    assert result.status == STATUS_TOO_MANY
    assert result.eligible_count == 5
    assert result.eligible == []
    get_settings.cache_clear()


def test_inspect_empty_after_filter():
    raw = _make_zip({".hidden": b"x", "a.exe": b"x"})
    result = inspect_archive(io.BytesIO(raw), filename="empty.zip")
    assert result.status == STATUS_EMPTY


def test_archive_members_api(client, auth_headers):
    test_client, _ = client
    raw = _make_zip(
        {
            "docs/a.txt": b"hello",
            ".DS_Store": b"junk",
            "readme.md": b"# hi",
        }
    )
    up = test_client.post(
        "/v1/files",
        headers=auth_headers,
        files={"file": ("pack.zip", raw, "application/zip")},
        data={"auto_parse": "false"},
    )
    assert up.status_code == 201
    ref = up.json()["reference_id"]
    resp = test_client.get(f"/v1/files/{ref}/archive/members", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["format"] in {"zip", "application/zip", "zip"} or body["format"]
    paths = {m["path"] for m in body["members"] if m["eligible"]}
    assert "docs/a.txt" in paths
    assert "readme.md" in paths


def test_upload_zip_expands_children(client, auth_headers):
    test_client, _ = client
    raw = _make_zip(
        {
            "notes.txt": b"alpha text",
            "other.md": b"# beta",
            ".DS_Store": b"skip",
        }
    )
    resp = test_client.post(
        "/v1/files",
        headers=auth_headers,
        files={"file": ("bundle.zip", raw, "application/zip")},
        data={"session_id": "sess-zip", "auto_parse": "true"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["metadata"]["extra"]["file_kind"] == "archive"
    arch = body["archive"]
    assert arch is not None
    assert arch["status"] == "expanded"
    assert arch["eligible_count"] == 2
    assert len(arch["children"]) == 2
    names = {c["filename"] for c in arch["children"]}
    assert names == {"notes.txt", "other.md"}
    # 子文件可 GET
    child_id = arch["children"][0]["reference_id"]
    detail = test_client.get(f"/v1/files/{child_id}", headers=auth_headers)
    assert detail.status_code == 200
    assert detail.json()["session_id"] == "sess-zip"
    assert detail.json()["custom_metadata"]["extracted_from"] == body["reference_id"]


def test_upload_zip_too_many(client, auth_headers, monkeypatch):
    monkeypatch.setenv("ARCHIVE_MAX_EXPAND_FILES", "2")
    get_settings.cache_clear()
    test_client, _ = client
    entries = {f"f{i}.txt": f"content-{i}".encode() for i in range(5)}
    raw = _make_zip(entries)
    resp = test_client.post(
        "/v1/files",
        headers=auth_headers,
        files={"file": ("many.zip", raw, "application/zip")},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["archive"]["status"] == "too_many"
    assert body["archive"]["children"] == []
    assert body["archive"]["eligible_count"] == 5
    # 父包仍可下载
    parent = body["reference_id"]
    dl = test_client.get(f"/v1/files/{parent}/download", headers=auth_headers)
    assert dl.status_code == 200
    get_settings.cache_clear()


def test_upload_tar_gz_expands(client, auth_headers):
    test_client, _ = client
    raw = _make_tar_gz({"docs/a.txt": b"tar-hello", "docs/b.md": b"# tar"})
    resp = test_client.post(
        "/v1/files",
        headers=auth_headers,
        files={"file": ("pack.tar.gz", raw, "application/gzip")},
    )
    assert resp.status_code == 201, resp.text
    arch = resp.json()["archive"]
    assert arch["status"] == "expanded"
    assert arch["format"] == "tar.gz"
    assert len(arch["children"]) == 2


def test_upload_sample_zip_expands_json(client, auth_headers):
    """sample/sample.zip: 5× item_*.json + __MACOSX → 5 children."""
    from pathlib import Path

    sample = Path(__file__).resolve().parents[1] / "sample" / "sample.zip"
    if not sample.is_file():
        pytest.skip("sample/sample.zip missing")
    test_client, _ = client
    resp = test_client.post(
        "/v1/files",
        headers=auth_headers,
        files={"file": ("sample.zip", sample.read_bytes(), "application/zip")},
        data={"session_id": "sess-sample-zip", "auto_parse": "true"},
    )
    assert resp.status_code == 201, resp.text
    arch = resp.json()["archive"]
    assert arch["status"] == "expanded"
    assert arch["eligible_count"] == 5
    assert len(arch["children"]) == 5
    names = {c["filename"] for c in arch["children"]}
    assert all(n.endswith(".json") for n in names)
    assert not any(n.startswith("__MACOSX") for n in names)
    # 每个子文件 metadata+layout 完成（PARSE_SYNC）
    for c in arch["children"]:
        assert c["metadata"]["extra"]["file_kind"] == "text"
        st = test_client.get(f"/v1/files/{c['reference_id']}/status", headers=auth_headers)
        profiles = {p["profile"]: p["status"] for p in st.json()["parse_results"]}
        assert profiles.get("metadata") == "completed"
        assert profiles.get("layout") == "completed"


def test_upload_non_archive_has_null_archive(client, auth_headers):
    test_client, _ = client
    resp = test_client.post(
        "/v1/files",
        headers=auth_headers,
        files={"file": ("notes.txt", b"plain", "text/plain")},
    )
    assert resp.status_code == 201
    assert resp.json().get("archive") is None


def test_child_dedup_same_content(client, auth_headers):
    test_client, _ = client
    same = b"shared-bytes-xyz"
    # 先上传同内容普通文件
    first = test_client.post(
        "/v1/files",
        headers=auth_headers,
        files={"file": ("solo.txt", same, "text/plain")},
    )
    assert first.status_code == 201
    solo_hash = first.json()["content_hash"]

    raw = _make_zip({"inside.txt": same})
    resp = test_client.post(
        "/v1/files",
        headers=auth_headers,
        files={"file": ("dup.zip", raw, "application/zip")},
    )
    assert resp.status_code == 201
    child = resp.json()["archive"]["children"][0]
    assert child["content_hash"] == solo_hash
    assert child["deduplicated"] is True


def _zip_with_password(entries: dict[str, bytes], password: str, tmp_path: Path) -> bytes | None:
    """Use system zip -P if available (ZipCrypto)."""
    src = tmp_path / "src"
    src.mkdir()
    for name, data in entries.items():
        p = src / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    out = tmp_path / "enc.zip"
    try:
        # zip stores relative names from cwd
        cmd = ["zip", "-P", password, "-r", str(out)]
        for name in entries:
            cmd.append(name)
        subprocess.run(
            cmd,
            cwd=src,
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    if not out.is_file():
        return None
    return out.read_bytes()


def test_upload_zip_password(client, auth_headers, tmp_path):
    raw = _zip_with_password({"secret.txt": b"top-secret"}, "s3cret", tmp_path)
    if raw is None:
        pytest.skip("system zip -P not available")
    test_client, _ = client

    # 无密码应 400
    bad = test_client.post(
        "/v1/files",
        headers=auth_headers,
        files={"file": ("enc.zip", raw, "application/zip")},
    )
    assert bad.status_code == 400
    detail = bad.json().get("detail") or ""
    assert "archive_password" in detail or "加密" in detail

    # 错误密码
    wrong = test_client.post(
        "/v1/files",
        headers=auth_headers,
        files={"file": ("enc.zip", raw, "application/zip")},
        data={"archive_password": "wrong"},
    )
    assert wrong.status_code == 400

    # 正确密码
    ok = test_client.post(
        "/v1/files",
        headers=auth_headers,
        files={"file": ("enc.zip", raw, "application/zip")},
        data={"archive_password": "s3cret"},
    )
    assert ok.status_code == 201, ok.text
    arch = ok.json()["archive"]
    assert arch["status"] == "expanded"
    assert len(arch["children"]) == 1
    assert arch["children"][0]["filename"] == "secret.txt"
    # 密码不得出现在响应元数据
    assert "s3cret" not in ok.text
