"""Тесты страницы доверия /trust и её API-контрактов."""
import receipts


def test_trust_page_renders(client):
    response = client.get("/trust")
    assert response.status_code == 200
    assert "text/html" in response.mimetype
    body = response.get_data(as_text=True)
    assert "Trust Center" in body
    assert "Nova Receipts" in body
    assert "/static/signature.js" in body
    assert "/static/trust.js" in body


def test_trust_static_assets_exist(client):
    for path in ("/static/signature.css", "/static/signature.js", "/static/trust.css",
                 "/static/trust.js"):
        assert client.get(path).status_code == 200, path


def test_trust_page_shows_own_receipts(client):
    client.get("/trust")           # первый запрос закрепляет browser id
    with client.session_transaction() as session:
        owner = "anon:" + str(session.get("nova_sid") or "")
    receipts.issue(kind="chat", owner=owner, chat_id="", provider="groq",
                   model="test", prompt="вопрос", output="ответ")
    data = client.get("/api/receipts").get_json()
    assert data["stats"]["total"] == 1
    assert data["items"][0]["model"] == "test"


def test_trust_page_available_without_login(client):
    assert client.get("/trust").status_code == 200
    assert client.get("/api/council/history").status_code == 200
