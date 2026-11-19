"""Security regressions for page fetching and streamed search answers."""
import ipaddress
from typing import ClassVar

import routes.search as search_routes


class FakeResponse:
    def __init__(self, *, status_code=200, headers=None, body=b"<p>safe page</p>"):
        self.status_code = status_code
        self.headers = headers or {"content-type": "text/html"}
        self._body = body
        self.closed = False

    @property
    def is_redirect(self):
        return 300 <= self.status_code < 400

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_bytes(self):
        yield self._body

    def close(self):
        self.closed = True


class FakeClient:
    responses: ClassVar[list] = []
    requests: ClassVar[list] = []

    def __init__(self, *args, **kwargs):
        self.responses = type(self).responses

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def build_request(self, method, url):
        return method, url

    def send(self, request, **kwargs):
        type(self).requests.append(request)
        return self.responses.pop(0)


def _allow_public_dns(monkeypatch):
    monkeypatch.setattr(
        search_routes,
        "_resolved_public_addresses",
        lambda hostname: {ipaddress.ip_address("93.184.216.34")},
    )


def test_fetch_rejects_loopback_and_private_dns_before_http(monkeypatch):
    calls = []

    def fake_getaddrinfo(hostname, *args, **kwargs):
        calls.append(hostname)
        return [(0, 0, 0, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(search_routes.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(search_routes.httpx, "Client", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("HTTP client must not be opened for private DNS")
    ))

    text, error = search_routes.fetch_page_text("http://internal.example/page")

    assert text is None
    assert error == "некорректный URL"
    assert calls == ["internal.example"]


def test_fetch_revalidates_redirect_target_and_blocks_loopback(monkeypatch):
    _allow_public_dns(monkeypatch)
    FakeClient.responses = [FakeResponse(status_code=302, headers={"location": "http://127.0.0.1/admin"})]
    FakeClient.requests = []
    monkeypatch.setattr(search_routes.httpx, "Client", FakeClient)

    def reject_loopback(url):
        return not url.startswith("http://127.0.0.1")

    monkeypatch.setattr(search_routes, "_validate_public_http_url", reject_loopback)
    text, error = search_routes.fetch_page_text("https://public.example/start")

    assert text is None
    assert error == "запрещённый redirect"
    assert len(FakeClient.requests) == 1
    assert FakeClient.requests[0][1] == "https://public.example/start"


def test_fetch_enforces_response_size_limit(monkeypatch):
    _allow_public_dns(monkeypatch)
    FakeClient.responses = [FakeResponse(body=b"x" * (search_routes.READER_MAX_BYTES + 1))]
    FakeClient.requests = []
    monkeypatch.setattr(search_routes.httpx, "Client", FakeClient)

    text, error = search_routes.fetch_page_text("https://public.example/large")

    assert text is None
    assert error == "слишком большой ответ"


def test_stream_answer_does_not_emit_provider_reasoning(monkeypatch):
    def fake_chat_stream(*args, **kwargs):
        yield "reasoning", "private chain of thought"
        yield "token", "Публичный "
        yield "reasoning", "more private notes"
        yield "token", "ответ"
        yield "done", ""

    monkeypatch.setattr(search_routes, "chat_stream", fake_chat_stream)

    events = list(search_routes._stream_answer([], "system", "provider", "model", reasoning=True))

    assert events == [
        {"type": "token", "token": "Публичный "},
        {"type": "token", "token": "ответ"},
    ]
    assert all("reasoning" not in repr(event) for event in events)
