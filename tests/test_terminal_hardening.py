"""Additional defence-in-depth checks for the command sandbox."""
import logging
import os

import pytest

import routes.terminal as terminal


def test_sandbox_env_never_inherits_provider_secrets(monkeypatch, tmp_path, sandbox):
    monkeypatch.setenv("GROQ_API_KEY", "sentinel-groq-key")
    monkeypatch.setenv("GEMINI_API_KEY", "sentinel-gemini-key")
    monkeypatch.setenv("SESSION_SECRET", "sentinel-session-secret")
    env = terminal._sandbox_env()
    for value in ("sentinel-groq-key", "sentinel-gemini-key", "sentinel-session-secret"):
        assert value not in env.values()
        assert value not in str(env)
    assert env["HOME"] == terminal._ws()
    assert os.path.isabs(env["PATH"]) is False or ":" in env["PATH"]


def test_sandbox_env_keeps_home_inside_workspace(sandbox):
    result = terminal.run_command("pwd")
    assert result["code"] == 0
    assert terminal._ws() in result["stdout"]


def test_terminal_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("TERMINAL_ENABLED", raising=False)
    assert terminal.is_enabled() is False


@pytest.mark.skipif(os.name != "posix", reason="resource limits are POSIX-only")
def test_resource_limits_are_applied_to_child(monkeypatch):
    import resource
    import subprocess
    import sys

    monkeypatch.setenv("TERMINAL_CPU_SECONDS", "7")
    monkeypatch.setenv("TERMINAL_MEMORY_MB", "256")
    preexec = terminal._resource_limits()
    assert callable(preexec)
    script = (
        "import resource,sys;"
        "sys.stdout.write('%d %d' % (resource.getrlimit(resource.RLIMIT_CPU)[0],"
        "resource.getrlimit(resource.RLIMIT_AS)[0]))"
    )
    output = subprocess.check_output(
        [sys.executable, "-c", script], preexec_fn=preexec, text=True)
    cpu, memory = output.split()
    assert int(cpu) == 7
    assert int(memory) == 256 * 1024 * 1024


def test_every_command_is_audited_without_output(caplog, sandbox):
    with caplog.at_level(logging.INFO, logger="novamind.audit"):
        result = terminal.run_command("echo hello-audit")
    assert result["code"] == 0
    lines = [record.getMessage() for record in caplog.records
             if record.name == "novamind.audit"]
    assert any("event=terminal_command" in line and "echo hello-audit" in line
               for line in lines)
    assert all("hello-audit\n" not in line for line in lines)


def test_agent_rejects_forbidden_command_and_path(sandbox):
    for command in ("sudo rm -rf /tmp", "cat ../../etc/passwd", "echo x && echo y"):
        result = terminal.run_agent(command)
        assert result["code"] != 0
        assert result["stdout"] == ""


def test_write_size_limit_in_file_api(client, sandbox, tmp_path):
    """Explicit file API rejects oversized writes before touching the disk."""
    with client.session_transaction() as sess:
        sess["admin_logged_in"] = True
    response = client.post("/api/terminal/file", json={
        "action": "write", "path": "big.txt", "content": "x" * 200_001,
    })
    assert response.status_code == 400
