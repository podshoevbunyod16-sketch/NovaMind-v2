"""Регрессия: фоновый поток ReAct не должен перечитывать сокет запроса.

Главный поток уже прочитал тело (request.get_json()), поэтому тело в
environ, который уходит в фоновый поток, пришлось положить заново.
Иначе request.get_json()/form/data внутри воркера ждёт CONTENT_LENGTH
байтов из исчерпанного LimitedStream и зависает навсегда: чат показывал
«найдено N сайтов» и на этом замерзал, а без автопоиска не отвечал совсем.
"""
import io
import json

import flask


def test_worker_request_context_carries_fresh_body(client, monkeypatch):
    captured = []
    real_request_context = flask.Flask.request_context

    def recording_request_context(self, environ, *args, **kwargs):
        env = dict(environ)
        captured.append(env)
        # Воркеру отдаём свежую копию тела, а снимок оставляем неизменным
        # для проверок ниже (воркер сам прочитает свой экземпляр).
        env["wsgi.input"] = io.BytesIO(env["wsgi.input"].getvalue())
        return real_request_context(self, env, *args, **kwargs)

    monkeypatch.setattr(flask.Flask, "request_context", recording_request_context)
    monkeypatch.setattr("ai_providers.chat_completion",
                        lambda *a, **k: ("Ответ модели.", {"model": "mock"}))

    payload = {"message": "привет", "tools": False, "search": False,
               "ui_language": "ru"}
    response = client.post("/api/agent/react", json=payload)
    body_text = response.get_data(as_text=True)   # поток должен дочитаться, а не зависнуть

    assert response.status_code == 200
    assert captured, "воркер должен пересоздавать контекст запроса"
    worker_env = captured[0]
    worker_body = worker_env["wsgi.input"].getvalue()
    assert json.loads(worker_body.decode("utf-8")) == payload
    assert worker_env["CONTENT_LENGTH"] == str(len(worker_body))
    events = [json.loads(line) for line in body_text.splitlines() if line.strip()]
    assert any(event.get("type") == "result" for event in events)
    assert any(event.get("reply") == "Ответ модели." for event in events)


def test_worker_language_directive_reads_body_without_blocking(client, monkeypatch):
    """Язык ответа из JSON-тела доходит до воркера (раньше тут был дедлок)."""
    monkeypatch.setattr("ai_providers.chat_completion",
                        lambda *a, **k: ("Ответ модели.", {"model": "mock"}))
    payload = {"message": "привет", "tools": False, "ui_language": "fr"}
    response = client.post("/api/agent/react", json=payload)
    events = [json.loads(line) for line in
              response.get_data(as_text=True).splitlines() if line.strip()]
    assert any(event.get("type") == "result" for event in events)
