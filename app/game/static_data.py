"""Public game static data: city-states, marine civs, luxury resources."""

# BNW + RekMOD luxury names — from public Unciv game data
LUXURY_RESOURCES: frozenset[str] = frozenset({
    "Amber", "Citrus", "Cloves", "Cocoa", "Coconut", "Coffee", "Copper",
    "Coral", "Cotton", "Crab", "Dyes", "Furs", "Gems", "Glass", "Gold Ore",
    "Incense", "Ivory", "Jade", "Jewelry", "Lapis Lazuli", "Marble",
    "Nutmeg", "Obsidian", "Olives", "Pearls", "Pepper", "Perfume",
    "Porcelain", "Rubber", "Salt", "Silk", "Silver", "Spices", "Sugar",
    "Tea", "Tobacco", "Truffles", "Whales", "Wine",
})

# Marine civilizations (benefit from coast start)
MARINE_CIVS: frozenset[str] = frozenset({
    "Australia", "Brunei", "Carthage", "Chile", "Denmark", "England",
    "Indonesia", "Japan", "Kilwa", "Korea", "The Netherlands", "Norway",
    "Oman", "Philippines", "Phoenicia", "Polynesia", "Portugal", "Spain",
    "Tonga", "Venice",
})

# City-states and non-playable civs (excluded from player nation checks)
CITY_STATES: frozenset[str] = frozenset({
    "Baku", "Chaco Canyon", "Djibouti", "Kuala Lumpur", "Kuwait City",
    "Kyzyl", "Ljubljana", "Luxembourg", "Monaco", "Montevideo", "Nicosia",
    "Reykjavik", "Teheran", "Thimphu", "Bangkok", "Cape Town", "Islamabad",
    "Kampala", "Lima", "Lusaka", "Mogadishu", "Ormus", "Panama City",
    "Paramaribo", "Phnom Penh", "Port-au-Prince", "Riga", "Santo Domingo",
    "Cahokia", "Colombo", "Dubai", "Gaborone", "Hong Kong", "Lagos",
    "Malacca", "Maseru", "Mohenjo-Daro", "Singapore", "Tallinn", "Trieste",
    "Vaduz", "Zurich", "Andorra", "Colchis", "Cyrene", "Harappa", "Havana",
    "Tirana", "Troy", "Valletta", "Geneva", "Ife", "Kathmandu", "La Venta",
    "Qufu", "Sarajevo", "Taipei", "Wittenberg",
    "Spectator", "Barbarians",
})


def get_city_states() -> frozenset[str]:
    return CITY_STATES


def get_marine_civs() -> frozenset[str]:
    return MARINE_CIVS


def get_luxuries() -> frozenset[str]:
    return LUXURY_RESOURCES
