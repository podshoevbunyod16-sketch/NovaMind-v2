"""Тесты «Совета моделей»: согласие, расхождения, API, лимиты, квитанция."""
import json

import council
import receipts


def _seats(monkeypatch, answers):
    """Подменяет места совета и ответы моделей без сети."""
    monkeypatch.setattr(council, "_candidates",
                        lambda limit=3: [("test-provider", "model-a"),
                                         ("test-provider", "model-b"),
                                         ("test-provider", "model-c")][:limit])
    calls = {"count": 0}

    def fake_completion(messages, provider=None, model=None, system=None, **kwargs):
        index = calls["count"] % len(answers)
        calls["count"] += 1
        return answers[index], {"model": model or "model-a"}

    import ai_providers
    monkeypatch.setattr(ai_providers, "chat_completion", fake_completion)
    return calls


def test_aligned_council_finds_consensus(client, monkeypatch):
    same = ("Утром лучше запускать тесты после установки зависимостей. "
            "Проверьте версию Python перед запуском. "
            "Сохраните лог, если тест упал.")
    _seats(monkeypatch, [same, same, same])

    result = council.ask("Как запустить тесты?", limit=3)
    assert result["agreement"] >= 70
    assert result["verdict"] == "aligned"
    assert result["consensus"]
    assert len(result["seats"]) == 3
    assert all(seat["status"] == "ok" for seat in result["seats"])
    assert result["receipt"]["id"]


def test_divided_council_reports_disagreement(client, monkeypatch):
    _seats(monkeypatch, [
        "Нужно переписать модуль на асинхронный движок и добавить очередь задач.",
        "Лучше оставить синхронный код, добавить кэш и не трогать архитектуру.",
        "Проблема в правах доступа к папке, проверьте владельца файлов и umask.",
    ])
    result = council.ask("Что делать с медленным сервисом?", limit=3)
    assert result["agreement"] < 70
    assert result["verdict"] in {"partial", "divided"}
    assert result["disagreements"]
    assert result["recommendation"]


def test_seat_errors_do_not_break_council(client, monkeypatch):
    monkeypatch.setattr(council, "_candidates",
                        lambda limit=3: [("test-provider", "model-a"),
                                         ("test-provider", "model-b")])
    import ai_providers

    def flaky(messages, provider=None, model=None, system=None, **kwargs):
        if "CRITIC" in str(system):
            return None, {"error": "HTTP 429"}
        return "Ответ практика: сделайте шаг один, затем шаг два.", {"model": model}

    monkeypatch.setattr(ai_providers, "chat_completion", flaky)
    result = council.ask("Что делать?", limit=2)
    statuses = {seat["status"] for seat in result["seats"]}
    assert "ok" in statuses and "error" in statuses
    assert result["agreement"] is not None


def test_no_models_raises(client, monkeypatch):
    monkeypatch.setattr(council, "_candidates", lambda limit=3: [])
    try:
        council.ask("Вопрос")
    except council.NoModels:
        pass
    else:  # pragma: no cover
        raise AssertionError("ожидали NoModels")


def test_route_requires_question(client):
    response = client.post("/api/council/ask", json={})
    assert response.status_code == 400


def test_route_returns_503_without_models(client, monkeypatch):
    monkeypatch.setattr(council, "_candidates", lambda limit=3: [])
    response = client.post("/api/council/ask", json={"question": "Есть ли жизнь?"})
    assert response.status_code == 503
    data = response.get_json()
    assert data["ok"] is False
    assert "needs" in data


def test_route_success_and_history(client, monkeypatch):
    same = "Сначала проверьте логи. Потом перезапустите сервис. Затем измерьте время ответа."
    _seats(monkeypatch, [same, same, same])

    response = client.post("/api/council/ask", json={"question": "Сервис лежит, что делать?"})
    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True
    assert data["agreement"] >= 70
    assert data["receipt"]["id"]

    stored = receipts.get(data["receipt"]["id"])
    assert stored["kind"] == "council"
    assert stored["meta"]["verdict"] == "aligned"

    history = client.get("/api/council/history").get_json()
    assert history["items"]
    assert history["items"][0]["receipt_id"] == data["receipt"]["id"]


def test_route_rate_limit(client, monkeypatch):
    same = "Один ответ на всех. Второй тезис для совета."
    _seats(monkeypatch, [same, same, same])
    monkeypatch.setenv("COUNCIL_RATE_GUEST", "1")
    monkeypatch.setenv("COUNCIL_RATE_LIMIT", "1")

    first = client.post("/api/council/ask", json={"question": "Первый вопрос"})
    assert first.status_code == 200
    second = client.post("/api/council/ask", json={"question": "Второй вопрос"})
    assert second.status_code == 429
    assert second.headers.get("Retry-After")


def test_agreement_metric_is_bounded(client, monkeypatch):
    _seats(monkeypatch, ["Совсем другой ответ про базы данных и индексы.",
                         "Ответ про кулинарию и рецепты супа.",
                         "Третий вариант про космос и орбиты планет."])
    result = council.ask("Что-нибудь", limit=3)
    assert 0 <= result["agreement"] <= 100
    assert isinstance(result["similarity"], list)
