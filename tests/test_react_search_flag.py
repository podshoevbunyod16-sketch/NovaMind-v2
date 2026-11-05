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


def test_react_route_is_bound_to_start_react_run():
    """Декоратор маршрута не должен «прилипать» к вспомогательной функции."""
    import ast
    import inspect
    import routes.react_agent as module
    tree = ast.parse(inspect.getsource(module))
    bound = {node.name: [ast.unparse(d) for d in node.decorator_list]
             for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert any("/api/agent/react" in d for d in bound["start_react_run"])
    assert bound["_search_flag"] == []
