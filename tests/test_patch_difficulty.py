import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock
import pytest
from app.routers import games
from app.game.parser import decode_save

@pytest.mark.parametrize('host',[None,'https://uncivserver.xyz'])
@pytest.mark.parametrize('nested_difficulty',[None,'Prince'])
def test_patch_only_difficulty_and_preview(monkeypatch,host,nested_difficulty):
    save={'difficulty':'Immortal','turns':23,'currentPlayer':'Rome',
          'gameParameters':{'speed':'Solo-iron'},'civilizations':[{'civName':'Rome','gold':999}],
          'tileMap':{'tiles':[1,2]}}
    if nested_difficulty is not None:
        save['gameParameters']['difficulty'] = nested_difficulty
    original=deepcopy(save)
    monkeypatch.setattr(games,'get_save_dict',AsyncMock(return_value=save))
    store=AsyncMock();preview=AsyncMock()
    monkeypatch.setattr(games,'store_save',store)
    monkeypatch.setattr(games,'store_preview',preview)
    uuid='876bb5a6-4544-424b-b70c-d13ace74857e'
    result=asyncio.run(games.patch_difficulty(uuid,games.PatchDifficultyRequest(difficulty='Deity',host=host)))
    assert result['previous']=='Immortal'
    saved=decode_save(store.await_args.args[1]);pv=decode_save(preview.await_args.args[1])
    original['difficulty']='Deity'
    original['gameParameters']['difficulty']='Deity'
    assert saved==original
    assert pv['difficulty']=='Deity' and pv['turns']==23
    assert pv['gameParameters']==original['gameParameters']
    assert store.await_args.kwargs['host']==preview.await_args.kwargs['host']==host


def test_preview_failure_not_silent(monkeypatch):
    monkeypatch.setattr(games,'get_save_dict',AsyncMock(return_value={'difficulty':'King'}))
    monkeypatch.setattr(games,'store_save',AsyncMock())
    monkeypatch.setattr(games,'store_preview',AsyncMock(side_effect=RuntimeError('unavailable')))
    with pytest.raises(RuntimeError):
        asyncio.run(games.patch_difficulty('876bb5a6-4544-424b-b70c-d13ace74857e',games.PatchDifficultyRequest(difficulty='Deity')))


def test_turn_zero_preview_omits_default_fields(monkeypatch):
    # Actual initial Unciv saves omit turns=0, rather than serializing it.
    save = {'difficulty': 'Immortal', 'currentPlayer': 'Rome',
            'gameParameters': {}, 'civilizations': []}
    monkeypatch.setattr(games, 'get_save_dict', AsyncMock(return_value=save))
    monkeypatch.setattr(games, 'store_save', AsyncMock())
    preview = AsyncMock()
    monkeypatch.setattr(games, 'store_preview', preview)
    asyncio.run(games.patch_difficulty(
        '876bb5a6-4544-424b-b70c-d13ace74857e',
        games.PatchDifficultyRequest(difficulty='Immortal-iron', host='https://uncivserver.xyz')))
    result = decode_save(preview.await_args.args[1])
    assert 'turns' not in result
    assert all(value is not None for value in result.values())
    assert result['difficulty'] == 'Immortal-iron'
    assert result['gameParameters']['difficulty'] == 'Immortal-iron'
