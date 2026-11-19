"""Regression tests for server-identity scoped knowledge_search."""

import identity
import knowledge
import tool_registry


def _knowledge_tool(monkeypatch, kb, owner):
    monkeypatch.setattr(knowledge, "default_base", lambda: kb)
    monkeypatch.setattr(identity, "chat_owner", lambda: owner)
    tool_registry.bootstrap_default_tools()
    return tool_registry.get_tool("knowledge_search")


def test_knowledge_search_owner_scope_is_taken_from_server_identity(tmp_path, monkeypatch):
    kb = knowledge.KnowledgeBase(str(tmp_path / "knowledge.db"))
    kb.ingest_text("Alice private", "alpha-only document", owner="email:alice@example.com")
    kb.ingest_text("Bob private", "beta-only document", owner="email:bob@example.com")
    tool = _knowledge_tool(monkeypatch, kb, "email:alice@example.com")

    result = tool.execute(query="document", owner="email:bob@example.com")

    assert result["ok"] is True
    assert {item["title"] for item in result["results"]} == {"Alice private"}


def test_knowledge_search_second_user_cannot_see_first_users_documents(tmp_path, monkeypatch):
    kb = knowledge.KnowledgeBase(str(tmp_path / "knowledge.db"))
    kb.ingest_text("Alice private", "alpha-only document", owner="email:alice@example.com")
    tool = _knowledge_tool(monkeypatch, kb, "email:bob@example.com")

    result = tool.execute(query="document")

    assert result["ok"] is True
    assert result["results"] == []


def test_knowledge_search_without_identity_fails_closed(tmp_path, monkeypatch):
    kb = knowledge.KnowledgeBase(str(tmp_path / "knowledge.db"))
    kb.ingest_text("Alice private", "alpha-only document", owner="email:alice@example.com")
    tool = _knowledge_tool(monkeypatch, kb, "")

    result = tool.execute(query="document", owner="email:alice@example.com")

    assert result["ok"] is False
    assert result["error"]["type"] == "permission_denied"


def test_admin_identity_keeps_explicit_all_owner_scope(tmp_path, monkeypatch):
    kb = knowledge.KnowledgeBase(str(tmp_path / "knowledge.db"))
    kb.ingest_text("Alice private", "alpha-only document", owner="email:alice@example.com")
    kb.ingest_text("Bob private", "beta-only document", owner="email:bob@example.com")
    tool = _knowledge_tool(monkeypatch, kb, "admin:root")

    result = tool.execute(query="document")

    assert result["ok"] is True
    assert {item["title"] for item in result["results"]} == {"Alice private", "Bob private"}
