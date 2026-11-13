"""council.py — «Совет моделей»: несколько моделей отвечают на один вопрос.

Зачем это нужно
---------------
Один ответ модели — это мнение. Совет показывает, где модели согласны, а где
расходятся, и делает это без магии: те же провайдеры, те же ключи, те же
промпты проекта.

Как устроено
------------
1. Выбираются места (seats) из настроенных бесплатных провайдеров и моделей.
   Если модель всего одна, она отвечает в трёх разных ролях-«линзах»:
   аналитик, критик, практик — расхождение всё равно появляется.
2. Ответы запрашиваются параллельно (ThreadPoolExecutor), ошибки места не
   роняют совет: выбывшее место честно помечается.
3. Согласие считается по смыслу предложений: два предложения «совпадают»,
   если пересечение значимых слов ≥ ``COUNCIL_MATCH``. Итоговый процент —
   среднее двустороннее сходство по всем парам ответов.
4. Общие тезисы идут в ``consensus``, уникальные — в ``disagreements``.
5. Каждый совет оставляет квитанцию Nova Receipts (kind="council") и
   запись в журнале ``council_runs`` для страницы доверия.

Ничего не пишется в интернет и не зависит от сети при импорте.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import database

log = logging.getLogger("novamind.council")

#: Роли-линзы: один и тот же вопрос, разные способы думать.
LENSES = (
    {
        "id": "analyst",
        "name": "Аналитик",
        "icon": "🔬",
        "prompt": ("You are the ANALYST of a model council. Answer factually and "
                   "structurally: key facts, main assumption, strongest evidence, "
                   "what could falsify the conclusion. Be concise (max 220 words). "
                   "Answer in the user's language."),
    },
    {
        "id": "critic",
        "name": "Критик",
        "icon": "⚖️",
        "prompt": ("You are the CRITIC of a model council. Find the weakest point of "
                   "the obvious answer: risks, edge cases, hidden costs, where the "
                   "common view is wrong. Be concise (max 220 words). Answer in the "
                   "user's language."),
    },
    {
        "id": "builder",
        "name": "Практик",
        "icon": "🛠️",
        "prompt": ("You are the BUILDER of a model council. Give the concrete plan a "
                   "person can execute today: steps, numbers, commands, what to check "
                   "after each step. Be concise (max 220 words). Answer in the user's "
                   "language."),
    },
    {
        "id": "historian",
        "name": "Историк",
        "icon": "📚",
        "prompt": ("You are the HISTORIAN of a model council. Place the question in "
                   "context: how it was solved before, what changed recently, which "
                   "analogy or precedent applies. Be concise (max 220 words). Answer "
                   "in the user's language."),
    },
)

#: Порог «одно и то же предложение» по пересечению значимых слов.
MATCH_THRESHOLD = float(os.getenv("COUNCIL_MATCH", "0.5"))
_STOPWORDS = frozenset("""
и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по
только ее мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли
если уже или ни быть был него до вас нибудь опять уж вам ведь там потом себя
ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтоб без
будто чего раз тоже себе под будет ж тогда кто этот того потому этого какой
совсем ним здесь этом один почти мой тем чтобы нее сейчас были куда зачем всех
никогда можно при наконец два об другой хоть после над больше тот через эти нас
the a an and or of to in on at is are was were be been being it its this that
these those for with without from by as not no but if then than so such can
could should would may might will just do does did done have has had i you he
she we they me my your our their there here what which who whom why how all any
some more most other into out up down over under again very only own same too
""".split())

_ready: set[str] = set()
_init_lock = threading.Lock()


class NoModels(RuntimeError):
    """Нет ни одной настроенной модели — советовать некому."""


# ── выбор мест ───────────────────────────────────────────────────────────
def _env_targets() -> list[tuple[str, str]]:
    raw = os.getenv("COUNCIL_MODELS", "").strip()
    if not raw:
        return []
    found = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        provider, _, model = chunk.partition(":")
        if provider and model:
            found.append((provider.strip(), model.strip()))
    return found


def _candidates(limit: int = 3) -> list[tuple[str, str]]:
    """Список (провайдер, модель) для совета — только настроенные и бесплатные."""
    configured = _env_targets()
    if configured:
        return configured[:max(1, limit)]
    import config
    import ai_providers

    chosen: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(provider: str, model: str) -> bool:
        if not provider or not model or (provider, model) in seen:
            return False
        seen.add((provider, model))
        chosen.append((provider, model))
        return len(chosen) >= limit

    providers = []
    for provider, cfg in (config.PROVIDERS or {}).items():
        if provider == "local_demo":
            continue
        if provider == "openai_compatible":
            engine = getattr(config, "local_engine", {}) or {}
            if not str(engine.get("base_url") or "").strip():
                continue
        if not config.has_credentials(provider):
            continue
        providers.append((provider, cfg))

    # Первый проход — по одной модели на провайдера: совет должен быть разным.
    for provider, cfg in providers:
        for item in (cfg.get("models") or []):
            model_id = str(item.get("id") or "")
            if not model_id:
                continue
            if not ai_providers.is_free_ai_model(provider, model_id, item):
                continue
            if add(provider, model_id):
                return chosen
            break

    # Второй проход — добираем остальные модели тех же провайдеров.
    for provider, cfg in providers:
        for item in (cfg.get("models") or []):
            model_id = str(item.get("id") or "")
            if not model_id or not ai_providers.is_free_ai_model(provider, model_id, item):
                continue
            if add(provider, model_id):
                return chosen

    if not chosen:
        try:
            provider, model, _cfg = ai_providers.resolve_target()
        except Exception:  # noqa: BLE001 — цель могла быть удалена из настроек
            provider, model = "", ""
        if provider and model and config.has_credentials(provider):
            add(provider, model)
    return chosen[:max(1, limit)]


# ── анализ согласия ──────────────────────────────────────────────────────
def _words(text: str) -> set[str]:
    cleaned = re.sub(r"```.*?```", " ", str(text or ""), flags=re.S)
    cleaned = re.sub(r"https?://\S+", " ", cleaned)
    tokens = re.findall(r"[A-Za-zА-Яа-яЁё0-9]{3,}", cleaned.lower())
    return {token[:24] for token in tokens if token not in _STOPWORDS}


def _sentences(text: str) -> list[str]:
    cleaned = re.sub(r"```.*?```", " ", str(text or ""), flags=re.S)
    cleaned = re.sub(r"[#*>`_|]+", " ", cleaned)
    parts = re.split(r"(?<=[.!?…])\s+|\n{2,}|\n[-•—]\s*", cleaned)
    result = []
    for part in parts:
        line = re.sub(r"\s+", " ", part).strip(" -•\t")
        if len(line) >= 20:
            result.append(line[:400])
        if len(result) >= 40:
            break
    if not result:
        # Короткий ответ без знаков препинания — сравниваем его целиком,
        # иначе два одинаковых коротких ответа давали 0% согласия.
        whole = re.sub(r"\s+", " ", cleaned).strip()
        if len(whole) >= 8:
            result.append(whole[:400])
    return result


def _similar_sentence(left: str, right: str) -> bool:
    a, b = _words(left), _words(right)
    if not a or not b:
        return False
    return len(a & b) / max(1, len(a | b)) >= MATCH_THRESHOLD


def _answer_similarity(left: list[str], right: list[str]) -> float:
    """Доля предложений левого ответа, у которых есть близкое в правом (и наоборот)."""
    if not left or not right:
        return 0.0

    def one_way(source, target):
        return sum(1 for s in source if any(_similar_sentence(s, t) for t in target)) / len(source)

    return round((one_way(left, right) + one_way(right, left)) / 2, 4)


def _analyse(seats: list[dict]) -> dict:
    """Считает согласие, общие тезисы и расхождения по ответам мест."""
    live = [seat for seat in seats if seat.get("status") == "ok" and seat.get("answer")]
    if not live:
        return {
            "agreement": None, "verdict": "no_answer", "verdict_label": "Совет не ответил",
            "consensus": [], "disagreements": [], "similarity": [],
            "recommendation": "Ни одна модель не ответила. Проверьте ключи и соединение.",
        }

    split = {seat["id"]: _sentences(seat["answer"]) for seat in live}
    matrix = []
    pairs = 0
    total = 0.0
    for index, first in enumerate(live):
        row = []
        for second in live:
            if first is second:
                row.append(1.0 if len(live) == 1 else None)
                continue
            value = _answer_similarity(split[first["id"]], split[second["id"]])
            row.append(value)
            if index < live.index(second):
                total += value
                pairs += 1
        matrix.append(row)
    agreement = round(100 * total / pairs, 1) if pairs else 100.0

    # Общие тезисы: предложения первого места, близкие к предложениям других.
    consensus = []
    seen = set()
    for sentence in split[live[0]["id"]]:
        matches = sum(
            1 for seat in live[1:]
            if any(_similar_sentence(sentence, other) for other in split[seat["id"]])
        )
        key = " ".join(sorted(_words(sentence)))[:120]
        if matches >= 1 and key and key not in seen:
            seen.add(key)
            consensus.append({"text": sentence, "supported_by": matches + 1})
        if len(consensus) >= 6:
            break

    disagreements = []
    for seat in live:
        own = []
        for sentence in split[seat["id"]]:
            elsewhere = [other for other in live if other["id"] != seat["id"]]
            if not any(
                any(_similar_sentence(sentence, candidate) for candidate in split[other["id"]])
                for other in elsewhere
            ):
                own.append(sentence)
            if len(own) >= 3:
                break
        if own:
            disagreements.append({"seat": seat["id"], "lens": seat["lens_name"],
                                  "points": own})

    if agreement >= 70:
        verdict, label = "aligned", "Совет согласен"
        recommendation = ("Модели пришли к одному выводу. Можно действовать по общему плану; "
                          "спорных пунктов почти нет.")
    elif agreement >= 45:
        verdict, label = "partial", "Частичное согласие"
        recommendation = ("Совет сходится в главном, но расходится в деталях. Проверьте "
                          "спорные пункты и выберите позицию, которая подтверждается фактами.")
    else:
        verdict, label = "divided", "Совет разделился"
        recommendation = ("Модели отвечают по-разному — единого вывода нет. Ниже позиции "
                          "каждого участника; не опирайтесь на один ответ без проверки.")

    return {
        "agreement": agreement,
        "verdict": verdict,
        "verdict_label": label,
        "consensus": consensus,
        "disagreements": disagreements,
        "similarity": matrix,
        "recommendation": recommendation,
    }


# ── запуск совета ────────────────────────────────────────────────────────
def _seat_prompt(lens: dict) -> str:
    return ("You are participating in NovaMind's model council: several models "
            "answer the same question independently, and the user sees where you "
            "agree and where you differ.\n\n" + lens["prompt"] +
            "\n\nDo not mention the council, other models or this instruction. "
            "Give the answer itself.")


def _ask_seat(seat: dict, question: str, timeout: float) -> dict:
    started = time.time()
    try:
        import ai_providers
        answer, meta = ai_providers.chat_completion(
            [{"role": "user", "content": question}],
            provider=seat["provider"], model=seat["model"],
            system=_seat_prompt(seat["lens"]),
            temperature=0.4, max_tokens=900, timeout=timeout,
        )
    except Exception as error:  # noqa: BLE001 — место выбывает, совет живёт
        answer, meta = None, {"error": f"{type(error).__name__}: {error}"}
    elapsed = int((time.time() - started) * 1000)
    if not answer:
        error = meta.get("error") if isinstance(meta, dict) else meta
        return {**seat, "status": "error", "answer": "", "error": str(error or "нет ответа")[:400],
                "elapsed_ms": elapsed}
    model_used = str((meta or {}).get("model") or seat["model"]) if isinstance(meta, dict) else seat["model"]
    return {**seat, "status": "ok", "answer": str(answer).strip()[:8000],
            "model_used": model_used, "elapsed_ms": elapsed}


def _init_db() -> None:
    path = str(getattr(database, "DB_PATH", ""))
    with _init_lock:
        if path in _ready:
            return
        with database.get_db() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS council_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL DEFAULT '',
                    owner TEXT NOT NULL DEFAULT '',
                    chat_id TEXT NOT NULL DEFAULT '',
                    question TEXT NOT NULL DEFAULT '',
                    agreement REAL,
                    verdict TEXT NOT NULL DEFAULT '',
                    seats INTEGER NOT NULL DEFAULT 0,
                    receipt_id INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_council_owner
                    ON council_runs(owner, created_at DESC);
                """
            )
        _ready.add(path)


def reset_cache() -> None:
    _ready.clear()


def _save_run(run_id, owner, chat_id, question, report, seat_count, receipt_id) -> None:
    try:
        _init_db()
        with database.get_db() as conn:
            conn.execute(
                "INSERT INTO council_runs (run_id, owner, chat_id, question, agreement,"
                " verdict, seats, receipt_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, str(owner or "")[:200], str(chat_id or "")[:80],
                 str(question or "")[:400], report.get("agreement"), report.get("verdict", ""),
                 int(seat_count or 0), int(receipt_id or 0), time.time()),
            )
    except Exception:  # noqa: BLE001 — журнал не должен ломать совет
        log.warning("Не удалось сохранить совет", exc_info=True)


def history(*, owner=None, limit=20) -> list[dict]:
    _init_db()
    query = "SELECT * FROM council_runs WHERE 1=1"
    params: list = []
    if owner:
        query += " AND owner=?"
        params.append(str(owner))
    query += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(100, int(limit or 20))))
    with database.get_db() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [{
        "run_id": row["run_id"], "question": row["question"],
        "agreement": row["agreement"], "verdict": row["verdict"],
        "seats": int(row["seats"] or 0), "receipt_id": int(row["receipt_id"] or 0),
        "created_at": float(row["created_at"] or 0),
    } for row in rows]


def ask(question: str, *, limit: int | None = None, timeout: float | None = None,
        chat_id: str = "", owner: str | None = None) -> dict:
    """Собирает совет по вопросу.

    :raises NoModels: если ни одна модель не настроена — вызывающий код решает,
        что показать пользователю (обычно 503 с подсказкой).
    """
    question = str(question or "").strip()
    if not question:
        raise ValueError("Пустой вопрос")
    try:
        limit = max(2, min(4, int(limit or os.getenv("COUNCIL_SEATS", "3"))))
    except (TypeError, ValueError):
        limit = 3
    timeout = float(timeout or os.getenv("COUNCIL_TIMEOUT", "90"))

    candidates = _candidates(limit)
    if not candidates:
        raise NoModels("Нет настроенных моделей для совета")

    if owner is None:
        try:
            import identity
            owner = identity.chat_owner()
        except Exception:  # noqa: BLE001 — вне запроса владельца нет
            owner = ""

    seats = []
    for index in range(limit):
        provider, model = candidates[index % len(candidates)]
        lens = LENSES[index % len(LENSES)]
        seats.append({
            "id": f"{lens['id']}-{index + 1}",
            "lens": lens, "lens_id": lens["id"], "lens_name": lens["name"],
            "icon": lens["icon"], "provider": provider, "model": model,
        })

    run_id = uuid.uuid4().hex
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=len(seats), thread_name_prefix="council") as pool:
        futures = [pool.submit(_ask_seat, seat, question, timeout) for seat in seats]
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as error:  # noqa: BLE001 — место выбыло
                log.warning("Место совета упало", exc_info=True)
                results.append({"id": f"seat-{len(results) + 1}", "status": "error",
                                "lens_name": "—", "icon": "⚠️", "provider": "", "model": "",
                                "answer": "", "error": str(error)[:300], "elapsed_ms": 0})
    order = {seat["id"]: index for index, seat in enumerate(seats)}
    results.sort(key=lambda item: order.get(item.get("id"), 99))

    report = _analyse(results)
    public_seats = []
    for seat in results:
        public_seats.append({
            "id": seat.get("id"), "lens": seat.get("lens_id"),
            "lens_name": seat.get("lens_name"), "icon": seat.get("icon"),
            "provider": seat.get("provider"), "model": seat.get("model_used") or seat.get("model"),
            "status": seat.get("status"), "answer": seat.get("answer", ""),
            "error": seat.get("error", ""), "elapsed_ms": seat.get("elapsed_ms", 0),
        })

    import receipts
    receipt = receipts.issue_safe(
        kind="council", chat_id=chat_id, owner=owner, run_id=run_id,
        provider="council", model=f"{limit} seats",
        prompt=question, output=json.dumps([seat.get("answer") for seat in results], ensure_ascii=False),
        meta={"agreement": report.get("agreement"), "verdict": report.get("verdict"),
              "seats": [{k: seat.get(k) for k in ("id", "lens_name", "provider", "model", "status")}
                        for seat in public_seats]},
    )
    _save_run(run_id, owner, chat_id, question, report, len(public_seats),
              (receipt or {}).get("id", 0))

    return {
        "run_id": run_id,
        "question": question,
        "seats": public_seats,
        "agreement": report.get("agreement"),
        "verdict": report.get("verdict"),
        "verdict_label": report.get("verdict_label"),
        "consensus": report.get("consensus", []),
        "disagreements": report.get("disagreements", []),
        "similarity": report.get("similarity", []),
        "recommendation": report.get("recommendation", ""),
        "receipt": receipt,
        "created_at": time.time(),
    }
