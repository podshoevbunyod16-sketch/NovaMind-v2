"""upload_security.py — validation of untrusted file uploads.

Rules implemented here are intentionally simple and testable:
  * filenames are normalised to a single safe basename;
  * extension, declared MIME and magic bytes must agree for binary formats;
  * files are checked for size before they are written to disk;
  * ZIP-based office formats are checked for bombs (entries, ratio, total size);
  * an optional external antivirus command can be configured by the operator.

No file is ever served from this module.  Storage remains a temporary,
per-request directory owned by the caller.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import zipfile
from pathlib import Path

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
MAX_ZIP_UNCOMPRESSED = int(os.getenv("MAX_ZIP_UNCOMPRESSED", str(200 * 1024 * 1024)))
MAX_ZIP_ENTRIES = int(os.getenv("MAX_ZIP_ENTRIES", "1000"))
MAX_ZIP_RATIO = int(os.getenv("MAX_ZIP_RATIO", "200"))

TEXT_EXTENSIONS = frozenset({
    ".txt", ".json", ".csv", ".tsv", ".py", ".js", ".ts", ".jsx", ".tsx",
    ".html", ".htm", ".css", ".md", ".xml", ".yaml", ".yml", ".log", ".ini",
    ".cfg", ".conf", ".sql", ".sh", ".bash", ".java", ".c", ".cpp", ".h",
    ".hpp", ".go", ".rs", ".php",
})
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})
OFFICE_EXTENSIONS = frozenset({".docx", ".xlsx", ".xlsm", ".pptx"})
ALLOWED_EXTENSIONS = TEXT_EXTENSIONS | IMAGE_EXTENSIONS | OFFICE_EXTENSIONS | {".pdf"}

_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
    (b"RIFF", ".webp"),
    (b"%PDF-", ".pdf"),
    (b"PK\x03\x04", ".zip"),
)
_UNSAFE_NAME = re.compile(r"[\x00-\x1f\x7f/\\]")


def normalize_filename(raw: str) -> str:
    """Return a safe basename, never containing path separators or control chars."""
    name = str(raw or "").replace("\x00", "")
    name = os.path.basename(name.replace("\\", "/"))
    name = _UNSAFE_NAME.sub("_", name).strip(" .")
    if not name:
        return "upload.bin"
    if len(name) > 120:
        stem, ext = os.path.splitext(name)
        name = stem[:120 - len(ext)] + ext
    return name


def sniff_extension(head: bytes) -> str:
    """Best-effort content type from magic bytes; '' when unknown/text."""
    for magic, ext in _MAGIC:
        if head.startswith(magic):
            return ext
    if b"\x00" not in head:
        try:
            head.decode("utf-8")
            return ".txt"
        except UnicodeDecodeError:
            pass
    return ""


def validate_upload(filename: str, declared_type: str, head: bytes,
                    size: int | None = None) -> tuple[str, str, str, str]:
    """Validate an upload before writing it to disk.

    Returns ``(safe_name, extension, normalised_content_type, error)``.  ``error``
    is empty when validation succeeds.  Size errors use a distinct message so
    callers can map it to HTTP 413.
    """
    safe_name = normalize_filename(filename)
    ext = os.path.splitext(safe_name)[1].lower()
    if size is not None and size > MAX_UPLOAD_BYTES:
        return safe_name, ext, declared_type, "TOO_LARGE"
    if ext not in ALLOWED_EXTENSIONS:
        return safe_name, ext, declared_type, "UNSUPPORTED_EXTENSION"
    detected = sniff_extension(head)
    binary_exts = IMAGE_EXTENSIONS | {".pdf"} | OFFICE_EXTENSIONS
    if ext in binary_exts:
        expected = ".zip" if ext in OFFICE_EXTENSIONS else ext
        if detected != expected:
            return safe_name, ext, declared_type, "CONTENT_MISMATCH"
    elif ext in TEXT_EXTENSIONS and detected not in ("", ".txt"):
        return safe_name, ext, declared_type, "CONTENT_MISMATCH"
    content_type = declared_type or "application/octet-stream"
    return safe_name, ext, content_type, ""


def check_zip_bomb(path: str | Path) -> str:
    """Detect ZIP bombs in office documents. Returns an error string or ''."""
    path = Path(path)
    if path.suffix.lower() not in OFFICE_EXTENSIONS:
        return ""
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                return "Слишком много элементов в архиве"
            total = sum(info.file_size for info in infos)
            compressed = sum(info.compress_size for info in infos) or 1
            if total > MAX_ZIP_UNCOMPRESSED or total / compressed > MAX_ZIP_RATIO:
                return "Подозрительно большой архив (возможна ZIP-bomb)"
            for info in infos:
                target = os.path.realpath(os.path.join(str(path), "..", info.filename))
                if info.filename.startswith("/") or ".." in Path(info.filename).parts:
                    return "Недопустимый путь внутри архива"
                if not target.startswith(os.path.dirname(str(path))):
                    return "Недопустимый путь внутри архива"
    except (zipfile.BadZipFile, OSError):
        return "Повреждённый документ"
    return ""


def antivirus_hook(path: str | Path) -> str:
    """Run an operator-configured scanner when available; never enabled by default.

    ``AV_SCAN_COMMAND`` is a shell-like template, e.g. ``clamscan --no-summary {path}``.
    Non-zero exit means "infected" or "scanner error" — either way the file is
    rejected.  The command output is not logged or returned to the client.
    """
    template = os.getenv("AV_SCAN_COMMAND", "").strip()
    if not template:
        return ""
    try:
        argv = [part.format(path=str(path)) for part in shlex.split(template)]
        result = subprocess.run(argv, capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError, KeyError, ValueError):
        return "Антивирусная проверка недоступна"
    return "Файл отклонён антивирусом" if result.returncode != 0 else ""
