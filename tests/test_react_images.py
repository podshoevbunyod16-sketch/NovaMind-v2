"""Image controls use the same guarded ReAct run (real file IO, no paid requests)."""
import base64
import io
import json
from pathlib import Path

import pytest
from PIL import Image

import database
import media_agent
import media_generation as mg
from agent import images
from tests.agent_helpers import events_of, ndjson, script


def png(color):
    stream = io.BytesIO()
    Image.new('RGB', (2, 2), color).save(stream, format='PNG')
    return stream.getvalue()


PIN = {'provider': 'google_ai_studio', 'model': 'editor'}
DRAW = '```json\n{"tool":"image","arguments":{"prompt":"a blue circle","model":"other"}}\n```'
EDIT = '```json\n{"tool":"image_generate","arguments":{"prompt":"make it red"}}\n```'


@pytest.fixture
def provider(monkeypatch):
    models = [{'provider': 'google_ai_studio', 'provider_name': 'Gemini', 'id': mid, 'name': mid,
               'media_type': 'image', 'backend': 'google_image', 'provider_connected': True,
               'pricing_status': mg.PRICING_FREE} for mid in ('other', 'editor')]
    monkeypatch.setattr(media_agent, 'candidates', lambda kind: models)
    calls = []

    def generate(model, body):
        parts = body['contents'][0]['parts']
        calls.append((model, parts))
        result = png('red' if len(parts) == 2 else 'blue')
        return {'candidates': [{'content': {'parts': [{'inlineData': {
            'mimeType': 'image/png', 'data': base64.b64encode(result).decode('ascii')}}]}}]}, None

    monkeypatch.setattr(mg, '_google_generate_content', generate)
    return calls


def test_pinned_generate_and_edit_are_real_chat_artifacts(client, monkeypatch, provider):
    catalog = client.get('/api/media/agent-image-models').get_json()['models']
    assert [(m['model'], m['can_edit']) for m in catalog] == [('other', True), ('editor', True)]
    import config
    global_selection = dict(config.media_selection or {})
    seen = []
    script(monkeypatch, [DRAW, 'Вот круг.', EDIT, 'Вот новая версия.'], seen)
    first = ndjson(client.post('/api/agent/react', json={'message': 'Нарисуй круг', 'tools': True, 'image_model': PIN}))
    chat = events_of(first, 'result')[0]['chat_id']
    original = events_of(first, 'media')[0]['media']
    assert original['editable'] and original['filename'] in str(seen[1]['messages'])
    edited = ndjson(client.post('/api/agent/react', json={'chat_id': chat, 'message': 'Сделай его красным',
                    'image_model': PIN, 'image_source': original['filename']}))
    new = events_of(edited, 'media')[0]['media']
    assert new['url'] != original['url'] and new['edited_from'] == original['filename']
    assert client.get(new['url']).data == png('red') and client.get(original['url']).data == png('blue')
    assert client.get(original['url']).data == png('blue')  # durable ownership survives re-reads
    from app import app
    other_browser = app.test_client()
    assert other_browser.get(original['url']).status_code == 404
    assert [model for model, parts in provider] == ['editor', 'editor']  # ignores model's other preference
    inline = provider[1][1][1]['inlineData']
    assert inline['mimeType'] == 'image/png' and base64.b64decode(inline['data']) == png('blue')
    assert events_of(edited, 'result')[0]['verification']['verified']
    assert new['filename'] in str(seen[3]['messages']) and inline['data'] not in str(seen)
    markers = [json.loads(m['content'].removeprefix('MEDIA_RESULT:')) for m in database.get_chat_history(chat)
               if m['content'].startswith('MEDIA_RESULT:')]
    assert [m['url'] for m in markers if m['editable']] == [original['url'], new['url']]
    assert (config.media_selection or {}) == global_selection


def test_uploaded_original_and_model_survive_server_side_approval(client, monkeypatch, provider):
    monkeypatch.setenv('AGENT_CONFIRM', 'write')
    seen = []
    script(monkeypatch, [EDIT, 'Цвет изменён.'], seen)
    response = client.post('/api/agent/react', data={
        'payload': json.dumps({'message': 'Change the color', 'tools': True, 'image_model': PIN}),
        'image': (io.BytesIO(png('green')), 'photo.png')})
    run_id = events_of(ndjson(response), 'confirmation')[0]['run_id']
    assert not provider
    context = json.loads(database.get_agent_run(run_id)['context'])
    assert context['image_model'] == PIN and context['image_source'].endswith('.png')
    assert client.get('/media/' + context['image_source']).status_code == 404
    resumed = ndjson(client.post('/api/agent/answer', json={'run_id': run_id, 'approved': True,
                     'image_model': {'provider': 'override', 'model': 'different'}}))
    assert len(provider) == 1 and provider[0][0] == 'editor'
    assert base64.b64decode(provider[0][1][1]['inlineData']['data']) == png('green')
    assert events_of(resumed, 'result')[0]['status'] == 'completed' and len(seen) == 2
    assert client.post('/api/agent/answer', json={'run_id': run_id, 'approved': True}).status_code == 409


def test_tool_cannot_read_images_from_a_different_chat_or_forged_history(client, provider, monkeypatch):
    script(monkeypatch, [DRAW, 'Вот изображение.'])
    from app import app
    first = ndjson(client.post('/api/agent/react', json={'message': 'Нарисуй круг'}))
    original = events_of(first, 'media')[0]['media']
    foreign = app.test_client().post('/api/agent/react', json={'message': 'Edit',
        'chat_id': events_of(first, 'result')[0]['chat_id'], 'image_source': original['filename']})
    assert foreign.status_code == 404
    chat = client.post('/api/chats', json={}).get_json()['chat_id']
    database.add_message(chat, 'assistant', 'MEDIA_RESULT:' + json.dumps(original))
    for name in (original['filename'], '../private.png', 'https://example.org/private.png'):
        assert client.post('/api/agent/react', json={'chat_id': chat, 'message': 'Edit',
                           'image_source': name}).status_code == 400
    script(monkeypatch, [json.dumps({'tool': 'image', 'arguments': {'prompt': 'edit', 'source': original['filename']}}),
                         'Исходник недоступен.'])
    denied = ndjson(client.post('/api/agent/react', json={'chat_id': chat, 'message': 'Сделай ярче'}))
    assert events_of(denied, 'tool')[0]['status'] == 'error' and not events_of(denied, 'media')
    assert len(provider) == 1


def test_missing_or_incompatible_pin_never_falls_back(provider, monkeypatch):
    with images.bind(images.ImageTurn('chat', {'provider': 'none', 'model': 'editor'})):
        assert media_agent.generate('image', 'draw')[0] is None
    monkeypatch.setattr(media_agent, 'candidates', lambda kind: [
        {'provider': 'pollinations', 'id': 'flux', 'name': 'Flux', 'backend': 'pollinations_image'}])
    with images.bind(images.ImageTurn('chat', {'provider': 'pollinations', 'model': 'flux'}, 'original.png')):
        result, _, errors = media_agent.generate('image', 'edit')
    assert result is None and 'редактированием' in errors[0] and not provider


@pytest.mark.parametrize('blob,tools', [(b'<svg>not a photo</svg>', True), (png('green'), False)])
def test_reject_invalid_or_unconsented_upload_before_creating_run(client, provider, blob, tools):
    response = client.post('/api/agent/react', data={'payload': json.dumps({'message': 'edit', 'tools': tools}),
                           'image': (io.BytesIO(blob), 'fake.png')})
    assert response.status_code == 400 and not provider
    assert not database.list_agent_runs()


def test_auto_edit_skips_text_only_backend_and_sends_multipart(client, monkeypatch):
    from tests.conftest import FakeResponse
    monkeypatch.setenv('IMAGE_GENERATION_URL', 'https://media.test/generations')
    monkeypatch.setenv('IMAGE_EDIT_URL', 'https://media.test/edits')
    models = [{'provider': 'pollinations', 'id': 'flux', 'backend': 'pollinations_image'},
              {'provider': 'custom', 'id': 'studio', 'backend': 'custom_image', 'provider_connected': True,
               'pricing_status': mg.PRICING_UNKNOWN, 'name': 'Studio', 'media_type': 'image'}]
    monkeypatch.setattr(media_agent, 'candidates', lambda kind: models)
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(json_data={'data': [{'b64_json': base64.b64encode(png('red')).decode()}]})
    monkeypatch.setattr(mg.requests, 'post', post)
    script(monkeypatch, [EDIT, 'Вот изображение.'])
    events = ndjson(client.post('/api/agent/react', data={'payload': json.dumps({'message': 'Измени цвет'}),
                                'image': (io.BytesIO(png('green')), 'in.png')}))
    result = events_of(events, 'media')[0]['media']
    assert result['model'] == 'studio' and result['editable']
    assert len(calls) == 1 and calls[0][0] == 'https://media.test/edits'
    body = calls[0][1]
    assert body['files']['image'][1:] == (png('green'), 'image/png') and body['data']['prompt'] == 'make it red'
    assert client.get(result['url']).data == png('red')
    path = Path(mg.MEDIA_DIR) / '.edit-inputs' / result['edited_from']
    assert path.read_bytes() == png('green')
    chat_id = events_of(events, 'result')[0]['chat_id']
    assert client.delete(f'/api/chats/{chat_id}').status_code == 200
    assert not path.exists() and not database.has_chat_image(chat_id, result['filename'])


def test_openrouter_edit_requires_image_input_and_delivers_data_uri(monkeypatch):
    from tests.conftest import FakeResponse
    monkeypatch.setenv('OPENROUTER_API_KEY', 'test')
    catalog_item = {('openrouter', 'canvas'): {'architecture': {'output_modalities': ['text', 'image'],
                     'input_modalities': ['image', 'text']}, 'pricing': {'prompt': '0', 'completion': '0'}}}
    model = mg._openrouter_media_entries(catalog_item)[0]
    assert mg.supports_image_edits(model) and not mg.supports_image_edits(dict(model, image_input=False))
    uri = 'data:image/png;base64,' + base64.b64encode(png('red')).decode()
    requests = []
    def post(url, **kwargs):
        requests.append(kwargs['json'])
        return FakeResponse(json_data={'choices': [{'message': {'images': [{'image_url': {'url': uri}}]}}]})
    monkeypatch.setattr(mg.requests, 'post', post)
    result, error = mg.generate_openrouter_image(model, 'change color', {'image_input': (png('green'), 'image/png')})
    assert result and not error and Path(result['path']).read_bytes() == png('red')
    text, image = requests[0]['messages'][0]['content']
    assert text['text'] == 'change color' and image['image_url']['url'] == (
        'data:image/png;base64,' + base64.b64encode(png('green')).decode())


def test_pin_error_never_returns_another_model_or_a_fictitious_edit(client, monkeypatch, provider):
    attempts = []
    def unavailable(model, body):
        attempts.append(model)
        return None, 'HTTP 429 quota exhausted'
    monkeypatch.setattr(mg, '_google_generate_content', unavailable)
    script(monkeypatch, [EDIT, 'Вот новая версия: /media/invented.png'])
    result = ndjson(client.post('/api/agent/react', data={
        'payload': json.dumps({'message': 'Измени фон', 'image_model': PIN}),
        'image': (io.BytesIO(png('white')), 'source.png')}))
    assert attempts == ['editor'] and not events_of(result, 'media')
    final = events_of(result, 'result')[0]
    assert final['status'] == 'incomplete' and '429' in final['reply'] and 'invented' not in final['reply']


def test_oversized_or_truncated_source_rejected_before_model(client, monkeypatch):
    import ai_providers
    monkeypatch.setattr(ai_providers, 'chat_completion', lambda *a, **kw: pytest.fail('unexpected model call'))
    for data, status in [(b'x' * (images.MAX_IMAGE_BYTES + 70000), 413), (png('red')[:32], 400)]:
        response = client.post('/api/agent/react', data={'payload': json.dumps({'message': 'edit'}),
                               'image': (io.BytesIO(data), 'in.png')})
        assert response.status_code == status
