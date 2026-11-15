from app.routers import spectator as s


def test_native_counterfactual_passed_to_viewer_without_replacing_original(monkeypatch):
    monkeypatch.setattr(s, '_remap_speed_for_engine', lambda save: None)
    monkeypatch.setattr(s, '_player_id_to_name', lambda: {})
    monkeypatch.setattr(s, 'compute_income_native', lambda save: {
        '__tileYields__': {'5,22': {'production': 7, 'food': 2}},
        '__tileYieldsWithoutResource__': {'5,22': {'production': 6, 'food': 2}},
    })
    save = {'tileMap': {'tileList': [{'position': {'x': 5, 'y': 22},
                                    'baseTerrain': 'Plains', 'resource': 'Coal'}]}}
    tile = s._build_state(save, 'test')['tiles'][0]
    assert tile['resource'] == 'Coal'
    assert tile['realYield'] == {'production': 7, 'food': 2}
    assert tile['realYieldWithoutResource'] == {'production': 6, 'food': 2}
