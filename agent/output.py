"""First-party final-answer hygiene for the ReAct runner.

This is an output-boundary filter, not a private-reasoning transcript parser. It
removes explicit thought markers and recognizable process notes while preserving
ordinary technical prose, code examples, and translations.
"""
from __future__ import annotations

import json
import re

_THINK_BLOCK = re.compile(r"(?is)<think(?:ing)?\b[^>]*>.*?</think(?:ing)?\s*>")
_THINK_TAIL = re.compile(r"(?is)<think(?:ing)?\b[^>]*>.*$")
_FINAL_LABEL = re.compile(r"(?im)^\s*(?:final answer|итоговый ответ|итог)\s*[:：]\s*")
_META_START = re.compile(
    r"(?iu)^\s*(?:"
    r"we\s+(?:need|should|must)\s+(?:answer|respond|mention|explain|think|understand)|"
    r"(?:the\s+)?user\s+(?:asks|asked|wants|provided|supplied)|"
    r"this\s+is\s+a\s+question\b.*?we\s+(?:need|should)\s+to\s+answer|"
    r"let'?s\s+(?:formulate|draft|answer|think)|"
    r"then\s+explanation\b|"
    r"thinking\s*[:：]|analysis\s*[:：]|reasoning\s*[:：]|"
    r"нужно\s+(?:ответить|сформулировать|упомянуть)|"
    r"пользователь\s+(?:спрашивает|просит|хочет|прислал)|"
    r"давай(?:те)?\s+(?:сформулируем|ответим|подумаем)|"
    r"рассуждения\s*[:：]|анализ\s*[:：])"
)
_PROCESS_TAIL = re.compile(
    r"(?iu)^\s*(?:then\s+explanation\b|they\s+(?:supplied|provided)|"
    r"the\s+user\s+(?:asks|wants|provided)|"
    r"затем\s+объяснение\b|пользователь\s+(?:спрашивает|просит))"
)
_INLINE_PROCESS_TAIL = re.compile(
    r"(?iu)\s+(?:then\s+explanation\b|they\s+(?:supplied|provided)|"
    r"the\s+user\s+(?:asks|wants|provided)|затем\s+объяснение\b).*$"
)
_CLOSED_FENCES = re.compile(r"(?s)```.*?```")
_TRUNCATED_END = re.compile(r"(?iu)(?:[,;:—–-]|\b(?:на|и|в|с|для|который|the|and|with|to|of|for|that|which))$")
_QUOTED = re.compile(r'["“«]([^"”»]{45,1000})["”»]', re.S)
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")

#: Хвост-подпись, которая ничего не обрывает, а только анонсирует список:
#: «Источники:», «Список ссылок:», «Sources:». Модели (особенно после веб-поиска)
#: заканчивают ответ такой строкой, потому что ссылки рисует интерфейс. Раньше
#: двоеточие в конце считалось признаком обрыва, ответ отбрасывался, и
#: пользователь вместо данных видел «не удалось получить чистый ответ».
_LABEL_WORDS = re.compile(
    r"(?iu)^(?:"
    r"источник(?:и|ов|а)?|ссылки(?:\s+на\s+источники)?|"
    r"список\s+(?:ссылок|источников)|литература|см\.?\s*также|"
    r"дополнительно|подробнее|по\s+теме|"
    r"sources?|references?|links?|further\s+reading"
    r")(?:\s*\([^)]*\))?$"
)
#: Последняя строка, после которой текст явно не закончен (анонс продолжения).
_OPEN_TAIL_WORDS = re.compile(
    r"(?iu)^(?:итог|вывод|выводы|резюме|в\s+итоге|таким\s+образом|"
    r"продолжение|далее|summary|conclusion|to\s+be\s+continued)$"
)
_EMPHASIS = re.compile(r"(?:\*\*|__|\*|_)+")
_TRAILING_REFS = re.compile(r"[\s,;]*\[\s*[0-9][0-9,\s\-–—]*\][\s,;.\[\]0-9\-–—]*$")
_SENTENCE_END = re.compile(r"[.!?…»\"')\]]$")


def _announcement_label(line: str) -> str:
    """Return the bare label of an announcement line («**Источники:** [1]» → label)."""
    text = _EMPHASIS.sub("", str(line or "").strip())
    text = text.lstrip("#").strip()
    text = _TRAILING_REFS.sub("", text)
    text = re.sub(r"\s*[:：]\s*$", "", text)
    text = re.sub(r"[\s:\-–—]+$", "", text)
    return text.strip()


def _strip_announcement_tail(text: str) -> str:
    """Remove a trailing «Источники:» / «Итог:» label that only announces a list.

    Such a line is not a cut-off sentence: the sources are rendered by the
    interface under the bubble. Dropping it keeps the answer usable instead of
    rejecting it as "incomplete" and showing the user a failure notice.
    Ordinary prose such as «Источники энергии в Таджикистане:» is not a bare
    label and stays untouched.
    """
    lines = str(text or "").rstrip().splitlines()
    removed = False
    while lines:
        last = lines[-1].strip()
        if not last:
            lines.pop()
            continue
        label = _announcement_label(last)
        if label and (_LABEL_WORDS.fullmatch(label) or _OPEN_TAIL_WORDS.fullmatch(label)):
            lines.pop()
            removed = True
            continue
        break
    return "\n".join(lines).strip() if removed else str(text or "").strip()


def _prepare(raw: str) -> str:
    text = str(raw or "").strip()
    text = _THINK_BLOCK.sub("", text)
    text = _THINK_TAIL.sub("", text)
    text = _FINAL_LABEL.sub("", text).strip()
    if not text:
        return ""
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        value = None
    if isinstance(value, dict) and any(key in value for key in ("action", "tool", "tool_input")):
        return ""

    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if paragraphs and _META_START.search(paragraphs[0]):
        answer_start = next((index for index, part in enumerate(paragraphs)
                             if not _META_START.search(part)), None)
        if answer_start is None:
            return ""
        paragraphs = paragraphs[answer_start:]
    while len(paragraphs) > 1 and _PROCESS_TAIL.search(paragraphs[-1]):
        paragraphs.pop()
    cleaned = "\n\n".join(paragraphs).strip()
    cleaned = _INLINE_PROCESS_TAIL.sub("", cleaned).strip()
    if _META_START.search(cleaned):
        return ""
    return _strip_announcement_tail(cleaned)


def is_truncated_answer(raw: str) -> bool:
    """Recognize a plausible cut-off response, excluding internal process notes."""
    prepared = _prepare(raw)
    if not prepared:
        return False
    if prepared.count("```") % 2:
        return True
    prose = _CLOSED_FENCES.sub(" ", prepared).strip()
    return bool(prose and _TRUNCATED_END.search(prose))


def partial_answer_prefix(raw: str) -> str:
    """Return an answer-shaped partial prefix for a bounded continuation attempt."""
    return _prepare(raw)


def clean_final_answer(raw: str, goal: str = "") -> str:
    """Strip explicit thought/process leakage and reject incomplete endings."""
    cleaned = _prepare(raw)
    if not cleaned or cleaned.count("```") % 2 or is_truncated_answer(cleaned):
        return ""
    return cleaned


def append_continuation(prefix: str, continuation: str) -> str:
    """Join a model continuation while avoiding duplicated overlap."""
    first, second = str(prefix or "").rstrip(), str(continuation or "").lstrip()
    if not first:
        return second
    if second.startswith(first):
        return second
    max_overlap = min(len(first), len(second), 1200)
    for overlap in range(max_overlap, 39, -1):
        if first.endswith(second[:overlap]):
            second = second[overlap:].lstrip()
            break
    return first + (" " if second else "") + second


def salvage_quoted_answer(raw: str, goal: str = "") -> str:
    """Extract a clearly answer-shaped quoted passage as a last-resort rescue."""
    goal_words = {word.lower() for word in re.findall(r"[\wа-яё]+", str(goal or ""), re.I)}
    for match in reversed(list(_QUOTED.finditer(str(raw or "")))):
        candidate = match.group(1).strip()
        words = {word.lower() for word in re.findall(r"[\wа-яё]+", candidate, re.I)}
        overlap = len(words & goal_words) / max(1, len(words))
        if overlap > 0.8 or not _CYRILLIC.search(candidate):
            continue
        answer = clean_final_answer(candidate, goal)
        if answer:
            return answer
    return ""
