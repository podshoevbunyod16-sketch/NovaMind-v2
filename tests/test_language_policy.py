"""Промпты английские, ответ — на языке интерфейса (language_policy)."""
import pathlib
import re

import pytest

import config
import language_policy
import local_llm


CYRILLIC = re.compile(r"[А-Яа-яЁё]")


# ─────────────────────────── нормализация ───────────────────────────

def test_normalize_accepts_regions_and_aliases():
    assert language_policy.normalize("ru-RU") == "ru"
    assert language_policy.normalize("en_US") == "en"
    assert language_policy.normalize("tj") == "tg"
    assert language_policy.normalize("Russian") == "ru"
    assert language_policy.normalize("zh-Hans") == "zh"
    assert language_policy.normalize("xx") == ""
    assert language_policy.normalize("") == ""


def test_registry_covers_interface_and_extra_languages():
    codes = {item["code"] for item in language_policy.supported()}
    assert {"tg", "ru", "en", "uz", "de", "zh"} <= codes
    assert set(language_policy.UI_LANGUAGES) == {"tg", "ru", "en"}


# ─────────────────────────── директива ───────────────────────────

def test_directive_is_english_and_names_the_language():
    text = language_policy.directive("ru")
    assert "Russian" in text and "ANSWER LANGUAGE" in text
    instruction = text.split("\n", 1)[1]                    # без строки-заголовка
    assert not CYRILLIC.search(instruction.replace("Русский", ""))
    assert "Russian (Русский)" in text                      # родное имя для модели
    assert language_policy.directive("xx") == ""


def test_apply_to_system_is_idempotent_and_switches_language():
    once = language_policy.apply_to_system("You are helpful.", "ru")
    twice = language_policy.apply_to_system(once, "ru")
    assert once == twice
    assert once.count(language_policy.MARKER) == 1

    switched = language_policy.apply_to_system(once, "en")
    assert switched.count(language_policy.MARKER) == 1
    assert "English" in switched and "Russian" not in switched
    assert switched.startswith("You are helpful.")


def test_apply_to_system_works_without_a_system_prompt():
    text = language_policy.apply_to_system(None, "tg")
    assert "Tajik" in text and text.strip()
    assert language_policy.strip_directive(text) == ""


def test_apply_to_messages_merges_one_system_prompt():
    messages = [{"role": "system", "content": "Rules."},
                {"role": "user", "content": "Hi"}]
    result = language_policy.apply_to_messages(messages, "ru")
    assert result[0]["role"] == "system"
    assert result[0]["content"].count(language_policy.MARKER) == 1
    assert result[1] == {"role": "user", "content": "Hi"}


# ─────────────────────────── выбор языка ───────────────────────────

def test_current_language_follows_config_and_override(monkeypatch):
    monkeypatch.setattr(config, "ui_language", "en")
    assert language_policy.current_language() == "en"
    with language_policy.use_language("ru"):
        assert language_policy.current_language() == "ru"
    assert language_policy.current_language() == "en"


def test_current_language_from_request_header_and_body(client, monkeypatch):
    monkeypatch.setattr(config, "ui_language", "tg")
    assert client.get("/api/settings/lang",
                      headers={"X-UI-Language": "en"}).status_code == 200
    with client.application.test_request_context(
            "/send", headers={"X-UI-Language": "ru"}):
        assert language_policy.current_language() == "ru"
    with client.application.test_request_context(
            "/send", json={"ui_language": "en", "message": "x"}):
        assert language_policy.current_language() == "en"


def test_language_endpoint_stores_answer_language(client, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RUNTIME_SETTINGS_FILE", str(tmp_path / "runtime.json"))
    response = client.post("/api/settings/lang", json={"language": "en"})
    data = response.get_json()
    assert response.status_code == 200
    assert data["language"] == "en" and data["answer_language"] == "English"
    # Сессия запоминает язык: у разных посетителей он может быть разным.
    with client.session_transaction() as stored:
        assert stored.get("ui_language") == "en"
    listing = client.get("/api/settings/lang").get_json()
    assert any(item["code"] == "ru" for item in listing["answer_languages"])


def test_language_endpoint_rejects_unknown_ui_language(client):
    assert client.post("/api/settings/lang", json={"language": "klingon"}).status_code == 400


# ─────────────────────────── промпты английские ───────────────────────────

def test_prompt_files_contain_no_russian_text():
    prompt = pathlib.Path("prompts/base/react_agent.txt").read_text(encoding="utf-8")
    assert not CYRILLIC.search(prompt)
    assert "ANSWER LANGUAGE directive" in prompt


def test_default_system_prompt_is_english():
    default = config.system_prompt if not config.system_prompt else config.system_prompt
    assert not CYRILLIC.search(default)


def test_react_fallback_prompt_is_english():
    import agent.loop as loop
    assert not CYRILLIC.search(loop.SYSTEM)
    assert "ANSWER LANGUAGE" in loop.SYSTEM


# ─────────────────────────── офлайн-ответы ───────────────────────────

@pytest.mark.parametrize("lang,needle", [
    ("en", "there is no language model"),
    ("ru", "языковой модели нет"),
    ("tg", "модели забонӣ нест"),
])
def test_offline_helper_answers_in_interface_language(monkeypatch, lang, needle):
    monkeypatch.setattr(config, "ui_language", lang)
    reply = local_llm.complete([{"role": "user", "content": "how to cook pilaf?"}])
    assert needle in reply.lower()
    assert "GROQ_API_KEY" in reply


def test_offline_helper_falls_back_to_english_for_other_languages(monkeypatch):
    monkeypatch.setattr(config, "ui_language", "de")
    reply = local_llm.complete([{"role": "user", "content": "how to cook pilaf?"}])
    assert "there is no language model" in reply.lower()


def test_offline_greeting_understands_any_language(monkeypatch):
    monkeypatch.setattr(config, "ui_language", "en")
    assert "offline helper" in local_llm.complete(
        [{"role": "user", "content": "привет"}]).lower()
    assert "offline loop" in local_llm.complete(
        [{"role": "user", "content": "who are you?"}]).lower()


def test_offline_markers_cover_all_languages():
    for text in ("Offline mode: there is no language model.",
                 "Офлайн-режим: языковой модели нет.",
                 "Реҷаи офлайнӣ: модели забонӣ нест."):
        assert local_llm.looks_offline(text)
