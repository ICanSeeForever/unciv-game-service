"""Public game static data: city-states, marine civs, luxury resources."""
import json
from functools import lru_cache
from pathlib import Path

from app.config import settings

# BNW + RekMOD luxury names — from public Unciv game data
LUXURY_RESOURCES: frozenset[str] = frozenset({
    "Amber", "Citrus", "Cloves", "Cocoa", "Coconut", "Coffee", "Copper",
    "Coral", "Cotton", "Crab", "Dyes", "Furs", "Gems", "Glass", "Gold Ore",
    "Incense", "Ivory", "Jade", "Jewelry", "Lapis Lazuli", "Marble",
    "Nutmeg", "Obsidian", "Olives", "Pearls", "Pepper", "Perfume",
    "Porcelain", "Rubber", "Salt", "Silk", "Silver", "Spices", "Sugar",
    "Tea", "Tobacco", "Truffles", "Whales", "Wine",
})


@lru_cache(maxsize=1)
def _load() -> dict:
    path = Path(settings.static_data_path)
    if not path.is_file():
        return {"cs": [], "marine": []}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def get_city_states() -> list[str]:
    return _load().get("cs", [])


def get_marine_civs() -> list[str]:
    return _load().get("marine", [])


def get_luxuries() -> frozenset[str]:
    return LUXURY_RESOURCES
