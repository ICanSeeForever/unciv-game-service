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
