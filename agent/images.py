"""Chat-scoped inputs for the image tool. Only server-created filenames are handles."""
from __future__ import annotations

import contextvars
import io
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import media_generation as mg

MAX_IMAGE_BYTES = 8 * 1024 * 1024
_UPLOAD_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
_NAME = re.compile(r"[a-zA-Z0-9_-]{1,150}\.(?:png|jpe?g|webp)\Z", re.I)


@dataclass(frozen=True)
class ImageTurn:
    chat_id: str
    model: dict | None = None  # Exact provider/model chosen by the human, not by the LLM.
    source: str = ""


_current = contextvars.ContextVar("react_images", default=ImageTurn(""))
current = _current.get


@contextmanager
def bind(turn: ImageTurn):
    token = _current.set(turn)
    try:
        yield
    finally:
        _current.reset(token)


def model_selection(value):
    if value is None:
        return None  # Auto
    if not isinstance(value, dict) or not all(
            isinstance(value.get(k), str) and 0 < len(value[k].strip()) <= 256
            for k in ("provider", "model")):
        raise ValueError("Укажите провайдера и идентификатор модели изображений.")
    return {k: value[k].strip() for k in ("provider", "model")}


def check_source(chat_id, name):
    import database
    if (not isinstance(name, str) or not _NAME.fullmatch(name) or not chat_id
            or not database.chat_allowed(chat_id) or not database.has_chat_image(chat_id, name)):
        raise ValueError("Исходное изображение недоступно в этом чате.")


def _image_mime(blob):
    from PIL import Image
    try:
        with Image.open(io.BytesIO(blob), formats=("PNG", "JPEG", "WEBP")) as image:
            if image.width * image.height > 24_000_000:
                raise ValueError("Изображение больше 24 мегапикселей.")
            mime = Image.MIME[image.format]
            image.verify()
        # verify() checks the container; JPEG, in particular, also needs a decode
        # to detect a truncated pixel stream. Both dimensions and bytes are bounded.
        with Image.open(io.BytesIO(blob)) as image:
            image.load()
        return mime
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError("Для редактирования нужен файл PNG, JPEG или WebP.") from exc


def _read(stream):
    blob = stream.read(MAX_IMAGE_BYTES + 1)
    if len(blob) > MAX_IMAGE_BYTES:
        raise ValueError("Изображение больше 8 МБ.")
    return blob, _image_mime(blob)


def store_upload(chat_id, stream):
    import database
    blob, mime = _read(stream)
    # Uploaded originals need not become publicly addressable /media URLs.
    folder = Path(mg.MEDIA_DIR) / ".edit-inputs"
    if folder.is_symlink():
        raise ValueError("Хранилище исходных изображений недоступно.")
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = f"edit_input_{uuid.uuid4().hex}{_UPLOAD_EXT[mime]}"
    path = folder / name
    with path.open("xb") as handle:
        handle.write(blob)
    try:
        database.add_chat_image(chat_id, name)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return name


def discard_uploads(names):
    folder = Path(mg.MEDIA_DIR) / ".edit-inputs"
    if folder.is_symlink():
        return
    for name in names:
        if re.fullmatch(r"edit_input_[0-9a-f]{32}\.(?:png|jpg|webp)", name):
            (folder / name).unlink(missing_ok=True)


def remember_generated(chat_id, result):
    """Register local save_media artifacts only; never trust a URL or a chat message."""
    import database
    name = result.get("filename")
    if not chat_id or not isinstance(name, str) or not _NAME.fullmatch(name):
        return
    path = Path(mg.MEDIA_DIR) / name
    if (result.get("url") == "/media/" + name and result.get("path") == str(path)
            and not path.is_symlink() and path.is_file() and path.stat().st_size <= MAX_IMAGE_BYTES):
        database.add_chat_image(chat_id, name)
        result["editable"] = True


def read_source(chat_id, name):
    check_source(chat_id, name)
    folder = Path(mg.MEDIA_DIR)
    if name.startswith("edit_input_"):
        folder = folder / ".edit-inputs"
    path = folder / name
    if folder.is_symlink() or path.is_symlink() or not path.is_file():
        raise ValueError("Файл исходного изображения больше недоступен.")
    with path.open("rb") as stream:
        return _read(stream)
