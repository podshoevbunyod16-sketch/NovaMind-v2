"""Резервный ответ из результатов поиска, когда языковая модель недоступна,
и целостность потоковой выдачи поиска."""
import routes.search as search


SOURCES = [
    {"title": "Example One", "url": "https://example.com/one", "host": "example.com",
     "snippet": "Первый фрагмент про тему вопроса."},
    {"title": "Example Two", "url": "https://example.org/two", "host": "example.org",
     "snippet": "Второй фрагмент."},
]


def test_fallback_answer_lists_sources_and_marks_limitation():
    answer = search._fallback_answer("тема вопроса", SOURCES, "все провайдеры недоступны")
    assert "Модель не ответила" in answer
    assert "все провайдеры недоступны" in answer
    assert "https://example.com/one" in answer
    assert "Первый фрагмент" in answer
    assert "https://example.org/two" in answer


def test_fallback_answer_without_sources():
    answer = search._fallback_answer("вопрос", [], "timeout")
    assert "Модель не ответила" in answer and "timeout" in answer


def test_answer_with_context_falls_back_to_sources_when_llm_down(monkeypatch):
    monkeypatch.setattr(search, "chat_stream",
                        lambda *a, **k: iter([("error", "все провайдеры недоступны"), ("done", "")]))
    monkeypatch.setattr(search, "chat_completion",
                        lambda *a, **k: (None, "все бесплатные AI-провайдеры недоступны"))
    gen = search._answer_with_context("тема", "контекст", SOURCES, "groq", "m")
    pumped = list(search._pump(gen, lambda **kw: kw))
    # результат генератора — последнее «return»: ищем через send
    gen2 = search._answer_with_context("тема", "контекст", SOURCES, "groq", "m")
    try:
        while True:
            next(gen2)
    except StopIteration as stop:
        answer, note = stop.value
    assert "Модель не ответила" in answer
    assert "https://example.com/one" in answer  # результаты поиска не потеряны
    assert note


def test_search_pipeline_emits_result_and_done(monkeypatch):
    monkeypatch.setattr(search, "build_search_query", lambda q, history=None: q)

    def fake_parallel(q, emit):
        if False:
            yield
        return SOURCES, []

    monkeypatch.setattr(search, "_parallel_search", fake_parallel)
    monkeypatch.setattr(search, "_read_pages", lambda sources: [])

    def fake_answer(*a, **k):
        if False:
            yield
        return "ответ", None

    monkeypatch.setattr(search, "_answer_with_context", fake_answer)
    events = list(search.search_pipeline("вопрос", force_search=True))
    types = [e.get("type") for e in events]
    assert "sources" in types
    assert types[-1] == "done"
    result = next(e for e in events if e.get("type") == "result")
    assert result["searched"] is True
    assert result["sources"] == SOURCES
