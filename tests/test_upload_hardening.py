"""Upload validation: filenames, magic bytes, size, ZIP bombs, hooks."""
import io
import os
import zipfile

import pytest

import upload_security as us


def test_filename_normalization_blocks_traversal():
    assert us.normalize_filename("../../../etc/passwd") == "passwd"
    assert us.normalize_filename("..\\..\\secret.txt") == "secret.txt"
    assert us.normalize_filename("a\x00b.png") == "ab.png"
    assert us.normalize_filename("") == "upload.bin"


def test_validate_upload_rejects_mismatch_and_unsupported():
    _, _, _, error = us.validate_upload("note.txt", "image/png", b"\x89PNG\r\n\x1a\n...", 100)
    assert error == "CONTENT_MISMATCH"
    _, _, _, error = us.validate_upload("evil.exe", "application/octet-stream", b"MZ...", 100)
    assert error == "UNSUPPORTED_EXTENSION"
    _, _, _, error = us.validate_upload("big.txt", "text/plain", b"a", us.MAX_UPLOAD_BYTES + 1)
    assert error == "TOO_LARGE"
    name, ext, _, error = us.validate_upload("../../report.txt", "text/plain", b"hello", 5)
    assert error == "" and name == "report.txt" and ext == ".txt"


def test_validate_upload_accepts_real_pdf_and_png():
    assert us.validate_upload("a.pdf", "application/pdf", b"%PDF-1.7\n", 20)[3] == ""
    assert us.validate_upload("a.png", "image/png", b"\x89PNG\r\n\x1a\n", 20)[3] == ""


def test_zip_bomb_detection(tmp_path):
    path = tmp_path / "doc.docx"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", os.urandom(100_000))
    assert us.check_zip_bomb(path) == ""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"A" * (us.MAX_ZIP_UNCOMPRESSED + 1))
    assert "ZIP-bomb" in us.check_zip_bomb(path)


def test_zip_path_traversal_rejected(tmp_path):
    path = tmp_path / "book.xlsx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("../../../evil.txt", "x")
    assert "Недопустимый путь" in us.check_zip_bomb(path)


def test_antivirus_hook_is_opt_in(monkeypatch, tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("ok")
    monkeypatch.delenv("AV_SCAN_COMMAND", raising=False)
    assert us.antivirus_hook(path) == ""
    monkeypatch.setenv("AV_SCAN_COMMAND", "false {path}")
    assert us.antivirus_hook(path) != ""
    monkeypatch.setenv("AV_SCAN_COMMAND", "true {path}")
    assert us.antivirus_hook(path) == ""


def test_endpoint_rejects_disguised_and_unsupported_files(client, monkeypatch):
    import routes.media as media_routes

    monkeypatch.setattr(media_routes, "_call_selected_text_ai", lambda prompt: ("unused", None))
    disguised = client.post("/api/attachments/analyze", data={
        "file": (io.BytesIO(b"\x89PNG\r\n\x1a\nnot really png"), "notes.txt")},
        content_type="multipart/form-data")
    assert disguised.status_code == 415
    unsupported = client.post("/api/attachments/analyze", data={
        "file": (io.BytesIO(b"MZ\x90\x00"), "evil.exe")},
        content_type="multipart/form-data")
    assert unsupported.status_code == 415


def test_endpoint_rejects_file_larger_than_limit_before_saving(client, monkeypatch):
    import routes.media as media_routes

    monkeypatch.setattr(us, "MAX_UPLOAD_BYTES", 32)
    monkeypatch.setattr(media_routes, "_call_selected_text_ai", lambda prompt: ("unused", None))
    response = client.post("/api/attachments/analyze", data={
        "file": (io.BytesIO(b"x" * 64), "large.txt")},
        content_type="multipart/form-data")
    assert response.status_code == 413


def test_generated_image_route_never_serves_other_directories(client, tmp_path, monkeypatch):
    import media_generation as mg

    img_dir = tmp_path / "images"
    img_dir.mkdir()
    monkeypatch.setattr(mg, "IMAGE_DIR", str(img_dir))
    (img_dir / "ok.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "private.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    assert client.get("/generated_image?file=ok.png").status_code == 200
    for traversal in ("../private.png", "/../../private.png", "..%2Fprivate.png"):
        assert client.get("/generated_image", query_string={"file": traversal}).status_code == 404
