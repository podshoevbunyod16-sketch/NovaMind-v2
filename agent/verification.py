"""Evidence-based completion checks for action-oriented requests."""
from __future__ import annotations

import re

import language_policy

_ACTION = re.compile(r"(?iu)\b(?:создай|сделай|напиши|сохрани|запусти|выполни|проверь|исправь|установи|создать|написать|запустить|проверить|build|create|write|save|run|execute|test|deploy)\b")
_WRITE = re.compile(r"(?iu)(?:файл|отч[её]т|сайт|приложени|код|документ|website|report|file)")
_CHECK = re.compile(r"(?iu)(?:проверь|протестируй|тест|запусти|выполни|проверить|test|run|execute)")
# These direct imperatives request an artifact, unlike questions about how to draw.
_MEDIA_GOAL = re.compile(r"(?i)^\s*(?:пожалуйста[, ]+|please\s+)?(?:(?P<image>нарисуй|draw|paint)|озвучь|прочитай вслух|read aloud)\b")
_CLAIM = re.compile(r"(?iu)\b(?<!не )(?<!not )(?<!never )(?:готов[оа]?\b|выполнено|нарисовал(?:а)?|озвучил(?:а)?|отредактировал(?:а)?|создан[оа]?\b|создал(?:а)?|сделал(?:а)?|запустил(?:а)?|проверил(?:а)?|успешно|готовый результат|done|completed|created|successfully)")


# A plan may answer a planning question, but is not evidence of execution.
_ADVICE = re.compile(r"(?i)^\s*(?:как\b|объясни\b|составь план\b|how (?:can|do|to)\b|explain\b)")
_PROMISE = re.compile(r"(?i)^\s*(?:мой план|план\s*[:\n]|сначала (?:создам|проверю|запущу|открою)|"
                      r"(?:я )?(?:создам файл|запущу тесты)|(?:my )?plan\s*:|(?:first,? )?I(?: will|'ll)\b)")


def verify_task(goal: str, observations: list[dict], *, image_source: str = "") -> dict:
    """Return evidence required for claims about concrete work.

    A plain informational answer does not require tool evidence. Action requests
    do; file creation and verification requests each require corresponding
    successful tools in the observation log.
    """
    goal = str(goal or "")
    media_goal = _MEDIA_GOAL.match(goal)
    needs_action = not _ADVICE.search(goal) and bool(_ACTION.search(goal) or media_goal)
    needs_write = needs_action and bool(_WRITE.search(goal))
    needs_check = needs_action and bool(_CHECK.search(goal))
    successful = []
    for item in observations or []:
        if not isinstance(item, dict) or item.get("tool") == "tool_search":
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        if result.get("ok") is True:
            successful.append({"tool": str(item.get("tool") or ""), "result": result})
    write_tools = {"file_write", "file_edit", "file_create"}
    check_tools = {"shell", "bash", "python", "terminal"}
    has_write = any(item["tool"] in write_tools for item in successful)
    # Inspecting data via a real MCP read is a check too; it need not be a Linux command.
    has_check = any(item["tool"] in check_tools or
                    (item["tool"] == "mcp_call" and item["result"].get("result")) for item in successful)
    missing = []
    # Verify what was requested/attempted, not just the existence of *some* successful
    # lookup. This check never selects the tool to run.
    media_names = {"image_generate": "image", "image": "image", "audio": "audio", "video": "video"}
    attempts = [media_names[item["tool"]] for item in observations or []
                if isinstance(item, dict) and item.get("tool") in media_names]
    kind = attempts[-1] if attempts else ""
    if media_goal:
        kind = "image" if media_goal.group("image") else "audio"
    if image_source:
        kind = "image"
    artifact_missing = False
    if kind:
        needs_action = True
        needs_write = False  # A media artifact need not be a workspace file_write.
        artifact_missing = not any(
            media_names.get(item["tool"]) == kind and isinstance(media := item["result"].get("result"), dict)
            and (media.get("url") or (kind == "video" and media.get("job_id")))
            and (not image_source or media.get("edited_from") == image_source) for item in successful)
        if artifact_missing:
            missing.append(language_policy.pick(en="an edited image from the selected original",
                                                ru="изображение, изменённое из выбранного оригинала",
                                                tg="тасвири таҳриршуда аз нусхаи интихобшуда") if image_source else
                           language_policy.pick(en="the requested media file", ru="запрошенный медиафайл",
                                                tg="файли медиаи дархостшуда"))
    if needs_action and not successful:
        missing.append(language_policy.pick(
            en="a successful tool result", ru="успешный результат инструмента",
            tg="натиҷаи бомуваффақияти асбоб"))
    if needs_write and not has_write:
        missing.append(language_policy.pick(
            en="proof that the file was created or changed",
            ru="подтверждение создания или изменения файла",
            tg="тасдиқи сохта ё тағйир додани файл"))
    if needs_check and not has_check:
        missing.append(language_policy.pick(
            en="a check or command result", ru="результат проверки/команды",
            tg="натиҷаи санҷиш/фармон"))
    return {"verified": needs_action and not missing if needs_action else True,
            "required": needs_action, "missing": missing, "artifact_missing": artifact_missing,
            "media_kind": kind, "media_attempted": kind in attempts,
            "evidence": [{"tool": item["tool"],
                          "summary": str(item["result"].get("result") or item["result"].get("content") or item["result"].get("output")
                                         or language_policy.pick(en="successful response",
                                                                 ru="успешный ответ",
                                                                 tg="ҷавоби бомуваффақият"))[:300]}
                         for item in successful[:12]]}


def honest_answer(goal: str, answer: str, verification: dict) -> str:
    """Prevent unsupported completion claims without masking non-success."""
    text = str(answer or "").strip()
    if verification.get("required") and not verification.get("verified") and (_CLAIM.search(text) or _PROMISE.match(text)):
        missing = ", ".join(verification.get("missing") or [language_policy.pick(
            en="evidence", ru="доказательства", tg="далелҳо")])
        prefix = language_policy.pick(
            en=f"I cannot honestly confirm that the task is complete: {missing} is missing.",
            ru=f"Не могу честно подтвердить завершение задачи: нет {missing}.",
            tg=f"Ман одилона тасдиқ карда наметавонам, ки вазифа иҷро шуд: {missing} нест.",
        )
        return prefix  # Do not echo the very success claim that failed verification.
    return text
