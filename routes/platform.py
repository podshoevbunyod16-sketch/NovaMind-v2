import hmac
import os
from flask import Blueprint, request, jsonify
from ai_providers import chat_completion

platform_bp = Blueprint("platform", __name__)
PLATFORM_TOKEN = os.getenv("PLATFORM_TOKEN", "")


def ask_model(text: str) -> str:
    reply, meta = chat_completion(
        [{"role": "user", "content": text}],
        system="Ты полезный AI-ассистент.",
        temperature=0.7,
        max_tokens=1024,
        timeout=60,
    )
    if reply is None:
        raise RuntimeError(str(meta))
    return reply


@platform_bp.route("/api/platform/run", methods=["POST"])
def platform_run():
    if not PLATFORM_TOKEN:
        return jsonify({"error": "platform token not configured"}), 503
    auth = request.headers.get("Authorization", "")
    if not hmac.compare_digest(auth, f"Bearer {PLATFORM_TOKEN}"):
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    text = str(data.get("text", "")).strip()
    if not text:
        return jsonify({"error": "text is required"}), 400
    try:
        return jsonify({"reply": ask_model(text)})
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502
