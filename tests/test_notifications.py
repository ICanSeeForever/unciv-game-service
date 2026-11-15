from copy import deepcopy
from app.routers.spectator import _notifications


def test_current_history_nation_isolation_and_defaults():
    save = {'civilizations': [
        {'civName': 'Rome', 'notifications': [{'text': '[Rome] has grown!'}],
         'notificationsLog': [{'turn': 2, 'notifications': [{'text': 'older'}]}, {'turn': 3, 'notifications': [{'text': 'newer'}]}]},
        {'civName': 'Poland', 'notifications': [{'text': 'private'}]}, {'civName': 'Spectator'}]}
    before = deepcopy(save)
    result = _notifications(save)
    assert result['Rome']['current'][0] == {'text': '[Rome] has grown!', 'category': 'General', 'icons': [], 'locations': []}
    assert result['Rome']['atStart'] == 1
    assert [h['turn'] for h in result['Rome']['history']] == [3, 2]
    assert result['Poland']['current'][0]['text'] == 'private'
    assert 'Spectator' not in result
    assert save == before


def test_only_safe_location_fields_and_no_action_payloads():
    n = {'text': 'Built', 'icons': ['BuildingIcons/Library'], 'category': 'Production', 'actions': [
        {'LocationAction': {'location': {'x': 1, 'y': -2}}}, {'CityAction': {'city': {'x': 1, 'y': -2}}},
        {'LocationAction': {'location': {'x': 3, 'y': 4}}}, {'TechAction': {'techName': 'Secret'}}]}
    r = _notifications({'civilizations': [{'civName': 'Rome', 'notificationCountAtStartTurn': 0, 'notifications': [n]}]})['Rome']
    assert r['atStart'] == 0
    assert r['current'][0]['locations'] == [{'x': 1, 'y': -2}, {'x': 3, 'y': 4}]
    assert 'actions' not in r['current'][0]
    assert r['current'][0]['icons'] == ['BuildingIcons/Library']


def test_old_empty_snapshot_does_not_retain_notifications():
    assert _notifications({'civilizations': [{'civName': 'Rome'}]})['Rome'] == {'current': [], 'atStart': 0, 'history': []}
    assert _notifications({}) == {}


def test_journal_exported_in_normalized_state(monkeypatch):
    from app.routers import spectator as s
    monkeypatch.setattr(s, '_remap_speed_for_engine', lambda save: None)
    monkeypatch.setattr(s, '_player_id_to_name', lambda: {})
    monkeypatch.setattr(s, 'compute_income_native', lambda save: {})
    save = {'civilizations': [{'civName': 'Rome', 'notifications': [{'text': 'Hello'}]}]}
    assert s._build_state(save, 'test')['notifications']['Rome']['current'][0]['text'] == 'Hello'
