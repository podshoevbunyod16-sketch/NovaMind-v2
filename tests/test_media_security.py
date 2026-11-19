"""Regression tests for media upload, ownership and bounded requests."""
import io
from pathlib import Path

import media_generation as mg


def png_bytes():
    return b"\x89PNG\r\n\x1a\n" + b"x" * 32


def test_media_upload_rejects_oversized_input(client, monkeypatch):
    monkeypatch.setattr("routes.media.MEDIA_UPLOAD_MAX_BYTES", 8)
    response = client.post(
        "/api/media/upload",
        data={"file": (io.BytesIO(png_bytes()), "ok.png")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 413


def test_media_upload_requires_signature_and_rejects_svg(client):
    mismatch = client.post(
        "/api/media/upload",
        data={"file": (io.BytesIO(b"not a png"), "x.png")},
        content_type="multipart/form-data",
    )
    assert mismatch.status_code == 415

    svg = client.post(
        "/api/media/upload",
        data={"file": (io.BytesIO(b"<svg><script>alert(1)</script></svg>"), "x.svg")},
        content_type="multipart/form-data",
    )
    assert svg.status_code == 415


def test_media_upload_rejects_path_traversal(client):
    response = client.post(
        "/api/media/upload",
        data={"file": (io.BytesIO(png_bytes()), "../escape.png")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400


def test_uploaded_file_is_not_visible_to_another_browser_session(client):
    uploaded = client.post(
        "/api/media/upload",
        data={"file": (io.BytesIO(png_bytes()), "private.png")},
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200
    url = uploaded.get_json()["url"]
    assert client.get(url).status_code == 200
    with client.session_transaction() as sess:
        sess["nova_sid"] = "different-browser"
    assert client.get(url).status_code == 404


def test_media_owner_read_failure_fails_closed(client, monkeypatch):
    uploaded = client.post(
        "/api/media/upload",
        data={"file": (io.BytesIO(png_bytes()), "private.png")},
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 200

    import database

    def database_failure(*_args, **_kwargs):
        raise OSError("temporary database failure")

    monkeypatch.setattr(database, "media_file_allowed", database_failure)
    assert client.get(uploaded.get_json()["url"]).status_code == 404


def test_owner_registration_failure_removes_uploaded_file(client, monkeypatch):
    import routes.media as media_routes

    monkeypatch.setattr(media_routes, "_remember_owner", lambda *_args, **_kwargs: False)
    response = client.post(
        "/api/media/upload",
        data={"file": (io.BytesIO(png_bytes()), "temporary.png")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 503
    assert list(Path(media_routes.MEDIA_DIR).iterdir()) == []


def test_async_media_owner_failure_discards_completed_output(client, monkeypatch):
    import routes.media as media_routes

    saved = mg.save_media("video", b"video", "video/mp4", prefix="async-test")
    job_id = mg.register_job({
        "job_id": "async-owner-failure",
        "provider": "custom",
        "state": "succeeded",
        "media": saved,
    })
    monkeypatch.setattr(media_routes, "_remember_owner", lambda *_args, **_kwargs: False)

    response = client.get(f"/api/media/jobs/{job_id}")
    assert response.status_code == 503
    assert not Path(saved["path"]).exists()
    assert mg.get_job(job_id)["state"] == "failed"


def test_analyze_rejects_large_input_before_provider_call(client, monkeypatch):
    monkeypatch.setattr("routes.media.ANALYZE_MAX_BYTES", 8)
    monkeypatch.setattr("routes.media._call_selected_text_ai", lambda *_: (_ for _ in ()).throw(AssertionError("provider called")))
    response = client.post(
        "/api/attachments/analyze",
        data={"file": (io.BytesIO(b"0123456789"), "note.txt")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 413


def test_tts_route_rejects_large_text_before_provider(client, monkeypatch):
    monkeypatch.setattr("routes.media.TTS_MAX_CHARS", 8)
    response = client.post("/api/media/tts", json={"text": "0123456789"})
    assert response.status_code == 413


def test_job_status_is_private_and_media_urls_are_redacted(client):
    job_id = mg.register_job({
        "kind": "video", "provider": "custom", "model": "test",
        "state": "succeeded",
        "status_url": "https://provider.test/status?token=secret",
        "media": {"url": "https://provider.test/video.mp4?token=secret"},
    })
    assert mg._JOBS[job_id]["owner"]
    other = client.application.test_client()
    other.get("/api/csrf-token")
    assert other.get(f"/api/media/jobs/{job_id}").status_code == 404
    public = client.get(f"/api/media/jobs/{job_id}")
    assert public.status_code == 200
    body = public.get_json()
    assert "status_url" not in body
    assert body["media"]["url"] == "https://provider.test/video.mp4"


def test_remote_completed_output_is_not_treated_as_local_media(client, monkeypatch):
    import routes.media as media_routes

    mg.ensure_media_dir()
    local_collision = Path(mg.MEDIA_DIR) / "clip.mp4"
    local_collision.write_bytes(b"local sentinel")
    job_id = mg.register_job({
        "provider": "custom", "state": "queued",
        "status_url": "https://provider.test/status?token=status-secret",
    })

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "status": "completed",
                "video_url": "https://provider.test/output/clip.mp4?token=media-secret",
            }

    def forbid_owner_registration(*_args, **_kwargs):
        raise AssertionError("remote URL must not be registered as a local file")

    monkeypatch.setattr(mg.requests, "get", lambda *_args, **_kwargs: FakeResponse())
    monkeypatch.setattr(media_routes, "_remember_owner", forbid_owner_registration)

    response = client.get(f"/api/media/jobs/{job_id}")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["media"]["url"] == "https://provider.test/output/clip.mp4"
    assert "path" not in payload["media"]
    assert local_collision.read_bytes() == b"local sentinel"
    assert "status_url" not in payload
    assert "status-secret" not in str(payload)
    assert "media-secret" not in str(payload)
