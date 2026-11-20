"""Focused, intent-aware search over permitted built-in and MCP tools.

Instead of sending every built-in tool and every connected MCP schema to the
model on every turn, the agent starts with only ``tool_search`` and
``use_tool``. ``tool_search`` returns only the small, relevant subset of tools
needed for the requested capability, filtering out unrelated third-party tools.
"""
from __future__ import annotations

import re
from typing import Any

# Aliases that duplicate canonical tools in tool_registry and should not clutter
# search results. (Direct execution still supports them.)
_ALIAS_TOOLS = frozenset({
    "web_fetch", "file_list", "bash", "python", "image_generate",
    "mcp_call", "tool_search", "use_tool",
})

# Generic action verbs and filler words that must NOT match across unrelated
# domains on their own (e.g. "search" / "поиск" alone should not pull in
# file_search or knowledge_search when the user wants web search).
_GENERIC_WORDS = frozenset({
    "a", "an", "the", "and", "or", "to", "for", "of", "in", "on", "at", "by",
    "with", "from", "my", "our", "this", "that", "is", "are", "how", "what",
    "which", "who", "when", "where", "why", "can", "could", "please", "need",
    "want", "find", "search", "open", "get", "read", "list", "show", "view",
    "look", "check", "inspect", "browse", "use", "run", "call", "tool", "tools",
    "server", "servers", "info", "information", "data", "page", "help",
    "и", "или", "в", "во", "на", "по", "для", "от", "до", "из", "с", "со", "к",
    "о", "об", "про", "как", "что", "где", "кто", "когда", "почему", "какой",
    "какие", "топ", "мой", "моя", "моё", "мои", "наш", "это", "этот", "нужен",
    "нужна", "нужно", "нужны", "пожалуйста", "найти", "найди", "найд", "поиск",
    "поищи", "поищу", "открыть", "открой", "посмотреть", "посмотри", "показать",
    "покажи", "прочитать", "прочитай", "узнать", "сделать", "выполнить",
    "использовать", "используй", "инструмент", "инструменты", "инструментов",
    "сервер", "серверы", "серверов", "информация", "информацию", "данные",
    "вопрос", "ответ", "программа",
})

_WEB_MARKERS = (
    "web", "internet", "online", "browser", "webpage", "website", "url", "http",
    "https", "link", "article", "news", "google", "duckduckgo", "wiki",
    "wikipedia", "weather", "forecast", "price", "ranking", "review",
    "интернет", "веб", "онлайн", "браузер", "сайт", "страниц", "ссылк",
    "стать", "новост", "погод", "курс", "рейтинг", "обзор", "топ ",
    "лучш", "актуальн", "в сети", "интернет-поиск", "веб-поиск",
)

_REPO_MARKERS = (
    "repo", "repos", "repository", "repositories", "github", "gitlab",
    "bitbucket", "branch", "branches", "commit", "commits", "pull request",
    "issue", "issues", "codebase", "репозитор", "репо", "гитхаб", "гитлаб",
    "ветк", "коммит", "пулл", "ишью", "релиз",
)

_MCP_MARKERS = (
    "mcp", "мкп", "интеграц", "подключен", "внешн",
)

_FILE_MARKERS = (
    "file", "files", "folder", "folders", "directory", "directories", "dir",
    "path", "filename", "grep", "tree", "patch", "файл", "папк", "директор",
    "каталог", "дерев", "путь", "содержим", "греп", "строк",
)

_LOCAL_MARKERS = (
    "local", "workspace", "sandbox", "disk", "локальн", "рабоч", "песочниц",
    "диск",
)

_TERMINAL_MARKERS = (
    "terminal", "shell", "bash", "console", "command", "cmd", "execute",
    "script", "python", "pytest", "pip", "npm", "node", "linux",
    "терминал", "консол", "команд", "баш", "выполн", "запуст", "скрипт",
    "тест", "установ",
)

_IMAGE_MARKERS = (
    "image", "picture", "photo", "draw", "paint", "sketch", "illustrat",
    "poster", "картинк", "изображени", "фото", "нарису", "рисун",
    "иллюстрац", "постер",
)

_AUDIO_MARKERS = (
    "audio", "voice", "tts", "speech", "speak", "sound", "mp3",
    "аудио", "голос", "озвуч", "речь", "звук",
)

_VIDEO_MARKERS = (
    "video", "clip", "movie", "animation", "видео", "ролик", "клип", "анимац",
)

_MEMORY_MARKERS = (
    "memory", "remember", "recall", "forget", "памят", "запомн", "вспомн", "забуд",
)

_KNOWLEDGE_MARKERS = (
    "knowledge", "rag", "база знаний", "базе знаний", "базу знаний", "документ",
)

_TASK_MARKERS = (
    "task", "todo", "checklist", "задач", "подпункт",
)

_SCHEDULE_MARKERS = (
    "schedule", "cron", "timer", "reminder", "расписан", "напомн", "крон", "таймер",
)

_SKILL_MARKERS = (
    "skill", "skill.md", "навык", "скилл",
)

_ASK_USER_MARKERS = (
    "ask user", "clarify", "уточн", "спрос", "вопрос пользовател",
)

_READ_ACTION_HINTS = (
    "find", "search", "open", "view", "show", "read", "get", "list", "look",
    "inspect", "browse", "check", "fetch", "contents", "tree", "info", "status",
    "найд", "поиск", "поищ", "откро", "откры", "посмотр", "покаж", "прочит",
    "список", "получит", "провер", "содержим",
)

_WRITE_ACTION_HINTS = (
    "create", "new", "add", "edit", "update", "modify", "write", "patch",
    "delete", "remove", "drop", "destroy", "merge", "close", "push", "fork",
    "commit", "upload", "save", "созда", "нов", "добав", "измен", "обнов",
    "запи", "сохран", "удал", "мердж", "закр", "пуш", "отредакт", "правк",
)

_DELETE_ACTION_HINTS = (
    "delete", "remove", "drop", "destroy", "purge", "удал", "сотр", "уничтож",
)
_DELETE_TOOL_PREFIXES = ("delete_", "remove_", "drop_", "destroy_", "purge_")

_MCP_WRITE_PREFIXES = (
    "create_", "new_", "add_", "edit_", "update_", "modify_", "write_",
    "delete_", "remove_", "drop_", "destroy_", "merge_", "close_", "push_",
    "fork_", "star_", "unstar_", "assign_", "post_", "send_", "upload_",
    "create", "delete", "remove", "update", "merge", "push", "fork",
)

_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)


def _split_identifiers(text: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(text or ""))
    spaced = re.sub(r"[_/.-]+", " ", spaced)
    return re.findall(r"[a-zа-яёҳқҷғӣӯ0-9]+", spaced.lower())


def _has_any(text: str, markers: tuple[str, ...]) -> bool:
    low = str(text or "").lower()
    return any(marker in low for marker in markers)


def _detect_intents(query: str, goal: str = "") -> dict[str, bool]:
    q = f" {str(query or '').lower()} "
    g = f" {str(goal or '').lower()} "

    intents = {
        "web": _has_any(q, _WEB_MARKERS) or bool(_URL_RE.search(q)),
        "repo": _has_any(q, _REPO_MARKERS),
        "mcp": _has_any(q, _MCP_MARKERS),
        "files": _has_any(q, _FILE_MARKERS),
        "local": _has_any(q, _LOCAL_MARKERS),
        "terminal": _has_any(q, _TERMINAL_MARKERS),
        "image": _has_any(q, _IMAGE_MARKERS),
        "audio": _has_any(q, _AUDIO_MARKERS),
        "video": _has_any(q, _VIDEO_MARKERS),
        "memory": _has_any(q, _MEMORY_MARKERS),
        "knowledge": _has_any(q, _KNOWLEDGE_MARKERS),
        "task": _has_any(q, _TASK_MARKERS),
        "schedule": _has_any(q, _SCHEDULE_MARKERS),
        "skill": _has_any(q, _SKILL_MARKERS),
        "ask_user": _has_any(q, _ASK_USER_MARKERS),
        "read_action": _has_any(q, _READ_ACTION_HINTS),
        "write_action": _has_any(q, _WRITE_ACTION_HINTS),
        "delete_action": _has_any(q, _DELETE_ACTION_HINTS),
    }

    # If the search query itself is vague (e.g. "find tools" or a topic like
    # "top ai models"), check if the original goal has a URL or clear domain,
    # otherwise leave explicit domains as detected from query.
    if not any(intents[k] for k in ("web", "repo", "mcp", "files", "terminal",
                                    "image", "audio", "video", "memory",
                                    "knowledge", "task", "schedule", "skill", "ask_user")):
        if _URL_RE.search(g):
            intents["web"] = True
        elif _has_any(g, _REPO_MARKERS):
            intents["repo"] = True
        elif _has_any(g, _IMAGE_MARKERS):
            intents["image"] = True
        elif _has_any(g, _AUDIO_MARKERS):
            intents["audio"] = True
        elif _has_any(g, _VIDEO_MARKERS):
            intents["video"] = True
        elif _has_any(g, _TERMINAL_MARKERS):
            intents["terminal"] = True
        elif _has_any(g, _FILE_MARKERS):
            intents["files"] = True

    return intents


def _distinctive_tokens(query: str) -> list[str]:
    tokens = _split_identifiers(query)
    return [tok for tok in tokens if len(tok) >= 3 and tok not in _GENERIC_WORDS]


def _score_builtin(name: str, spec: Any, intents: dict[str, bool],
                   distinctive: list[str], has_mcp_repo: bool,
                   has_mcp_tools: bool, query_text: str) -> float:
    score = 0.0

    # Direct mention of tool name
    if name in distinctive or any(tok in name for tok in distinctive if len(tok) >= 4):
        score += 10.0

    if name == "mcp_list":
        if intents["mcp"] and not has_mcp_tools:
            return 8.0
        return 0.0

    if name in {"web_search", "web_open"}:
        if intents["web"]:
            score += 10.0
            if name == "web_open" and ("open" in distinctive or "url" in distinctive or "http" in distinctive):
                score += 1.0
            if name == "web_search" and ("search" in distinctive or not intents["repo"]):
                score += 1.0
        return score

    if name == "knowledge_search":
        if intents["knowledge"]:
            score += 11.0
        return score

    remote_repo_hint = _has_any(query_text, ("github", "gitlab", "bitbucket", "mcp", "мкп", "гитхаб", "гитлаб"))

    if name in {"file_read", "file_write", "file_edit", "file_search", "file_tree"}:
        # Do not leak local file tools into remote GitHub/MCP repo queries
        # unless local/workspace was asked or no MCP repo tools exist.
        if (intents["repo"] and has_mcp_repo and not intents["local"]
                and (remote_repo_hint or not intents["files"])):
            return 0.0
        if intents["files"] or (intents["repo"] and not has_mcp_repo):
            score += 6.0
            if name == "file_read" and (intents["read_action"] or not intents["write_action"]):
                score += 4.0
            if name == "file_tree" and any(w in distinctive for w in ("tree", "structure", "folder", "dir", "directory")):
                score += 4.0
            if name == "file_tree" and intents["repo"] and not has_mcp_repo:
                score += 3.0
            if name == "file_search" and any(w in distinctive for w in ("grep", "regex", "content")):
                score += 5.0
            if name == "file_edit" and intents["write_action"]:
                score += 4.5
            if name == "file_write" and any(w in distinctive for w in ("create", "write", "new")):
                score += 4.5
            # If user clearly asked to read/find only, suppress write/edit tools.
            if intents["read_action"] and not intents["write_action"] and name in {"file_write", "file_edit"}:
                score = 0.0
        return score

    if name == "shell":
        if intents["terminal"]:
            score += 11.0
        return score

    if name == "git":
        if intents["terminal"] and any("git" in tok for tok in distinctive):
            score += 11.0
        elif intents["repo"] and not remote_repo_hint and (intents["local"] or not has_mcp_repo):
            score += 9.0
        return score

    if name == "image":
        if intents["image"]:
            score += 12.0
        return score

    if name == "audio":
        if intents["audio"]:
            score += 12.0
        return score

    if name == "video":
        if intents["video"]:
            score += 12.0
        return score

    if name in {"remember", "recall", "forget"}:
        if intents["memory"]:
            score += 10.0
            if name == "forget" and not any("forget" in tok or "забуд" in tok or "удал" in tok for tok in distinctive):
                score -= 6.0
        return score

    if name in {"task", "task_done"}:
        if intents["task"]:
            score += 10.0
        return score

    if name == "schedule":
        if intents["schedule"]:
            score += 11.0
        return score

    if name == "skill_create":
        if intents["skill"]:
            score += 11.0
        return score

    if name == "ask_user":
        if intents["ask_user"]:
            score += 10.0
        return score

    return score


def _is_mcp_repo_tool(item: dict) -> bool:
    hay = " ".join([
        str(item.get("server_id") or ""),
        str(item.get("server_name") or ""),
        str(item.get("name") or ""),
        str(item.get("description") or ""),
    ]).lower()
    return any(tok in hay for tok in ("repo", "repository", "github", "gitlab", "git", "branch", "commit", "pull_request", "issue"))


def _score_mcp(item: dict, intents: dict[str, bool], distinctive: list[str]) -> float:
    server_id = str(item.get("server_id") or "").lower()
    server_name = str(item.get("server_name") or "").lower()
    tool_name = str(item.get("name") or "").lower()
    description = str(item.get("description") or "").lower()
    annotations = item.get("annotations") if isinstance(item.get("annotations"), dict) else {}

    id_tokens = set(_split_identifiers(f"{server_id} {server_name} {tool_name}"))
    desc_tokens = set(_split_identifiers(description))
    schema = item.get("inputSchema") if isinstance(item.get("inputSchema"), dict) else {}
    prop_tokens = set(_split_identifiers(" ".join((schema.get("properties") or {}).keys())))

    score = 0.0

    # Distinctive token matches (skip generic words so "search"/"open" never
    # pulls unrelated MCP tools into a web query).
    id_hits = 0
    desc_hits = 0
    for tok in distinctive:
        if tok in id_tokens or any(tok in t or (len(tok) >= 4 and t in tok) for t in id_tokens):
            id_hits += 1
        elif tok in desc_tokens or tok in prop_tokens:
            desc_hits += 1

    if id_hits:
        score += id_hits * 5.0
    if desc_hits:
        score += desc_hits * 2.0

    is_repo_tool = _is_mcp_repo_tool(item)
    if intents["repo"] and is_repo_tool:
        score += 7.0
        # Specific boost for core repo find/open/read tools
        if any(k in tool_name for k in ("repo", "repository", "repositories", "file_contents", "tree", "readme", "branch", "commit")):
            score += 3.0
        # Boost issue/PR tools when explicitly mentioned, demote otherwise
        if any(k in tool_name for k in ("issue", "pull_request")):
            if any(k in " ".join(distinctive) for k in ("issue", "issues", "pull", "pr", "ишью", "пулл")):
                score += 6.0
            else:
                score -= 5.0
        # Demote unrelated account/notification/gist tools unless explicitly asked
        if any(k in tool_name for k in ("notification", "gist", "user", "get_me")) and not any(
            k in " ".join(distinctive) for k in ("notification", "gist", "user", "profile")
        ):
            score -= 5.0

    if intents["mcp"] and not intents["repo"] and score == 0:
        # User asked generally about MCP servers/tools
        score += 4.0

    # Read vs write vs destructive gating for MCP tools:
    # A read-only query ("посмотри на мой репозиторий") should surface find/open/
    # read/get/list tools and exclude create/delete/merge/push tools.
    # Destructive tools (delete_repo, etc.) stay hidden unless delete intent is explicit.
    is_destructive = (
        tool_name.startswith(_DELETE_TOOL_PREFIXES)
        or annotations.get("destructiveHint") is True
    )
    is_write_tool = (
        tool_name.startswith(_MCP_WRITE_PREFIXES)
        or is_destructive
        or annotations.get("readOnlyHint") is False
    )
    is_read_tool = (
        annotations.get("readOnlyHint") is True
        or any(k in id_tokens for k in ("find", "search", "open", "get", "read", "list", "view", "show", "tree", "contents", "info"))
    )

    if is_destructive and not intents["delete_action"]:
        score -= 12.0
    elif intents["read_action"] and not intents["write_action"]:
        if is_write_tool and not is_read_tool:
            score -= 10.0
        elif is_read_tool and score > 0:
            score += 3.0
    elif intents["write_action"] and is_write_tool and score > 0:
        score += 3.0

    # If the query is purely a web search/info query without repo/mcp/distinctive
    # MCP identifier hits, do not pollute results with MCP tools.
    if not intents["repo"] and not intents["mcp"] and id_hits == 0 and desc_hits < 2:
        return 0.0

    return score


def search_tools(query: str, specs: list[Any], mcp_tools: list[dict] | None = None,
                 limit: int = 5, goal: str = "") -> list[dict]:
    """Return only the tools relevant to ``query`` from permitted specs and MCP tools."""
    try:
        cap = max(1, min(8, int(limit or 5)))
    except (TypeError, ValueError):
        cap = 5

    q = str(query or "").strip()
    if not q:
        q = str(goal or "").strip()

    intents = _detect_intents(q, goal)
    distinctive = _distinctive_tokens(q)
    mcp_list = [item for item in (mcp_tools or []) if isinstance(item, dict) and item.get("name")]
    has_mcp_repo = any(_is_mcp_repo_tool(item) for item in mcp_list)

    # Check if any domain or distinctive MCP hit exists; if not, default general
    # informational queries to web search + web open (when available).
    has_explicit_non_web = any(intents[k] for k in (
        "repo", "mcp", "files", "terminal", "image", "audio", "video",
        "memory", "knowledge", "task", "schedule", "skill", "ask_user",
    ))

    scored: list[tuple[float, int, dict]] = []

    for item in mcp_list:
        s = _score_mcp(item, intents, distinctive)
        if s > 0:
            scored.append((s, 0, {
                "tool": str(item["name"]),
                "server_id": str(item.get("server_id") or ""),
                "kind": "mcp",
                "description": str(item.get("description") or "")[:240],
                "parameters": item.get("inputSchema") or {"type": "object", "properties": {}},
            }))

    # If MCP matched strongly for a repo/MCP query, do not fallback to web.
    has_mcp_hits = bool(scored)
    if not intents["web"] and not has_explicit_non_web and not has_mcp_hits:
        intents["web"] = True

    for spec in specs or []:
        name = str(getattr(spec, "name", "") or "")
        if not name or name in _ALIAS_TOOLS:
            continue
        s = _score_builtin(name, spec, intents, distinctive, has_mcp_repo, bool(mcp_list), q)
        if s > 0:
            scored.append((s, 1, {
                "tool": name,
                "kind": "builtin",
                "description": str(getattr(spec, "description", "") or "")[:240],
                "parameters": dict(getattr(spec, "schema", None) or {"type": "object", "properties": {}}),
            }))

    if not scored:
        return []

    scored.sort(key=lambda entry: (-entry[0], entry[1], entry[2]["tool"]))
    top_score = scored[0][0]
    cutoff = max(3.0, top_score * 0.45)

    filtered: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()
    for score, _priority, payload in scored:
        if score < cutoff:
            continue
        dedup_key = (payload.get("server_id", ""), payload["tool"])
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        filtered.append(payload)
        if len(filtered) >= cap:
            break

    return filtered
