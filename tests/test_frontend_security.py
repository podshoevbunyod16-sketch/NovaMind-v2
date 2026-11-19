"""Static, network-free regression checks for frontend injection boundaries."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "static" / "app.js"
ADMIN_HTML = ROOT / "templates" / "admin.html"


def test_dynamic_frontend_markup_has_no_inline_event_handlers():
    app = APP_JS.read_text(encoding="utf-8")
    admin = ADMIN_HTML.read_text(encoding="utf-8")
    inline_handler = r"\bon(?:click|error|load|mouseover)\s*=\s*['\"]"
    assert not re.search(inline_handler, app, re.IGNORECASE)
    assert not re.search(inline_handler, admin, re.IGNORECASE)
    assert "addEventListener('click'" in app
    assert "addEventListener('click'" in admin


def test_untrusted_urls_are_allowlisted_before_href_or_src_rendering():
    app = APP_JS.read_text(encoding="utf-8")
    assert "function safeUrl(value)" in app
    assert "javascript:" in app and "data:" in app and "vbscript:" in app
    assert "const avatarUrl = data.google_login ? safeUrl(data.avatar) : ''" in app
    assert "const sourceUrl = safeUrl(source.url)" in app
    assert "const url = safeUrl(payload.url)" in app
    assert "const imageUrl = safeUrl(imageMatch[1])" in app
    assert "const authUrl = safeUrl(url)" in app


def test_dynamic_values_use_text_content_or_escaped_data_attributes():
    admin = ADMIN_HTML.read_text(encoding="utf-8")
    app = APP_JS.read_text(encoding="utf-8")
    assert "statsGrid.replaceChildren" in admin
    assert "statValue.textContent = String(value)" in admin
    assert "button.textContent = '📄 ' + String(filename)" in admin
    assert "data-composio-toolkit=\"${escapeHtml(card.slug)}\"" in app
    assert "button.dataset.composioToolkit" in app
    assert "onclick=" not in app.lower()
    assert "onclick=" not in admin.lower()
