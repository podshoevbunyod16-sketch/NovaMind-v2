"""Тесты Nova Receipts: цепочка, подписи, API и интеграция с чатом."""
import json
import os

import database
import receipts


def _issue(**overrides):
    payload = dict(kind="chat", chat_id="chat-1", owner="anon:test", provider="groq",
                   model="test-model", prompt="Вопрос", output="Ответ",
                   tools=["web_search"], usage={"total_tokens": 42, "cost_usd": 0.0001})
    payload.update(overrides)
    return receipts.issue(**payload)


def test_chain_is_created_and_verified(client):
    first = _issue(output="первый")
    second = _issue(output="второй")
    third = _issue(output="третий")

    assert first["id"] == 1
    assert second["prev_hash"] == first["hash"]
    assert third["prev_hash"] == second["hash"]
    assert first["prev_hash"] == receipts.GENESIS
    assert len(first["hash"]) == 64

    report = receipts.verify()
    assert report["ok"] is True
    assert report["checked"] == 3
    assert report["head_hash"] == third["hash"]
    assert report["algorithm"] == "sha256+hmac-sha256/v1"


def test_single_receipt_verification(client):
    entry = _issue()
    check = receipts.verify_one(entry["id"])
    assert check["ok"] is True
    assert check["reasons"] == []
    assert receipts.verify_one(99999)["ok"] is False


def test_tampering_is_detected(client):
    first = _issue(output="оригинал")
    _issue(output="второй")

    with database.get_db() as conn:
        conn.execute("UPDATE receipts SET output_sha256=? WHERE id=?",
                     ("0" * 64, first["id"]))
        conn.commit()

    report = receipts.verify()
    assert report["ok"] is False
    assert report["broken_count"] >= 1
    assert report["broken"][0]["id"] == first["id"]
    assert "hash" in report["broken"][0]["reasons"]
    assert receipts.verify_one(first["id"])["ok"] is False


def test_deletion_breaks_chain(client):
    _issue(output="один")
    middle = _issue(output="два")
    _issue(output="три")

    with database.get_db() as conn:
        conn.execute("DELETE FROM receipts WHERE id=?", (middle["id"],))
        conn.commit()

    report = receipts.verify()
    assert report["ok"] is False
    broken_ids = {item["id"] for item in report["broken"]}
    # Разрыв виден на следующей записи: её prev_hash больше не существует.
    assert middle["id"] + 1 in broken_ids


def test_signature_secret_mismatch_is_detected(client, monkeypatch):
    entry = _issue()
    monkeypatch.setenv("RECEIPT_SECRET", "другой-секрет")
    report = receipts.verify()
    assert report["ok"] is False
    assert report["broken"][0]["id"] == entry["id"]
    assert "signature" in report["broken"][0]["reasons"]


def _current_owner(client):
    """Владелец текущей тестовой сессии (browser id создаётся первым запросом)."""
    client.get("/api/receipts")
    with client.session_transaction() as session:
        return "anon:" + str(session.get("nova_sid") or "")


def test_api_lists_only_own_receipts(client):
    _issue(owner="anon:чужой")
    _issue(owner="anon:свой")
    _issue(owner=_current_owner(client))

    response = client.get("/api/receipts")
    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True
    assert len(data["items"]) == 1
    assert data["stats"]["total"] == 1


def test_receipt_detail_hides_foreign_receipts(client):
    owner = _current_owner(client)
    foreign = _issue(owner="anon:чужой", chat_id="")
    response = client.get(f"/api/receipts/{foreign['id']}")
    assert response.status_code == 404

    mine = _issue(owner=owner, chat_id="")
    allowed = client.get(f"/api/receipts/{mine['id']}")
    assert allowed.status_code == 200
    payload = allowed.get_json()
    assert payload["receipt"]["verification"]["ok"] is True


def test_verify_endpoint_and_export(client):
    _issue(owner=_current_owner(client))
    report = client.get("/api/receipts/verify").get_json()["report"]
    assert report["ok"] is True
    assert report["checked"] == 1
    assert report["mine"]["total"] == 1

    export = client.get("/api/receipts/export")
    assert export.status_code == 200
    assert export.mimetype == "application/x-ndjson"
    lines = [line for line in export.get_data(as_text=True).splitlines() if line]
    assert lines and json.loads(lines[0])["hash"]


def test_receipts_can_be_disabled(client, monkeypatch):
    monkeypatch.setenv("RECEIPTS_ENABLED", "0")
    assert _issue() == {}
    assert receipts.stats()["total"] == 0


def test_chat_stream_attaches_receipt(client, monkeypatch):
    import routes.chat as chat_routes

    def fake_stream(messages, **kwargs):
        yield "token", "Привет!"
        yield "done", ""

    monkeypatch.setattr(chat_routes, "chat_stream", fake_stream)
    monkeypatch.setattr(chat_routes, "resolve_target",
                        lambda: ("groq", "openai/gpt-oss-120b", {"max_tokens": 1024}))

    response = client.post("/send_stream", json={"message": "Здравствуй", "tools": False})
    assert response.status_code == 200
    events = [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line]
    done = [event for event in events if event.get("done")]
    assert done, events
    receipt = done[-1].get("receipt") or {}
    assert receipt.get("id")

    detail = client.get(f"/api/receipts/{receipt['id']}").get_json()["receipt"]
    assert detail["kind"] == "chat"
    assert detail["output_chars"] > 0
    assert detail["verification"]["ok"] is True


def test_cli_verify_returns_zero(client, capsys):
    _issue()
    assert receipts._cli(["verify"]) == 0
    assert "Цепочка цела" in capsys.readouterr().out
