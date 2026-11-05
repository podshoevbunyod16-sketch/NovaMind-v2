"""Кнопка «Автопоиск» должна включать принудительный веб-поиск react-агента."""
from routes.react_agent import _search_flag


def test_frontend_sends_search_key():
    assert _search_flag({"search": True, "tools": True}) is True


def test_legacy_auto_search_key():
    assert _search_flag({"auto_search": "1"}) is True


def test_search_off_or_missing():
    assert _search_flag({"search": False, "tools": False}) is False
    assert _search_flag({"message": "hi"}) is False
    assert _search_flag(None) is False
