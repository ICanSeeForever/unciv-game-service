"""Дуэльный бан природных чудес (El Dorado / Fountain of Youth).

Проверяем, что на карте ровно для 2 игроков наличие запрещённого чуда даёт
issue (→ рестарт), а для >2 игроков и без чуда — нет.
"""
from app.services.map_checker import check_map, _DUEL_BANNED_WONDERS

_WONDER_ISSUE_MARK = "Запрещённые для дуэли природные чудеса"


def _civ(name: str, x: int, y: int) -> dict:
    return {
        "civName": name,
        "cities": [{
            "location": {"x": x, "y": y},
            "cityConstructions": {"builtBuildings": ["Palace"]},
        }],
    }


def _save(civ_positions: list[tuple[str, int, int]], wonder: str | None) -> dict:
    players = [{"playerType": "Human", "chosenCiv": n} for n, _, _ in civ_positions]
    civs = [_civ(n, x, y) for n, x, y in civ_positions]
    # Небольшая сетка тайлов, чтобы работали границы/дистанции.
    tiles = []
    for tx in range(-2, 14):
        for ty in range(-2, 6):
            tiles.append({"position": {"x": tx, "y": ty}, "baseTerrain": "Plains"})
    if wonder:
        tiles.append({"position": {"x": 5, "y": 5}, "baseTerrain": "Plains", "naturalWonder": wonder})
    return {
        "gameParameters": {"players": players},
        "civilizations": civs,
        "tileMap": {"tileList": tiles, "mapParameters": {"worldWrap": False}},
    }


def _has_wonder_issue(res) -> bool:
    return any(_WONDER_ISSUE_MARK in i for i in res.issues)


def test_duel_with_banned_wonder_flags():
    for wonder in _DUEL_BANNED_WONDERS:
        res = check_map(_save([("Rome", 0, 0), ("Egypt", 10, 0)], wonder))
        assert _has_wonder_issue(res), f"{wonder} должно рестартить дуэль: {res.issues}"
        assert wonder in res.details.get("duel_banned_wonders", [])


def test_duel_without_wonder_ok():
    res = check_map(_save([("Rome", 0, 0), ("Egypt", 10, 0)], None))
    assert not _has_wonder_issue(res)
    assert res.details.get("duel_banned_wonders") == []


def test_three_players_with_wonder_not_flagged():
    # Не дуэль (3 игрока) — чудо разрешено, issue не добавляется.
    res = check_map(_save([("Rome", 0, 0), ("Egypt", 10, 0), ("India", 5, 5)], "El Dorado"))
    assert not _has_wonder_issue(res)
