"""Guard: КАЖДЫЙ GET/PUT к uncivserver обязан идти через прокси-цепочку.

Правило (по требованию владельца): любые чтения/записи на uncivserver.xyz идут
через форвард-прокси (germ→neth→прямой), ВСЕГДА — при sync ON и OFF, включая
восстановление из бэкапа. Реализовано на транспортном уровне в
``app/game/remote.py`` (``fetch_file``/``put_file`` через ``_egress_transports()``).

Эти тесты — статический предохранитель: они падают, если
  1) кто-то добавил HTTP-клиент вне явного allowlist (потенциальный обход прокси), или
  2) remote.py перестал прокидывать ``proxy=`` / ``UNCIV_UPLOAD_PROXY``.

Сеть не трогают — только скан исходников. Запуск: ``pytest tests/`` или
``python tests/test_proxy_guard.py``.
"""
import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"

# Файлы, которым разрешён свой HTTP-клиент, с обоснованием. Всё остальное с
# httpx/requests/aiohttp должно быть осознанно добавлено сюда после ревью —
# иначе это возможный прямой поход на uncivserver мимо проксированного remote.py.
ALLOWED_HTTP_CLIENTS = {
    "game/remote.py",      # единственная точка общения с uncivserver — проксирована
    "launchers/local.py",  # качает Unciv.jar с GitHub-релизов (не uncivserver)
    "routers/games.py",    # оффсайт-бэкап POST на наш BACKUP_IP-приёмник (не uncivserver)
}

# Реальное использование HTTP-клиента (импорт/конструктор), а не англ. слово "request".
_CLIENT_PAT = re.compile(
    r"httpx\.(?:Async)?Client"
    r"|^\s*import\s+httpx"
    r"|^\s*import\s+requests"
    r"|^\s*import\s+aiohttp"
    r"|requests\.(?:get|post|put|Session)",
    re.MULTILINE,
)


def _rel(p: Path) -> str:
    return p.relative_to(APP).as_posix()


def test_no_http_clients_outside_allowlist() -> None:
    offenders = []
    for py in APP.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="ignore")
        if _CLIENT_PAT.search(text) and _rel(py) not in ALLOWED_HTTP_CLIENTS:
            offenders.append(_rel(py))
    assert not offenders, (
        "Новый HTTP-клиент вне allowlist — доступ к uncivserver обязан идти через "
        "проксированные remote.fetch_file/put_file. Проверь и, если это не поход на "
        "uncivserver, добавь файл в ALLOWED_HTTP_CLIENTS: " + ", ".join(sorted(offenders))
    )


def test_remote_routes_through_proxy_chain() -> None:
    src = (APP / "game" / "remote.py").read_text(encoding="utf-8")
    assert "UNCIV_UPLOAD_PROXY" in src, "remote.py должен читать UNCIV_UPLOAD_PROXY"
    assert "_egress_transports" in src, "remote.py должен использовать _egress_transports()"
    # и GET (fetch_file), и PUT (put_file) должны строить клиент с proxy=
    assert src.count("proxy=proxy") >= 2, (
        "fetch_file и put_file обязаны передавать proxy=proxy в httpx-клиент"
    )
    # egress-цепочка = прокси по порядку + прямой резерв
    assert "list(_UPLOAD_PROXIES) + [None]" in src, (
        "_egress_transports() должен возвращать прокси + прямой (None) резерв"
    )


if __name__ == "__main__":
    test_no_http_clients_outside_allowlist()
    test_remote_routes_through_proxy_chain()
    print("proxy guard: OK")
