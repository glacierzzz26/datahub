"""鉴权用例：无 token → 401；错 token → 401；对 token → 200；未配置 → fail-closed。"""
from fastapi.testclient import TestClient

from app import config
from app.server import build_app


def _client() -> TestClient:
    return TestClient(build_app())


def test_healthz_no_auth():
    r = _client().get("/v1/healthz")
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "ok"


def test_datasets_missing_token_401(auth_token):
    r = _client().get("/v1/datasets")
    assert r.status_code == 401


def test_datasets_wrong_token_401(auth_token):
    r = _client().get("/v1/datasets", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_datasets_ok_with_token(auth_token):
    r = _client().get("/v1/datasets", headers={"Authorization": f"Bearer {auth_token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    ids = [d["id"] for d in body["data"]]
    assert "hotspot.indices" in ids
    assert len(ids) == 6


def test_token_not_configured_fails_closed(monkeypatch):
    monkeypatch.setattr(config, "TOKEN", "")
    r = _client().get("/v1/datasets", headers={"Authorization": "Bearer anything"})
    assert r.status_code == 401
