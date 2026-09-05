"""Unciv save file parsing (base64 + gzip + JSON)."""
import base64
import gzip
import json


def decode_save(raw: str) -> dict:
    """Decode a raw Unciv save string into a Python dict."""
    data = base64.b64decode(raw)
    data = gzip.decompress(data)
    return json.loads(data.decode("utf-8"))


def encode_save(game_dict: dict) -> str:
    """Encode a Python dict back into a raw Unciv save string."""
    raw = json.dumps(game_dict, ensure_ascii=False)
    compressed = gzip.compress(raw.encode("utf-8"))
    return base64.b64encode(compressed).decode("ascii")


# Поля-указатели «чей ход», которые обязаны совпадать между сейвом и превью.
_PREVIEW_TURN_KEYS = ("currentPlayer", "turns", "currentTurnStartTime")


def align_preview_to_save(save_text: str, preview_text: str | None) -> str | None:
    """Вернуть превью, чей указатель хода приведён к сейву (сейв — авторитет).

    Пер-ходовой бэкап может захватить превью, отстающее от сейва на одного игрока
    (сейв заливается раньше своего превью). Вся детекция «чей ход» (наш шедулер и
    клиент в лобби) читает превью — из-за отставания показывается игрок на шаг
    назад, а реальный ``currentPlayer`` из сейва почти сразу доигрывает. Копируем
    ``currentPlayer``/``turns``/``currentTurnStartTime`` из сейва, чтобы пара была
    консистентна. Best-effort: при любой ошибке декодирования возвращаем исходное
    превью без изменений.
    """
    if not preview_text:
        return preview_text
    try:
        save = decode_save(save_text)
        prev = decode_save(preview_text)
    except Exception:  # noqa: BLE001 — битое превью не должно ронять restore
        return preview_text
    changed = False
    for key in _PREVIEW_TURN_KEYS:
        if key in save and prev.get(key) != save[key]:
            prev[key] = save[key]
            changed = True
    if not changed:
        return preview_text
    try:
        return encode_save(prev)
    except Exception:  # noqa: BLE001
        return preview_text
