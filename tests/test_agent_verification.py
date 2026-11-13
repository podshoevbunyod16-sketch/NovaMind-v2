from agent.loop import AgentContext, run_react


def test_action_claim_requires_tool_evidence(monkeypatch):
    import config
    monkeypatch.setattr(config, "ui_language", "ru")
    result = run_react(AgentContext(
        "Создай файл отчёта", complete=lambda *a, **k: ("Готово, файл отчёта создан.", {}),
        tools=[],
    ))
    assert "Не могу честно подтвердить завершение" in result.answer


def test_action_claim_uses_the_interface_language(monkeypatch):
    import config

    monkeypatch.setattr(config, "ui_language", "en")
    result = run_react(AgentContext(
        "Create the report file", complete=lambda *a, **k: ("Done, the report is created.", {}),
        tools=[],
    ))
    assert "cannot honestly confirm" in result.answer


def test_non_action_answer_does_not_require_tools():
    result = run_react(AgentContext(
        "Что такое SQLite?", complete=lambda *a, **k: ("SQLite — встроенная база данных.", {}),
        tools=[],
    ))
    assert result.answer == "SQLite — встроенная база данных."
