import io
import zipfile


def test_workspace_zip_tree_preview_and_traversal(client, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    chat_id = client.post("/api/chats", json={"title": "Workspace"}).get_json()["chat_id"]
    from flask import session
    # create a file under the exact owner/session namespace used by the routes
    with client.session_transaction() as sess:
        owner = "admin:" + str(sess.get("admin_username") or "admin") if sess.get("admin_logged_in") else "anon:" + sess["nova_sid"]
    import identity
    from agent.workspace import workspace_root
    root = workspace_root(owner, chat_id)
    (root / "report.txt").write_text("Привет", encoding="utf-8")

    listing = client.get(f"/api/agent/workspace/tree?chat_id={chat_id}").get_json()
    assert listing["files"][0]["path"] == "report.txt"
    preview = client.get(f"/api/agent/workspace/file?chat_id={chat_id}&path=report.txt").get_json()
    assert preview["content"] == "Привет"
    assert client.get(f"/api/agent/workspace/file?chat_id={chat_id}&path=../secret").status_code == 400
    archive = zipfile.ZipFile(io.BytesIO(client.get(f"/api/agent/workspace/zip?chat_id={chat_id}").data))
    assert archive.namelist() == ["report.txt"]


def test_workspace_isolated_between_browsers(client, monkeypatch, tmp_path):
    from app import app
    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    first = app.test_client()
    second = app.test_client()
    first_chat = first.post("/api/chats", json={}).get_json()["chat_id"]
    first.get("/api/agent/workspace/tree?chat_id=" + first_chat)
    response = second.get("/api/agent/workspace/tree?chat_id=" + first_chat)
    assert response.status_code == 404


def test_manual_terminal_and_react_share_one_owned_workspace(client, monkeypatch, tmp_path):
    from app import app
    from tests.agent_helpers import script, ndjson, events_of
    monkeypatch.setenv('AGENT_WORKSPACE_ROOT', str(tmp_path / 'chats'))
    monkeypatch.setenv('TERMINAL_ENABLED', '1')
    with client.session_transaction() as sess:
        sess['admin_logged_in'] = True
        sess['admin_username'] = 'tester'
    chat = client.post('/api/chats', json={}).get_json()['chat_id']
    body = {'chat_id': chat, 'command': 'echo terminal-output > note.txt'}
    assert client.post('/api/terminal/run', json=body).get_json()['code'] == 0
    seen = []
    script(monkeypatch, ['{"tool":"shell","arguments":{"command":"cat note.txt"}}', 'В файле terminal-output.'], seen)
    events = ndjson(client.post('/api/agent/react', json={'chat_id': chat, 'message': 'Прочитай файл через терминал',
                                                         'tools': True, 'workspace': True}))
    assert events_of(events, 'tool')[0]['result']['stdout'].strip() == 'terminal-output'
    assert 'terminal-output' in seen[1]['messages'][-1]['content'] and events_of(events, 'result')[0]['linux']
    other = app.test_client()
    assert other.post('/api/terminal/run', json=body).status_code == 403
    with other.session_transaction() as sess:
        sess['nova_google_login'] = True
        sess['nova_user_email'] = 'other@example.org'
        sess['nova_user_nick'] = 'Other'
    assert other.post('/api/terminal/run', json=body).status_code == 404
    second = client.post('/api/chats', json={}).get_json()['chat_id']
    assert client.post('/api/terminal/run', json={'chat_id': second, 'command': 'cat note.txt'}).get_json()['code'] != 0
    monkeypatch.setenv('TERMINAL_ENABLED', '0')
    assert client.post('/api/terminal/run', json=body).status_code == 403
