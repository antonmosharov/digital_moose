import json
import time

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_debug_token_read_only_redaction_rotation_and_pagination(tmp_path, monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    with TestClient(create_app(str(tmp_path))) as client:
        admin = {"X-Moose-Request": "1"}
        store = client.app.state.store
        store.save_settings(
            Settings(
                bot_token="telegram-secret-123",
                api_key="provider-secret-456",
                news_api_key="news-secret-789",
            )
        )
        assert client.get("/api/debug").status_code == 401
        assert client.post("/api/debug-token").status_code == 403
        token = client.post("/api/debug-token", headers=admin).json()["token"]
        bearer = {"Authorization": "Bearer " + token, **admin}
        assert token not in store.state("debug_token_hash")
        store.permit_chat(-100, "Test", True)
        for i in range(1, 4):
            store.remember(
                {
                    "chat": {"id": -100},
                    "message_id": i,
                    "date": time.time(),
                    "from": {"id": 10},
                    "text": "provider-secret-456 " + token,
                }
            )
        store.log(
            "Test", "error", "news-secret-789 api_token=unknown-secret", tools_used=["read_news"]
        )
        before_changes = store.db.total_changes
        result = client.get("/api/debug?limit=2", headers=bearer)
        assert result.status_code == 200
        assert store.db.total_changes == before_changes
        data = result.json()
        assert len(data["history"]) == 2 and data["has_more"]
        assert all(
            s not in result.text
            for s in [
                token,
                "provider-secret-456",
                "news-secret-789",
                "telegram-secret-123",
                "unknown-secret",
            ]
        )
        assert "api_key" not in data["settings"]
        assert "payload" not in data["activity"][0]
        page = client.get(f"/api/debug?before={data['next_before']}&limit=2", headers=bearer).json()
        assert len(page["history"]) == 1
        for path in [
            "/api/settings",
            "/api/debug-token",
            "/api/consciousness/prefill",
            "/api/playground",
        ]:
            method = client.patch if path == "/api/settings" else client.post
            assert method(path, headers=bearer, json={}).status_code == 403
        assert client.get("/api/state", headers=bearer).status_code == 403
        assert client.post("/api/debug", headers=bearer).status_code == 405
        assert client.get("/api/debug?limit=101", headers=bearer).status_code == 422
        assert client.get("/api/debug?chat_id=-200", headers=bearer).json()["history"] == []
        replacement = client.post("/api/debug-token", headers=admin).json()["token"]
        assert client.get("/api/debug", headers=bearer).status_code == 401
        new_bearer = {"Authorization": "Bearer " + replacement}
        assert client.get("/api/debug", headers=new_bearer).status_code == 200
        assert client.delete("/api/debug-token", headers=admin).status_code == 200
        assert client.get("/api/debug", headers=new_bearer).status_code == 401
        assert token not in json.dumps(client.get("/api/state").json())


def test_debug_token_generation_requires_admin_password(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "admin-secret")
    with TestClient(create_app(str(tmp_path))) as client:
        headers = {"X-Moose-Request": "1"}
        assert client.post("/api/debug-token", headers=headers).status_code == 401
        token = client.post(
            "/api/debug-token", headers=headers, auth=("admin", "admin-secret")
        ).json()["token"]
        assert (
            client.get("/api/debug", headers={"Authorization": "Bearer " + token}).status_code
            == 200
        )
