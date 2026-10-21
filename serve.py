#!/usr/bin/env python3
"""Kleiner lokaler Webserver für die Preisseite.

Der LichtBlick-GraphQL-Endpoint antwortet nur mit CORS-Erlaubnis für
*.lichtblick.de – ein reines Aufmachen von index.html per file:// oder von
einem anderen Origin aus funktioniert daher nicht. Dieser Server liefert die
statische Seite aus und reicht die Preisanfrage (gleicher Ursprung) server-
seitig an das LichtBlick-API weiter. Es läuft komplett lokal, keine Daten
gehen an Dritte außer LichtBlick selbst.

    python3 serve.py                 # http://127.0.0.1:8080
    LB_HOST=0.0.0.0 python3 serve.py # im LAN freigeben (Achtung: HTTP,
                                     # Zugangsdaten gehen unverschlüsselt durchs LAN)

Das Browser-Formular POSTet Zugangsdaten an /api/prices; pro E-Mail wird ein
ablaufender Token-Cache in ~/.lichtblick/ abgelegt (Dateirechte 0600).
Ausgeliefert werden nur die Dateien in STATIC_FILES – nie .env, .git oder
Quelltext.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fetch_prices  # noqa: E402

ROOT = Path(__file__).parent
# Pfad -> (Datei, Content-Type). Alles andere ist 404.
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/data/prices_demo.json": ("data/prices_demo.json", "application/json; charset=utf-8"),
}

_lock = threading.Lock()
# Antwort-Cache, damit ein versehentliches Skript nicht gegen die API bollert.
# Schlüssel enthält das Passwort (gehasht) -> ohne Passwort kein Cache-Treffer.
_last_fetch: dict[str, tuple[float, dict]] = {}
CACHE_TTL = 120  # Sekunden
# Fehl-Logins pro E-Mail bremsen (LichtBlick sperrt Konten nach zu vielen Versuchen)
_failures: dict[str, tuple[int, float]] = {}
FAIL_BASE_DELAY = 5  # Sekunden, verdoppelt sich je Fehlversuch
FAIL_MAX_DELAY = 15 * 60
MAX_BODY = 64 * 1024


def _cache_key(email: str, password: str, contract_id: int | None) -> str:
    raw = json.dumps([email.lower(), password, contract_id])
    return hashlib.sha256(raw.encode()).hexdigest()


def _retry_after(email: str, now: float) -> int:
    n, last = _failures.get(email.lower(), (0, 0.0))
    if not n:
        return 0
    delay = min(FAIL_BASE_DELAY * 2 ** (n - 1), FAIL_MAX_DELAY)
    return max(0, int(last + delay - now + 0.999))


class Handler(BaseHTTPRequestHandler):
    server_version = "lichtblick-preise"
    sys_version = ""

    # ---- Helpers -------------------------------------------------------
    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, obj: dict, extra: dict | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self._send(status, body, "application/json; charset=utf-8", extra)

    # ---- Routen --------------------------------------------------------
    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/api/health":
            self._send_json(200, {"ok": True})
            return
        entry = STATIC_FILES.get(path)
        if entry is None:
            self._send_json(404, {"error": "nicht gefunden"})
            return
        try:
            body = (ROOT / entry[0]).read_bytes()
        except OSError:
            self._send_json(404, {"error": "nicht gefunden"})
            return
        self._send(200, body, entry[1])

    do_HEAD = do_GET  # noqa: N815

    def do_POST(self):  # noqa: N802
        if self.path.split("?")[0] != "/api/prices":
            self._send_json(404, {"error": "unbekannter Pfad"})
            return
        try:
            self._handle_prices()
        except Exception:  # nie ohne Antwort abbrechen; Details nur ins Log
            traceback.print_exc()
            self._send_json(500, {"error": "Interner Fehler im lokalen Server"})

    def _handle_prices(self) -> None:
        length_hdr = self.headers.get("Content-Length") or "0"
        if not length_hdr.isdigit():
            self.close_connection = True
            self._send_json(400, {"error": "ungültige Content-Length"})
            return
        length = int(length_hdr)
        if length > MAX_BODY:
            self.close_connection = True
            self._send_json(413, {"error": "Request-Body zu groß"})
            return
        try:
            body = json.loads(self.rfile.read(length).decode() or "{}")
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "Body muss JSON mit email/password sein"})
            return
        email = str(body.get("email") or "").strip()
        password = str(body.get("password") or "")
        if not email or not password:
            self._send_json(400, {"error": "email und password erforderlich"})
            return
        raw_cid = body.get("contractId")
        contract_id = None
        if raw_cid not in (None, ""):
            if isinstance(raw_cid, bool) or not str(raw_cid).strip().isdigit():
                self._send_json(400, {"error": "contractId muss eine Zahl sein"})
                return
            contract_id = int(str(raw_cid).strip())

        now = time.time()
        key = _cache_key(email, password, contract_id)
        with _lock:
            prev = _last_fetch.get(key)
            if prev and now - prev[0] < CACHE_TTL:
                self._send_json(200, prev[1])
                return
            wait = _retry_after(email, now)
        if wait:
            self._send_json(429, {"error": f"Zu viele Fehlversuche – bitte in {wait} s erneut versuchen."},
                            {"Retry-After": str(wait)})
            return

        try:
            data = fetch_prices.fetch_prices(email, password, contract_id)
        except RuntimeError as e:
            if "B2C-Login" in str(e):
                with _lock:
                    n, _ = _failures.get(email.lower(), (0, 0.0))
                    _failures[email.lower()] = (n + 1, time.time())
            self._send_json(502, {"error": str(e)})
            return
        with _lock:
            _failures.pop(email.lower(), None)
            # abgelaufene Einträge aufräumen, damit der Cache nicht wächst
            for k in [k for k, (t, _) in _last_fetch.items() if now - t >= CACHE_TTL]:
                del _last_fetch[k]
            _last_fetch[key] = (time.time(), data)
        self._send_json(200, data)


if __name__ == "__main__":
    host = os.environ.get("LB_HOST", "127.0.0.1")
    port = int(os.environ.get("LB_PORT", "8080"))
    print(f"LichtBlick-Preise:  http://{host}:{port}/")
    print("Strg+C zum Beenden.")
    try:
        ThreadingHTTPServer((host, port), Handler).serve_forever()
    except KeyboardInterrupt:
        pass
