import io

from app.core.metrics import get_metrics
from tests.conftest import TEST_HEADERS


def test_metrics_increments_on_upload(client):
    get_metrics().reset()
    test_client, _ = client
    before = get_metrics().get("uploads_total")
    test_client.post(
        "/v1/files",
        headers=TEST_HEADERS,
        files={"file": ("m.txt", io.BytesIO(b"metrics"), "text/plain")},
    )
    assert get_metrics().get("uploads_total") == before + 1

    r = test_client.get("/metrics")
    assert r.status_code == 200
    assert "uploads_total" in r.text
