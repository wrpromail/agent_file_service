#!/usr/bin/env python3
"""本地联调：上传图片/音频后，用用户指令做多模态识别。

依赖：
  - 本机 LiteLLM（默认 http://localhost:4000，密钥 LITELLM_API_KEY）
  - config.yaml 中 vision_model / audio_model（默认 qwen3.5:cloud）
  - 已安装依赖：pip install -e ".[dev,parsers]"

用法（仓库根目录）:
  python scripts/test_multimodal_live.py
  python scripts/test_multimodal_live.py --base-url http://127.0.0.1:8020
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import time
import wave
from pathlib import Path

import httpx
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_API = os.environ.get("FILE_SERVICE_URL", "http://127.0.0.1:8020")
API_KEY = os.environ.get("API_KEYS", "dev-api-key").split(",")[0].strip()
TENANT = os.environ.get("TENANT_ID", "live-test")


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {API_KEY}",
        "X-Tenant-Id": TENANT,
    }


def make_invoice_png() -> bytes:
    img = Image.new("RGB", (420, 140), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([8, 8, 412, 132], outline=(20, 20, 20), width=2)
    draw.text((28, 36), "Invoice INV-2026-001", fill=(0, 0, 0))
    draw.text((28, 72), "Amount: 1280.00 CNY", fill=(0, 0, 0))
    draw.text((28, 100), "Vendor: Acme Labs", fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def make_silent_wav(seconds: float = 0.4, rate: int = 16000) -> bytes:
    """生成极短静音 wav（用于探测 audio 自定义解析通路；模型可能仅返回无语音说明）。"""
    nframes = int(rate * seconds)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * nframes)
    return buf.getvalue()


def wait_job(client: httpx.Client, base: str, ref_id: str, job_id: str, timeout: float = 180.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"{base}/v1/files/{ref_id}/custom-parse/{job_id}", headers=_headers())
        r.raise_for_status()
        body = r.json()
        if body.get("status") in {"completed", "failed"}:
            return body
        time.sleep(1.5)
    raise TimeoutError(f"custom-parse job {job_id} not finished within {timeout}s")


def wait_deep_content(client: httpx.Client, base: str, ref_id: str, timeout: float = 180.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get(f"{base}/v1/files/{ref_id}/status?profile=deep", headers=_headers())
        st.raise_for_status()
        results = st.json().get("parse_results") or []
        deep = next((x for x in results if x.get("profile") == "deep"), None)
        if deep and deep.get("status") == "completed":
            c = client.get(f"{base}/v1/files/{ref_id}/content?profile=deep", headers=_headers())
            c.raise_for_status()
            return c.json()
        if deep and deep.get("status") == "failed":
            raise RuntimeError(f"deep parse failed: {deep}")
        time.sleep(1.5)
    raise TimeoutError("deep content not ready")


def main() -> int:
    parser = argparse.ArgumentParser(description="Live multimodal file-service test")
    parser.add_argument("--base-url", default=DEFAULT_API)
    parser.add_argument("--skip-auto-deep", action="store_true", help="跳过图片自动 deep")
    parser.add_argument("--skip-audio", action="store_true", help="跳过音频 custom-parse")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    print(f"→ health {base}/health")
    with httpx.Client(timeout=120.0) as client:
        health = client.get(f"{base}/health")
        print("  ", health.status_code, health.text[:200])
        health.raise_for_status()

        # ── 1) 图片自动识别（deep + 默认提示词）──────────────────────────
        if not args.skip_auto_deep:
            png = make_invoice_png()
            print("→ upload image with auto_parse=true (expect deep)")
            up = client.post(
                f"{base}/v1/files",
                headers=_headers(),
                data={"auto_parse": "true"},
                files={"file": ("live_invoice.png", png, "image/png")},
            )
            print("  status", up.status_code, up.text[:400])
            up.raise_for_status()
            body = up.json()
            ref = body["reference_id"]
            print(
                f"  ref={ref} auto_applied={body.get('auto_parse_applied')} "
                f"skip={body.get('auto_parse_skip_reason')} parse_status={body.get('parse_status')}"
            )
            if body.get("auto_parse_applied"):
                content = wait_deep_content(client, base, ref)
                texts = " ".join(b.get("text") or "" for b in content.get("blocks") or [])
                print("  deep content preview:", texts[:400])
                if "INV-2026" not in texts and "1280" not in texts and "Invoice" not in texts:
                    print("  ⚠️  未在 deep 结果中看到发票关键字（模型输出可能措辞不同）")
            else:
                print("  ⚠️  自动解析未应用，请检查 config.yaml auto_parse.image.enabled")

        # ── 2) 图片 + 用户特定指令 custom-parse ─────────────────────────
        png = make_invoice_png()
        print("→ upload image auto_parse=false, then custom-parse with user prompt")
        up = client.post(
            f"{base}/v1/files",
            headers=_headers(),
            data={"auto_parse": "false"},
            files={"file": ("live_invoice_custom.png", png, "image/png")},
        )
        up.raise_for_status()
        ref = up.json()["reference_id"]
        prompt = "请只提取发票号码和金额，用两行 Markdown 列表输出。"
        job = client.post(
            f"{base}/v1/files/{ref}/custom-parse",
            headers=_headers(),
            json={"prompt": prompt, "media_kind": "image"},
        )
        print("  create job", job.status_code, job.text[:500])
        job.raise_for_status()
        job_body = job.json()
        if job_body.get("status") not in {"completed", "failed"}:
            job_body = wait_job(client, base, ref, job_body["job_id"])
        print("  result status:", job_body.get("status"))
        print("  result_text:\n", (job_body.get("result_text") or job_body.get("error") or "")[:800])
        if job_body.get("status") != "completed":
            print("  ✗ image custom-parse failed")
            return 1

        # ── 3) 音频 + 用户特定指令 ──────────────────────────────────────
        if not args.skip_audio:
            wav = make_silent_wav()
            print("→ upload audio, custom-parse with user prompt")
            up = client.post(
                f"{base}/v1/files",
                headers=_headers(),
                data={"auto_parse": "false"},
                files={"file": ("live_silent.wav", wav, "audio/wav")},
            )
            up.raise_for_status()
            ref = up.json()["reference_id"]
            job = client.post(
                f"{base}/v1/files/{ref}/custom-parse",
                headers=_headers(),
                json={"prompt": "请转写音频内容；若无语音请明确说明。", "media_kind": "audio"},
            )
            print("  create job", job.status_code, job.text[:500])
            if job.status_code >= 400:
                print("  ⚠️  音频 custom-parse 创建失败（模型/网关可能不支持 audio 输入）")
            else:
                job_body = job.json()
                if job_body.get("status") not in {"completed", "failed"}:
                    job_body = wait_job(client, base, ref, job_body["job_id"])
                print("  result status:", job_body.get("status"))
                print("  result_text:\n", (job_body.get("result_text") or job_body.get("error") or "")[:800])

    print("✓ multimodal live checks finished")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
