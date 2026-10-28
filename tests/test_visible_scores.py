from app.routers.internal import _visible_scores


def _civ(name, met):
    return {"civName": name, "diplomacy": {m: {} for m in met}}


def test_score_hidden_until_all_alive_players_met():
    save = {"civilizations": [
        _civ("Rome", ["Maya", "Khmer"]),
        _civ("Maya", ["Rome"]),
        _civ("Khmer", ["Rome", "Maya"]),
        _civ("India", []),
    ]}
    roster = [
        {"nation": "Rome", "alive": True},
        {"nation": "Maya", "alive": True},
        {"nation": "Khmer", "alive": True},
        {"nation": "India", "alive": False},
    ]
    income = {n: {"ranking": {"Score": s}} for n, s in
              [("Rome", 100), ("Maya", 300), ("Khmer", 200), ("India", 50)]}
    res = _visible_scores(save, roster, income)
    # Maya не встретила Khmer → скрыта; India мертва и её встречать не нужно,
    # но сама India никого не встретила → скрыта. Скрытые — в порядке ходов.
    assert res == [
        {"nation": "Khmer", "score": 200},
        {"nation": "Rome", "score": 100},
        {"nation": "Maya", "score": None},
        {"nation": "India", "score": None},
    ]
