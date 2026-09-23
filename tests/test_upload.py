import uuid

import pytest
from fastapi.testclient import TestClient

from autoprotocol.config import Settings
from autoprotocol.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("autoprotocol.main.probe", lambda *args: 1.0)
    config = Settings(data_dir=tmp_path, max_upload_mb=1, _env_file=None)
    with TestClient(create_app(config), base_url="http://localhost") as client:
        yield client


def upload(client, **kwargs):
    return client.post("/api/meetings?title=Test&consent=true&timezone=Asia/Almaty", **kwargs)


def test_upload_persists_and_retry_does_not_duplicate(client):
    headers = {"Content-Type": "application/octet-stream", "Idempotency-Key": str(uuid.uuid4())}
    first = upload(client, content=b"fake-media", headers=headers)
    assert first.status_code == 202
    second = upload(client, content=b"fake-media", headers=headers)
    assert second.json()["id"] == first.json()["id"]
    assert len(client.get("/api/meetings").json()) == 1
    record = client.get(f"/api/meetings/{first.json()['id']}").json()
    assert record["status"] == "queued"
    assert record["occurred_on"] is None
    assert "source_path" not in record
    assert client.get(f"/api/meetings/{record['id']}/result").status_code == 409


@pytest.mark.parametrize(
    "query",
    [
        "title=Test&consent=false",
        "title=Test&consent=true&timezone=Invalid/Zone",
        "title=Test&consent=true&occurred_on=not-a-date",
        "title=%20%20&consent=true",
    ],
)
def test_upload_metadata_validation(client, query):
    response = client.post(
        f"/api/meetings?{query}", content=b"x", headers={"Content-Type": "application/octet-stream"}
    )
    assert response.status_code == 422
    assert client.get("/api/meetings").json() == []


def test_stream_limit_without_content_length(client):
    response = upload(
        client,
        content=iter([b"x" * 600000, b"x" * 600000]),
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 413
    assert client.get("/api/meetings").json() == []


def test_corrupt_media_is_not_queued(client, monkeypatch):
    def reject(*args):
        raise ValueError("Повреждённая запись")

    monkeypatch.setattr("autoprotocol.main.probe", reject)
    response = upload(client, content=b"bad", headers={"Content-Type": "application/octet-stream"})
    assert response.status_code == 422
    assert client.get("/api/meetings").json() == []


def test_cross_origin_upload_and_unknown_host_are_rejected(client):
    response = upload(
        client,
        content=b"x",
        headers={"Content-Type": "application/octet-stream", "Origin": "https://unrelated.example"},
    )
    assert response.status_code == 403
    assert client.get("/", headers={"Host": "unrelated.example"}).status_code == 400


def test_empty_file_rejected(client):
    assert (
        upload(
            client, content=b"", headers={"Content-Type": "application/octet-stream"}
        ).status_code
        == 422
    )
