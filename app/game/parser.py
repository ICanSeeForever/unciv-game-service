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


# Структура GameInfoPreview (обрезанная версия сейва). Верхний уровень и поля
# каждой цивилизации — ровно то, что кладёт Unciv ``GameInfo.asPreview()``.
_PREVIEW_TOP_KEYS = ("civilizations", "currentPlayer", "currentTurnStartTime",
                     "difficulty", "gameId", "gameParameters", "turns")
_PREVIEW_CIV_KEYS = ("civName", "playerType", "playerId", "civID",
                     "totalTurnTimeSeconds", "turnsPlayedAsHuman")


def build_preview_from_save(save: dict) -> dict:
    """Собрать GameInfoPreview из полного сейва (как Unciv ``GameInfo.asPreview()``).

    Превью — обрезанная копия сейва: тот же порядок цивилизаций, но у каждой лишь
    лёгкие поля (``civName``/``playerId``/``playerType``/…), плюс указатель хода
    (``currentPlayer``/``turns``/``currentTurnStartTime``), ``difficulty``,
    ``gameId``, ``gameParameters``. null-поля опускаются — как в сериализации Unciv.
    Проверено побайтово против настоящих превью.
    """
    civs: list[dict] = []
    for c in save.get("civilizations", []):
        civs.append({k: c[k] for k in _PREVIEW_CIV_KEYS if c.get(k) is not None})
    out: dict = {}
    for k in _PREVIEW_TOP_KEYS:
        out[k] = civs if k == "civilizations" else save.get(k)
    return out


def regenerate_preview(save_text: str, fallback: str | None = None) -> str | None:
    """Сгенерировать превью С НУЛЯ из сейва (сейв — единственный источник истины).

    Пер-ходовой бэкап может захватить превью, отстающее от сейва на одного игрока
    (сейв заливается раньше своего превью), а вся детекция «чей ход» читает превью
    → неверный игрок в показе и самопроизвольный ход. Поэтому при восстановлении
    архивное превью НЕ используем, а собираем заново из восстанавливаемого сейва.
    Best-effort: при ошибке декодирования/сборки возвращаем ``fallback`` (исходное
    архивное превью), чтобы не сделать хуже.
    """
    try:
        return encode_save(build_preview_from_save(decode_save(save_text)))
    except Exception:  # noqa: BLE001 — битый сейв не должен ронять restore
        return fallback
