"""HTTP access to a remote Unciv multiplayer server (external host).

When a game lives on an external Unciv server (e.g. https://uncivserver.xyz) we
cannot touch its files directly — only through the server's REST API:

    GET  <host>/files/<id>       -> raw save text (base64+gzip), Basic auth
    PUT  <host>/files/<id>       <- raw save text,               Basic auth

UncivServer's auth model (see UncivServer.kt): the Basic-auth *username* must be a
valid UUID; GET ignores the password entirely; PUT is accepted when the userId has
no registered password OR the supplied password matches. So:

  * GET — always send a throwaway UUID login (password ignored).
  * PUT — try the caller's uid/password first, then the Spectator's playerId parsed
    from the file (it usually has no password), then a throwaway UUID (unknown
    userIds have no password → accepted).

All content is passed through verbatim as the on-disk format (base64+gzip text);
callers decode/encode with app.game.parser.
"""
import base64
import gzip
import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

# Throwaway UUID login used as the last-resort PUT credential and the GET login.
# UncivServer only needs a syntactically valid UUID; an unknown userId has no
# password and is therefore accepted on write.
FALLBACK_UID = "5f6d4c3b-2a19-4e87-b6c5-0d1e2f3a4b5c"

# uncivserver.xyz sits behind Cloudflare, which blocks requests whose User-Agent
# is not the Unciv game client (returns a 403 HTML challenge). Real Unciv clients
# send "Unciv/<version>…"; we must do the same or every PUT/GET is bounced before
# it reaches the game server. Overridable via env if the rule ever changes.
_UNCIV_UA = os.getenv("UNCIV_USER_AGENT", "Unciv/4.21.14-GNU-Terry-Pratchett")
_HEADERS = {"User-Agent": _UNCIV_UA}

_GET_TIMEOUT = httpx.Timeout(30.0)
_PUT_TIMEOUT = httpx.Timeout(60.0)


def _files_url(host: str, file_name: str) -> str:
    return f"{host.rstrip('/')}/files/{file_name}"


async def fetch_file(host: str, file_name: str) -> str:
    """GET <host>/files/<file_name>; return raw on-disk text. Raises on HTTP error.

    ``file_name`` is the bare game id for the save, or ``<id>_Preview`` for preview.
    """
    async with httpx.AsyncClient(timeout=_GET_TIMEOUT, follow_redirects=True,
                                 headers=_HEADERS) as c:
        resp = await c.get(_files_url(host, file_name), auth=(FALLBACK_UID, ""))
    resp.raise_for_status()
    return resp.text.strip()


def spectator_id_from_raw(raw: str) -> str | None:
    """Parse the Spectator civ's playerId from a save file (encoded or plain JSON).

    Follows the decode logic requested: if the body is already JSON, use it as-is;
    otherwise base64-decode + gzip-decompress first. Returns None if not found.
    """
    text = (raw or "").strip()
    if not text:
        return None
    data = None
    try:
        data = json.loads(text)
    except (ValueError, json.JSONDecodeError):
        try:
            decoded = gzip.decompress(base64.b64decode(text)).decode("utf-8")
            data = json.loads(decoded)
        except Exception:
            return None
    if not isinstance(data, dict):
        return None
    for civ in data.get("civilizations") or []:
        if isinstance(civ, dict) and civ.get("civName") == "Spectator":
            pid = civ.get("playerId")
            return str(pid) if pid else None
    return None


async def put_file(host: str, file_name: str, raw: str, *,
                   uid: str | None = None, password: str | None = None) -> str:
    """PUT raw content to <host>/files/<file_name>, trying auth strategies in order.

    1. caller's ``uid`` (+ optional ``password``), if given;
    2. the Spectator playerId parsed from the file (no password);
    3. a throwaway UUID (UncivServer accepts unknown userIds on write).

    Returns the login that succeeded. Raises the last HTTP error if all fail.
    """
    candidates: list[tuple[str, str]] = []
    if uid:
        candidates.append((uid, password or ""))
    spec = spectator_id_from_raw(raw)
    if spec and all(spec != login for login, _ in candidates):
        candidates.append((spec, ""))
    if all(FALLBACK_UID != login for login, _ in candidates):
        candidates.append((FALLBACK_UID, ""))

    url = _files_url(host, file_name)
    body = raw.encode("utf-8")
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=_PUT_TIMEOUT, follow_redirects=True,
                                 headers=_HEADERS) as c:
        for login, pw in candidates:
            try:
                resp = await c.put(url, content=body, auth=(login, pw))
            except httpx.HTTPError as e:
                last_exc = e
                continue
            if resp.status_code < 400:
                return login
            last_exc = httpx.HTTPStatusError(
                f"PUT {url} -> {resp.status_code}", request=resp.request,
                response=resp)
            # 401/403 → try next credential; other codes unlikely to differ but
            # we still fall through to the throwaway UUID as a last resort.
    if last_exc:
        raise last_exc
    raise RuntimeError(f"PUT {url}: no auth candidates")
