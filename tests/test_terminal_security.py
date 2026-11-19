"""Безопасность терминала (этап 6).

Проверяем ровно то, чего нельзя допускать: выход из рабочей папки,
запрещённые программы, инъекции через «;», «&&», «$()» и подмену путей
симлинками. Конвейеры «|» разрешены, но каждая часть проверяется отдельно,
и shell=True нигде не используется.
"""
import os

import pytest

import routes.terminal as term_routes
from agent import policy


@pytest.fixture(autouse=True)
def _legacy_host_mode_for_existing_process_tests(monkeypatch):
    """Opt in only for legacy subprocess behavior covered by this file."""
    monkeypatch.setenv("TERMINAL_HOST_EXECUTION", "1")
    monkeypatch.delenv("AGENT_CONFIRM", raising=False)
    policy.reset_cache()
    policy.reset_confirmations()
    yield
    policy.reset_confirmations()
    policy.reset_cache()


def _run(admin_client, command):
    return admin_client.post("/api/terminal/run", json={"command": command}).get_json()


# ══════════════ конвейеры разрешены ══════════════

def test_safe_pipeline_works(admin_client, sandbox):
    (sandbox / "data.txt").write_text("яблоко\nбанан\nяблоко\n", encoding="utf-8")

    result = _run(admin_client, "cat data.txt | grep яблоко | wc -l")
    assert result["code"] == 0
    assert result["stdout"].strip() == "2"

    head = _run(admin_client, "cat data.txt | head -1")
    assert head["stdout"].strip() == "яблоко"


def test_pipeline_validates_every_segment(admin_client, sandbox):
    (sandbox / "data.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(ValueError):
        term_routes.check_command("cat data.txt | bash")
    result = _run(admin_client, "cat data.txt | bash")
    assert (result.get("code") == 2
            or "не в списке разрешённых" in result.get("error", result.get("stderr", "")))


def test_terminal_never_uses_shell_true():
    """Гарантия: команды запускаются списком аргументов, без shell."""
    source = open(term_routes.__file__, encoding="utf-8").read()
    assert "shell=True" not in source
    assert "preexec_fn=" not in source


def test_terminal_applies_resource_limits_in_wrapper(admin_client, sandbox):
    result = _run(
        admin_client,
        "python3 -c 'import resource; print(resource.getrlimit(resource.RLIMIT_AS)[0])'",
    )
    assert result["code"] == 0
    assert result["stdout"].strip() == str(512 * 1024 * 1024)


# ══════════════ запрещённые конструкции ══════════════

@pytest.mark.parametrize("command", [
    "ls && rm -rf /",
    "ls || ls",
    "echo $(whoami)",
    "echo ${HOME}",
    "echo `id`",
    "ls; ls",
    "sudo ls",
    "cat /etc/passwd",
    "ls ../../",
    "cat ../secret.txt",
])
def test_forbidden_commands_are_rejected(admin_client, sandbox, command):
    """Парсер команд — последняя линия обороны: такой ввод не проходит."""
    with pytest.raises(ValueError):
        term_routes.check_command(command)

    # и через HTTP: либо 400, либо структурированный отказ
    response = admin_client.post("/api/terminal/run", json={"command": command})
    if response.status_code == 200:
        assert response.get_json()["code"] == 2
    else:
        assert response.status_code == 400 and response.get_json()["error"]


def test_semicolon_inside_quotes_is_allowed(admin_client, sandbox):
    result = _run(admin_client, "python3 -c 'x = 1; print(x + 1)'")
    assert result["code"] == 0 and result["stdout"].strip() == "2"


def test_absolute_paths_are_rejected_for_files_tools(admin_client, sandbox):
    with pytest.raises(ValueError):
        term_routes.safe_path("/etc/passwd")
    with pytest.raises(ValueError):
        term_routes.safe_path("../outside")
    assert term_routes.safe_path("notes/file.md").startswith(str(sandbox))


def test_symlink_escape_is_blocked(admin_client, sandbox, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("секрет", encoding="utf-8")
    link = sandbox / "link.txt"
    os.symlink(outside, link)

    # Симлинк, ведущий наружу, не проходит и через shell
    with pytest.raises(ValueError):
        term_routes.check_command("cat link.txt")

    import tool_registry
    tool_registry.bootstrap_default_tools()
    result = _run(admin_client, "cat link.txt")
    assert result.get("code") != 0 or "секрет" not in result.get("stdout", "")


def test_redirect_outside_workspace_is_blocked(admin_client, sandbox):
    result = _run(admin_client, "echo привет > ../outside.txt")
    assert result.get("code") == 2 or result.get("error")
    assert not os.path.exists(sandbox.parent / "outside.txt")


def test_redirect_inside_workspace_works(admin_client, sandbox):
    result = _run(admin_client, "echo отчёт > note.txt")
    assert result["code"] == 0
    assert (sandbox / "note.txt").read_text(encoding="utf-8").strip() == "отчёт"


def test_sandbox_python_is_the_server_interpreter(admin_client, sandbox):
    """`python3` в песочнице = тот же интерпретатор, что у сервера.

    Иначе пакет, поставленный агентом, не виден приложению: на телефоне это
    совпадает, а в venv — нет (ModuleNotFoundError при верном коде).
    """
    import sys
    result = _run(admin_client, "python3 -c 'import sys; print(sys.executable)'")
    assert result["code"] == 0
    assert result["stdout"].strip() == sys.executable


def test_server_interpreter_can_be_disabled(admin_client, sandbox, monkeypatch):
    monkeypatch.setenv("TERMINAL_SERVER_PYTHON", "0")
    import routes.terminal as term
    assert term._with_server_interpreter(["python3", "-c", "1"]) == ["python3", "-c", "1"]
    assert term._with_server_interpreter(["pip", "install", "x"]) == ["pip", "install", "x"]


def test_host_fallback_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("TERMINAL_HOST_EXECUTION", raising=False)
    with pytest.raises(ValueError, match="host fallback отключён"):
        term_routes.check_command("mkdir generated")


@pytest.mark.parametrize("command", ["curl https://example.test", "ssh example.test", "wget https://example.test"])
def test_network_commands_are_never_available_as_host_fallback(command, monkeypatch):
    monkeypatch.setenv("TERMINAL_HOST_EXECUTION", "1")
    with pytest.raises(ValueError, match="Сетевые команды отключены"):
        term_routes.check_command(command)


def test_direct_terminal_route_obeys_confirmation_policy(admin_client, sandbox, monkeypatch):
    monkeypatch.setenv("AGENT_CONFIRM", "danger")
    policy.reset_cache()
    response = admin_client.post("/api/terminal/run", json={"command": "rm -rf build"})
    assert response.status_code == 403
    body = response.get_json()
    assert body["confirmation_required"] is True
    assert not (sandbox / "build").exists()


def test_direct_terminal_route_allows_safe_read_only_command(admin_client, sandbox):
    (sandbox / "readme.txt").write_text("ok", encoding="utf-8")
    response = admin_client.post("/api/terminal/run", json={"command": "cat readme.txt"})
    assert response.status_code == 200
    assert response.get_json()["stdout"].strip() == "ok"


def test_agent_timeout_is_clamped_to_hard_ceiling(monkeypatch):
    monkeypatch.setenv("TERMINAL_HOST_EXECUTION", "1")
    assert term_routes._agent_timeout("sleep 1", 999999) == term_routes.MAX_COMMAND_TIMEOUT
    assert term_routes._agent_timeout("python3 -m venv venv", 999999) == term_routes.MAX_SLOW_COMMAND_TIMEOUT
