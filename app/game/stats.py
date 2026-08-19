"""Per-civ income / happiness engine, ported from Unciv's CityStats.

Computes the live per-turn figures Unciv shows in the world-screen top bar
(science / gold / culture / faith income and net happiness) which the save does
NOT store. Runs on the backend so it can use the full save (trades, occupied
flags) instead of the lossy spectator snapshot the frontend used to compute on.

Inputs are the already-normalized `cities` + `tiles` from spectator._build_state
plus the raw civ objects (for adopted policies / difficulty). Ruleset JSONs are
bundled under app/game/ruleset (RekMOD).
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

_RULESET_DIR = Path(__file__).parent / "ruleset"
STATS = ["food", "production", "gold", "science", "culture", "faith", "happiness"]
_STAT_NAME = {
    "Gold": "gold", "Science": "science", "Culture": "culture", "Faith": "faith",
    "Food": "food", "Production": "production", "Happiness": "happiness",
}
_STAT_RE = "Gold|Science|Culture|Faith|Food|Production|Happiness"
_APPLY_SCOPES = {
    "in all cities", "in this city",
    "in cities following this religion", "in all cities following this religion",
}


def _load(name: str):
    txt = (_RULESET_DIR / name).read_text(encoding="utf-8")
    txt = re.sub(r"/\*.*?\*/", " ", txt, flags=re.S)
    txt = re.sub(r"//.*", " ", txt)
    txt = re.sub(r",(\s*[}\]])", r"\1", txt)
    return json.loads(txt)


@lru_cache(maxsize=1)
def _ruleset() -> dict:
    buildings = {b["name"]: b for b in _load("Buildings.json")}
    resources = {r["name"]: r for r in _load("TileResources.json")}
    improvements = {t["name"]: t for t in _load("TileImprovements.json")} if (_RULESET_DIR / "TileImprovements.json").exists() else {}
    terrains = {t["name"]: t for t in _load("Terrains.json")}
    specialists = {s["name"]: s for s in _load("Specialists.json")}
    policies = _load("Policies.json")
    beliefs = {b["name"]: b for b in _load("Beliefs.json")}
    return {
        "B": buildings, "RES": resources, "IMP": improvements, "TER": terrains,
        "SPC": specialists, "POL": policies, "BEL": beliefs,
    }


def _stat_of(obj: dict) -> dict:
    return {k: obj[k] for k in STATS if k in obj}


def _tile_yield(t: dict, rs: dict) -> dict:
    y = {k: 0 for k in STATS}
    srcs = [rs["TER"].get(t.get("baseTerrain"), {})]
    srcs += [rs["TER"].get(f, {}) for f in (t.get("terrainFeatures") or [])]
    if t.get("resource") and t["resource"] in rs["RES"]:
        srcs.append(rs["RES"][t["resource"]])
    imp = t.get("improvement")
    if imp and imp in rs["IMP"]:
        srcs.append(rs["IMP"][imp])
    for s in srcs:
        for k, v in _stat_of(s).items():
            y[k] += v
    return y


def _policy_uniques(adopted: set, rs: dict) -> list:
    out: list[str] = []
    for br in rs["POL"]:
        if br["name"] in adopted:
            out += br.get("uniques", [])
        for p in br.get("policies", []):
            if p["name"] in adopted:
                out += p.get("uniques", [])
    return out


def _religion_uniques(city: dict, religions_by_name: dict, rs: dict) -> list:
    rel = religions_by_name.get(city.get("religion"))
    if not rel:
        return []
    out: list[str] = []
    for b in rel.get("followerBeliefs", []):
        out += rs["BEL"].get(b, {}).get("uniques", [])
    return out


def _add_multi(bracket: str, cnt: int, flat: dict) -> None:
    for m in re.finditer(rf"([+-]?\d+) ({_STAT_RE})", bracket):
        st = _STAT_NAME.get(m.group(2))
        if st:
            flat[st] += int(m.group(1)) * cnt


def _apply_unique(u: str, flat: dict, pct: dict, ctx: dict, rs: dict) -> None:
    base = re.sub(r"\s*<[^>]*>", "", u)
    for c in re.findall(r"<([^>]*)>", u):
        cl = c.lower()
        if "cities with a" in cl:
            m = re.search(r"\[([^\]]+)\]", c)
            if m and m.group(1) not in ctx["blds"]:
                return
        else:
            return  # unknown condition -> skip
    # percent per object
    m = re.match(r"^\[([+-]?\d+)\]% \[(\w+)\] from every \[([^\]]+)\]", base)
    if m:
        st = _STAT_NAME.get(m.group(2))
        if st:
            f = m.group(3)
            cnt = ctx["blds"].count(f) + sum(1 for t in ctx["worked"] if t.get("improvement") == f)
            pct[st] += int(m.group(1)) * cnt
        return
    # bare percent, optionally with a plain scope
    m = re.match(r"^\[([+-]?\d+)\]% \[(\w+)\](?: \[([^\]]+)\])?$", base)
    if m and (m.group(3) is None or m.group(3) in _APPLY_SCOPES):
        st = _STAT_NAME.get(m.group(2))
        if st:
            pct[st] += int(m.group(1))
        return
    # multi-stat flat with scope
    m = re.match(r"^\[[^\]]*\] \[([^\]]+)\]$", base)
    if m and m.group(1) in _APPLY_SCOPES:
        _add_multi(base[: base.index("]") + 1], 1, flat)
        return
    # per population
    m = re.match(rf"^\[\+?(\d+) ({_STAT_RE})\] per \[(\d+)\] population", base)
    if m:
        st = _STAT_NAME.get(m.group(2))
        if st:
            flat[st] += (ctx["pop"] // int(m.group(3))) * int(m.group(1))
        return
    # from [Strategic/Luxury/Bonus] resource tiles
    m = re.match(r"^\[([^\]]*)\] from \[(Strategic|Luxury|Bonus) resource\] tiles", base)
    if m:
        cnt = sum(1 for t in ctx["worked"]
                  if t.get("resource") and rs["RES"].get(t["resource"], {}).get("resourceType") == m.group(2))
        _add_multi(m.group(1), cnt, flat)
        return
    # from [Terrain] tiles
    m = re.match(r"^\[([^\]]*)\] from \[([^\]]+)\] tiles", base)
    if m:
        terr = m.group(2)
        cnt = sum(1 for t in ctx["worked"]
                  if t.get("baseTerrain") == terr or terr in (t.get("terrainFeatures") or []))
        _add_multi(m.group(1), cnt, flat)
        return
    # from every [Building/Improvement/Specialist]
    m = re.match(r"^\[([^\]]*)\] from every \[([^\]]+)\]", base)
    if m:
        f = m.group(2)
        if f == "Specialist":
            cnt = ctx["spec"]
        else:
            cnt = ctx["blds"].count(f) + sum(1 for t in ctx["worked"] if t.get("improvement") == f)
        _add_multi(m.group(1), cnt, flat)


def _city_stats(city: dict, tiles_by_pos: dict, policy_uniques: list, extra_uniques: list, rs: dict) -> dict:
    flat = {k: 0 for k in STATS}
    pct = {k: 0 for k in STATS}
    pop = city.get("population") or 1
    blds = city.get("buildings") or []
    worked = [tiles_by_pos[(w["x"], w["y"])] for w in (city.get("workedTiles") or []) if (w["x"], w["y"]) in tiles_by_pos]
    ctx = {"pop": pop, "blds": blds, "worked": worked,
           "spec": sum((city.get("specialists") or {}).values())}
    flat["science"] += pop  # Civ V: 1 science per citizen (hardcoded in Unciv)
    cc = tiles_by_pos.get((city["x"], city["y"]))
    if cc:
        for k, v in _tile_yield(cc, rs).items():
            flat[k] += v
        flat["food"] = max(flat["food"], 1)
        flat["production"] = max(flat["production"], 1)
    for t in worked:
        for k, v in _tile_yield(t, rs).items():
            flat[k] += v
    for sp, cnt in (city.get("specialists") or {}).items():
        for k, v in _stat_of(rs["SPC"].get(sp, {})).items():
            flat[k] += v * cnt
    for b in blds:
        bb = rs["B"].get(b, {})
        for k, v in _stat_of(bb).items():
            flat[k] += v
        for k, v in _stat_of(bb.get("percentStatBonus") or {}).items():
            pct[k] += v
        for u in bb.get("uniques", []):
            _apply_unique(u, flat, pct, ctx, rs)
    for u in extra_uniques:
        _apply_unique(u, flat, pct, ctx, rs)
    for u in policy_uniques:
        _apply_unique(u, flat, pct, ctx, rs)
    out = {k: int(flat[k] * (1 + pct[k] / 100)) for k in STATS}
    # Production-conversion projects (Unciv PerpetualConstruction, 1:1).
    proj = (city.get("construction") or {}).get("name")
    if proj == "Science":
        out["science"] += out["production"]
    elif proj == "Gold":
        out["gold"] += out["production"]
    return out


# Difficulty happiness constants (AI civs on Deity bot games): the "AI" ruleset
# difficulty (base 15, +0/luxury, unhappinessModifier 0.9) times Deity's
# aiUnhappinessModifier 0.75 -> 0.675, per Unciv CityStats.
_HAPP_BASE = 15
_HAPP_PER_LUXURY = 4
_UNHAPP_MOD = 0.9 * 0.75


def compute_income(cities: list, tiles: list, civ_stats: dict, religions: list) -> dict:
    """Return {civName: {gold, science, culture, faith, happiness}} per-turn."""
    rs = _ruleset()
    tiles_by_pos = {(t["x"], t["y"]): t for t in tiles}
    religions_by_name = {r["name"]: r for r in (religions or [])}
    out: dict[str, dict] = {}
    # luxury count (owned + improved tiles); TODO: add traded luxuries from diplomacy
    lux_by_civ: dict[str, set] = {}
    for t in tiles:
        civ = t.get("owningCiv")
        if civ and t.get("resource") and t.get("improvement") \
                and rs["RES"].get(t["resource"], {}).get("resourceType") == "Luxury":
            lux_by_civ.setdefault(civ, set()).add(t["resource"])

    for name, cs in (civ_stats or {}).items():
        adopted = set(cs.get("adoptedPolicies") or [])
        pol = _policy_uniques(adopted, rs)
        totals = {k: 0 for k in STATS}
        maintenance = 0
        happiness = _HAPP_BASE + len(lux_by_civ.get(name, set())) * _HAPP_PER_LUXURY
        for city in cities:
            if city.get("owner") != name:
                continue
            puppet = bool(city.get("isPuppet"))
            rel = _religion_uniques(city, religions_by_name, rs)
            st = _city_stats(city, tiles_by_pos, [] if puppet else pol, rel, rs)
            for k in STATS:
                totals[k] += st[k]
            for b in (city.get("buildings") or []):
                maintenance += rs["B"].get(b, {}).get("maintenance", 0) or 0
            happiness += st["happiness"]
            pop = city.get("population") or 1
            pop_pct = 0
            reducers = [u for b in (city.get("buildings") or []) for u in rs["B"].get(b, {}).get("uniques", [])]
            reducers += [] if puppet else pol
            for u in reducers:
                m = re.match(r"^\[([+-]?\d+)\]% Unhappiness from \[Population\](?: \[([^\]]+)\])?", u)
                if not m:
                    continue
                scope = m.group(2) or "in all cities"
                if "non-occupied" in scope and puppet:
                    continue
                pop_pct += int(m.group(1))
            unhappiness = max(0, pop * (1 + pop_pct / 100))
            happiness -= (3 + unhappiness) * _UNHAPP_MOD
        out[name] = {
            "gold": totals["gold"] - maintenance,
            "science": totals["science"],
            "culture": totals["culture"],
            "faith": totals["faith"],
            "happiness": round(happiness),
        }
    return out
