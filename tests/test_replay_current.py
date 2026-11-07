import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from app.routers import spectator as s

UUID = '876bb5a6-4544-424b-b70c-d13ace74857e'
HOST = 'https://uncivserver.xyz'

@pytest.fixture
def game(tmp_path, monkeypatch):
    folder = tmp_path / 'backups' / 'test-game'
    folder.mkdir(parents=True)
    core = tmp_path / 'core'
    (core / folder.name).mkdir(parents=True)
    monkeypatch.setattr(s, '_CORE_SESSIONS_PATH', core / 'sessions_index.json')
    monkeypatch.setattr(s, '_resolve_uuid', lambda _: 'old-archive-uuid')
    monkeypatch.setattr(s, '_backup_folder', lambda _: folder)
    return folder, core / folder.name / 'game_params.json'

@pytest.mark.parametrize('host,sync,local,expected', [
    (HOST, False, False, HOST),
    (HOST, False, True, HOST),
    (HOST, True, True, None),
    (HOST, True, False, HOST),
    ('https://unciv.icanseeforever.com', True, False, HOST),
    ('https://unciv.icanseeforever.com', False, True, None),
])
def test_source_and_load(game, monkeypatch, host, sync, local, expected):
    folder, params = game
    params.write_text(json.dumps({'GAME_ID': UUID, 'HOST': host, 'SYNC': sync}))
    monkeypatch.setattr(s, '_has_live_save', lambda _: local)
    assert s._current_source(folder) == (UUID, expected)
    fetch = AsyncMock(return_value={'turns': 7})
    monkeypatch.setattr(s, 'get_save_dict', fetch)
    save, uuid, path = asyncio.run(s._load_current(folder))
    fetch.assert_awaited_once_with(UUID, host=expected)
    assert save['turns'] == 7 and uuid == UUID
    assert (path is None) == bool(expected)


def test_detail_external_and_access(game, monkeypatch):
    folder, params = game
    params.write_text(json.dumps({'GAME_ID': UUID, 'HOST': HOST}))
    monkeypatch.setattr(s, '_has_live_save', lambda _: False)
    monkeypatch.setattr(s, '_rotate_folder', lambda _: None)
    monkeypatch.setattr(s, '_is_spectatable', lambda _: False)
    allowed = Request({'type':'http','headers':[(b'x-allow-active',b'1')]})
    detail = asyncio.run(s.game_detail(folder.name, allowed, rotate=False))
    assert detail['hasCurrent'] is True
    assert detail['currentGameId'] == UUID
    denied = Request({'type':'http','headers':[]})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(s.game_detail(folder.name, denied, rotate=False))
    assert exc.value.status_code == 403


def test_missing_legacy(game, monkeypatch):
    folder, _ = game
    monkeypatch.setattr(s, '_resolve_uuid', lambda _: None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(s._load_current(folder))
    assert exc.value.status_code == 404


def test_current_state_uses_remote(game, monkeypatch):
    folder, params = game
    params.write_text(json.dumps({'GAME_ID': UUID, 'HOST': HOST}))
    fetch = AsyncMock(return_value={'turns': 7})
    monkeypatch.setattr(s, 'get_save_dict', fetch)
    monkeypatch.setattr(s, '_build_state', lambda save, uuid, **kw: {'turn':save['turns']})
    monkeypatch.setattr(s, '_civ_player_ids_from_backup', lambda _: {})
    request = Request({'type':'http','headers':[(b'x-allow-active',b'1')]})
    result = asyncio.run(s.game_state(folder.name, request, turn='current', snap=None, rfile=None))
    assert result == {'turn':7, 'savedAt':None}
    fetch.assert_awaited_once_with(UUID, host=HOST)


def test_remote_errors(game, monkeypatch):
    from httpx import Request as HttpRequest, Response, HTTPStatusError
    folder, params = game
    params.write_text(json.dumps({'GAME_ID': UUID, 'HOST': HOST}))
    req = HttpRequest('GET', HOST)
    for code, expected in [(404,404),(403,502),(500,502)]:
        fetch = AsyncMock(side_effect=HTTPStatusError('error',request=req,response=Response(code)))
        monkeypatch.setattr(s, 'get_save_dict', fetch)
        with pytest.raises(HTTPException) as exc:
            asyncio.run(s._load_current(folder))
        assert exc.value.status_code == expected
