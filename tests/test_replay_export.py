import asyncio
import io
import os
import tarfile
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from app.routers import spectator as s
from app.game.parser import encode_save

UUID = '876bb5a6-4544-424b-b70c-d13ace74857e'


def save(marker):
    return {'gameId': UUID, 'turns': 7, 'marker': marker, 'difficulty': 'Immortal',
            'gameParameters': {'speed': 'old-speed', 'isOnlineMultiplayer': True},
            'civilizations': [{'civName': 'Rome', 'playerType': 'Human'},
                              {'civName': 'Greece', 'playerType': 'Human'},
                              {'civName': 'Spectator', 'playerType': 'Human'},
                              {'civName': 'Geneva'}]}


def archive(path, marker, timestamp):
    raw = encode_save(save(marker)).encode()
    with tarfile.open(path, 'w:gz') as tar:
        member = tarfile.TarInfo(UUID)
        member.size = len(raw)
        tar.addfile(member, io.BytesIO(raw))
    os.utime(path, (timestamp, timestamp))


@pytest.fixture
def sources(tmp_path, monkeypatch):
    folder = tmp_path / 'g'
    folder.mkdir()
    rotation = tmp_path / 'rotation'
    rotation.mkdir()
    archive(folder / '7_Rome_first.tar.gz', 'before-restart', 10)
    archive(folder / '7_Rome_second.tar.gz', 'after-restart', 20)
    archive(rotation / '7_Greece_rotation.tar.gz', 'rotation', 30)
    monkeypatch.setattr(s, '_backup_folder', lambda _: folder)
    monkeypatch.setattr(s, '_rotate_folder', lambda _: rotation)
    return folder, rotation


@pytest.mark.parametrize('selector,marker', [
    ({'turn': '7', 'snap': 0}, 'before-restart'),
    ({'turn': '7', 'snap': 1}, 'after-restart'),
    ({'turn': '7', 'snap': 0, 'rfile': '7_Greece_rotation.tar.gz'}, 'rotation'),
])
def test_exact_snapshot_and_read_only(sources, selector, marker):
    paths = [p for f in sources for p in f.iterdir()]
    originals = {p: p.read_bytes() for p in paths}
    source = s.ReplayExportSource(name='g', **selector)
    result = asyncio.run(s.replay_export('save', source))
    assert result['save']['marker'] == marker
    assert result['save']['gameParameters']['speed'] == 'old-speed'  # no viewer remap
    assert result['filename'] == UUID
    civs = asyncio.run(s.replay_export('civs', source))
    assert civs == {'civs': ['Rome', 'Greece', 'Spectator']}
    source.nations = ['Greece', 'Spectator']
    single = asyncio.run(s.replay_export('single', source))['save']
    assert single['marker'] == marker
    assert single['gameParameters']['isOnlineMultiplayer'] is False
    assert single['difficulty'] == single['gameParameters']['difficulty'] == 'King'
    assert [c['civName'] for c in single['civilizations'] if c.get('playerType') == 'Human'] == ['Greece', 'Spectator']
    assert originals == {p: p.read_bytes() for p in paths}


def test_current_uses_same_host_resolution(sources, monkeypatch):
    load = AsyncMock(return_value=(save('current'), UUID, None))
    monkeypatch.setattr(s, '_load_current', load)
    result = asyncio.run(s.replay_export('save', s.ReplayExportSource(name='g')))
    assert result['save']['marker'] == 'current'
    load.assert_awaited_once_with(sources[0])


def test_id_external_fallback(monkeypatch):
    fetch = AsyncMock(side_effect=[FileNotFoundError(), save('external')])
    monkeypatch.setattr(s, 'get_save_dict', fetch)
    monkeypatch.setattr(s.settings, 'uncivserver_url', 'https://uncivserver.xyz')
    result = asyncio.run(s.replay_export('save', s.ReplayExportSource(game_id=UUID)))
    assert result['save']['marker'] == 'external'
    assert fetch.await_args_list[1].kwargs == {'host': 'https://uncivserver.xyz'}


@pytest.mark.parametrize('kwargs,kind,code', [
    ({'name': 'g', 'turn': '7', 'snap': 99}, 'save', 404),
    ({'name': 'g', 'rfile': '../wrong.tar.gz'}, 'save', 400),
    ({'name': 'g', 'rfile': 'missing.tar.gz'}, 'save', 404),
    ({'name': 'g', 'turn': '7', 'snap': 0}, 'single', 400),
    ({'name': 'g', 'turn': '7', 'snap': 0, 'nations': ['Unknown']}, 'single', 422),
    ({'name': 'g', 'game_id': UUID}, 'save', 400),
    ({'game_id': '../secret'}, 'save', 400),
])
def test_invalid_source_or_selection(sources, kwargs, kind, code):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(s.replay_export(kind, s.ReplayExportSource(**kwargs)))
    assert exc.value.status_code == code


def test_export_requires_internal_api_key(monkeypatch):
    import httpx
    from app.main import app
    monkeypatch.setattr(s.settings, 'api_key', 'test-key')
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://test') as client:
            return await client.post('/internal/replay-export/save', json={'name': 'g'})
    assert asyncio.run(request()).status_code == 401
