from copy import deepcopy
from app.routers.spectator import _active_trades, _build_state
from app.routers import spectator as s


def test_accepted_terms_keep_perspective_defaults_and_immediate_payment():
    trade = {'ourOffers': [{'name': 'Gold per turn', 'type': 'Gold_Per_Turn', 'duration': 8, 'amount': 2}],
             'theirOffers': [{'name': 'Gold', 'amount': 40}]}
    save = {'civilizations': [
        {'civName': 'Rome', 'diplomacy': {'Greece': {'trades': [trade]}}},
        {'civName': 'Greece', 'diplomacy': {'Rome': {'trades': [
            {'ourOffers': trade['theirOffers'], 'theirOffers': trade['ourOffers']}]}}}]}
    before = deepcopy(save)
    result = _active_trades(save)
    assert result['Rome'][0]['theirOffers'] == [{'name': 'Gold', 'type': 'Gold', 'amount': 40, 'duration': -1}]
    assert result['Rome'][0]['ourOffers'] == result['Greece'][0]['theirOffers']
    assert result['Rome'][0]['otherCiv'] == 'Greece'
    assert save == before


def test_requests_expired_and_immediate_only_not_active():
    save = {'civilizations': [{'civName': 'Rome', 'tradeRequests': [{'trade': {}}], 'diplomacy': {
        'Greece': {'trades': [{'ourOffers': [{'duration': 0}]}, {'theirOffers': [{'name': 'Gold'}]}]}}}]}
    assert _active_trades(save) == {'Rome': []}


def test_resources_one_sided_and_city_states():
    save = {'civilizations': [{'civName': 'Rome', 'diplomacy': {
        'Gaborone': {'trades': [{'theirOffers': [{'name': 'Iron', 'type': 'Strategic_Resource', 'duration': 3}]}]}}}]}
    trade = _active_trades(save)['Rome'][0]
    assert trade['ourOffers'] == []
    assert trade['theirOffers'][0]['amount'] == 1
    assert trade['otherCiv'] == 'Gaborone'


def test_snapshot_changes_do_not_retain_old_trades():
    old = {'civilizations': [{'civName': 'Rome', 'diplomacy': {'Greece': {'trades': [
        {'ourOffers': [{'duration': 1}]}]}}}]}
    assert len(_active_trades(old)['Rome']) == 1
    new = deepcopy(old)
    new['civilizations'][0]['diplomacy']['Greece']['trades'][0]['ourOffers'][0]['duration'] = 0
    assert _active_trades(new)['Rome'] == []
    assert _active_trades({}) == {}


def test_state_exports_trades(monkeypatch):
    monkeypatch.setattr(s, '_remap_speed_for_engine', lambda save: None)
    monkeypatch.setattr(s, '_player_id_to_name', lambda: {})
    monkeypatch.setattr(s, 'compute_income_native', lambda save: {})
    save = {'civilizations': [{'civName': 'Rome', 'diplomacy': {'Greece': {'trades': [
        {'ourOffers': [{'name': 'Iron', 'type': 'Strategic_Resource', 'duration': 4}]}]}}}]}
    assert _build_state(save, 'test')['trades'] == _active_trades(save)


def test_pending_requests_are_reversed_for_sender_and_not_duplicated():
    from app.routers.spectator import _pending_trades
    request = {'requestingCiv': 'Mysore', 'trade': {
        'ourOffers': [{'name': 'Cotton', 'type': 'Luxury_Resource', 'duration': 25}],
        'theirOffers': [{'name': 'Dyes', 'type': 'Luxury_Resource', 'duration': 25}]}}
    save = {'civilizations': [{'civName': 'Mysore'}, {'civName': 'Poland', 'tradeRequests': [request]}]}
    before = deepcopy(save)
    result = _pending_trades(save)
    assert result['Poland'] == []
    assert result['Mysore'][0]['otherCiv'] == 'Poland'
    assert result['Mysore'][0]['ourOffers'][0]['name'] == 'Dyes'
    assert result['Mysore'][0]['theirOffers'][0]['name'] == 'Cotton'
    assert _active_trades(save)['Mysore'] == []
    assert save == before


def test_pending_immediate_only_requests_and_civ_ids():
    from app.routers.spectator import _pending_trades
    save = {'civilizations': [{'civName': 'Rome', 'civID': 'rome-id'}, {
        'civName': 'Poland', 'tradeRequests': [{'requestingCiv': 'rome-id', 'trade': {
            'theirOffers': [{'name': 'Gold', 'amount': 40}]}}]}]}
    assert _pending_trades(save)['Rome'][0]['ourOffers'][0]['amount'] == 40
    save['civilizations'][1]['tradeRequests'] = []
    assert _pending_trades(save)['Rome'] == []
