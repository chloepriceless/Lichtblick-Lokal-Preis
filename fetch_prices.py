#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 chloepriceless – https://github.com/chloepriceless/Lichtblick-Lokal-Preis
"""LichtBlick „ÖkoStrom Dynamic“ – Preisabruf.

Holt Strompreis-Kurven (heute + morgen), den aktuellen Preispunkt und eine
Vertragsübersicht aus dem GraphQL-Backend der LichtBlick-App und schreibt sie
nach data/prices.json.

Genutzt werden dieselben Endpoints/Header wie die offizielle App:

  Login   : Azure AD B2C, ROPC-Flow (Policy b2c_1a_ropc_signin)
  API     : POST https://graph.lichtblick.de/  (Apollo GraphQL)

Zugangsdaten über Umgebungsvariablen (oder .env im Projektstamm):

  LB_EMAIL      App-Login (E-Mail)
  LB_PASSWORD   App-Login (Passwort)
  LB_CONTRACT_ID  optional; sonst erster Strom-Vertrag des Accounts

Aufruf:  python3 fetch_prices.py [--out data/prices.json]
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

B2C_BASE = "https://anmelden.lichtblick.de/lichtblickitb2cprod.onmicrosoft.com/oauth2/v2.0"
B2C_POLICY = "b2c_1a_ropc_signin"
B2C_CLIENT_ID = "c6079bc3-9572-4da7-abb7-b69694239976"
B2C_SCOPE = (
    "openid offline_access profile "
    "https://lichtblickitb2cprod.onmicrosoft.com/graph-gateway/app-backend.access "
    "https://lichtblickitb2cprod.onmicrosoft.com/graph-gateway/service-view.access"
)
GRAPH_URL = "https://graph.lichtblick.de/"
GRAPH_HEADERS = {
    "apollographql-client-name": "lichtblick-app-android",
    "apollographql-client-version": "3.9.0",
}
TZ = "Europe/Berlin"

CACHE_STEM = Path(os.environ.get("LB_TOKEN_CACHE_DIR", str(Path.home() / ".lichtblick")))


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(TZ)
    except ZoneInfoNotFoundError as e:  # Windows ohne Zeitzonen-Datenbank
        raise RuntimeError(
            f"Zeitzone {TZ} nicht gefunden – bitte 'pip install tzdata' ausführen."
        ) from e


def _cache_path(email: str) -> Path:
    h = hashlib.sha256(email.lower().encode()).hexdigest()[:16]
    return CACHE_STEM / f"token-{h}.json"


def _password_hash(password: str, salt: str) -> str:
    """Bindet den Token-Cache ans Passwort: ohne richtiges Passwort kein Token."""
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt),
                               200_000).hex()

ENERGY_PRICE_CURVES = """
query EnergyPriceCurves($contractId: Int!, $startToday: DateTime!, $endToday: DateTime!,
                        $startTomorrow: DateTime!, $endTomorrow: DateTime!) {
  todayPriceCurves: energyPriceCurves(input: { powerCloudContractId: $contractId,
    priceCurveAggregation: DAY, start: $startToday, end: $endToday }) {
    __typename ...EnergyPriceCurvesFragment }
  tomorrowPriceCurves: energyPriceCurves(input: { powerCloudContractId: $contractId,
    priceCurveAggregation: DAY, start: $startTomorrow, end: $endTomorrow }) {
    __typename ...EnergyPriceCurvesFragment }
}
fragment EnergyPriceCurvesFragment on EnergyPriceCurvesPayload {
  __typename
  energyPriceCurvesResponse {
    __typename
    energyPricesInCents { __typename valueInCents time }
    maxAvgPriceRange { __typename startTime endTime averagePrice }
    minAvgPriceRange { __typename startTime endTime averagePrice }
    averagePriceInCents
  }
}
"""

SMOOTHED_CURVES = """
query SmoothedEnergyPriceCurves($input: SmoothedEnergyPriceCurvesInput!) {
  smoothedEnergyPriceCurves(input: $input) {
    __typename
    currentPricePoint { __typename time valueInCents }
    energyPriceCurvesResponse {
      __typename
      energyPricesInCents { __typename time valueInCents }
      maxAvgPriceRange { __typename startTime endTime averagePrice }
      minAvgPriceRange { __typename startTime endTime averagePrice }
    }
  }
}
"""

SELECTABLE_CONTRACTS = """
query SelectableContracts {
  b2cCustomer {
    __typename id
    appSelectableContracts {
      __typename
      contracts { __typename b2cContractId productName category
        address { __typename city postCode street houseNumber } }
    }
  }
}
"""

# Zusammengesetzt aus den App-Queries StatusCard, TariffInfo, ContractMeter,
# ContractDeposit und PaymentInfoCard (Feldnamen aus dem Android-DEX).
CONTRACT_OVERVIEW = """
query ContractOverview($contractId: Int!) {
  b2cCustomer {
    __typename id
    contract(contractId: $contractId) {
      __typename id
      startDate endDate createdAt terminationDate revocationDate
      extendedFeatures { __typename commodityContractStatus contractStatusChangingReason }
      minimumRuntime { __typename value unit }
      currentTariff { __typename fixedPriceMonths fixedPriceType
        terminationPeriod { __typename value unit } }
      appTariff { __typename monthlyWorkingPrice monthlyWorkingPriceNt yearlyBasePrice }
      currentProduct { __typename productName activeFrom }
      meter { __typename meterNumber }
      currentDeposit { __typename grossAmount firstDueDate }
      nextDepositPayment { __typename grossAmount dueDate }
      isDirectDebit isBankTransfer
    }
  }
}
"""

# ---------------------------------------------------------------- HTTP-Helfer


def _post(url: str, data: bytes, headers: dict) -> dict:
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode()
    except urllib.error.HTTPError as e:
        # GraphQL-Antworten kommen auch als 4xx mit JSON-Body
        try:
            resp = json.loads(e.read().decode())
        except Exception:
            raise RuntimeError(f"HTTP {e.code} für {url}") from e
        if not isinstance(resp, dict):
            raise RuntimeError(f"HTTP {e.code} für {url}")
        return resp
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"Netzwerkfehler für {url}: {getattr(e, 'reason', e)}") from e
    try:
        resp = json.loads(body)
    except ValueError as e:
        raise RuntimeError(f"Keine JSON-Antwort von {url}") from e
    if not isinstance(resp, dict):
        raise RuntimeError(f"Unerwartete Antwort von {url}")
    return resp


def _b2c_token(fields: dict) -> dict:
    url = f"{B2C_BASE}/token?p={B2C_POLICY}"
    data = urllib.parse.urlencode(fields).encode()
    resp = _post(url, data, {"Content-Type": "application/x-www-form-urlencoded"})
    if "access_token" not in resp:
        raise RuntimeError(
            f"B2C-Login fehlgeschlagen: {resp.get('error')}: "
            f"{str(resp.get('error_description'))[:200]}"
        )
    return resp


# ------------------------------------------------------------------- Login


def _load_cache(email: str, password: str) -> dict | None:
    """Cache-Eintrag nur zurückgeben, wenn er zum Passwort passt (Ablauf egal)."""
    try:
        raw = json.loads(_cache_path(email).read_text())
        salt, pw_hash = raw.get("salt"), raw.get("pw_hash")
        if (isinstance(salt, str) and isinstance(pw_hash, str)
                and hmac.compare_digest(_password_hash(password, salt), pw_hash)):
            return raw
    except Exception:
        pass
    return None


def _write_cache(path: Path, payload: dict) -> None:
    """Atomar und von Anfang an mit 0600 schreiben (kein prozessweites umask)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(payload, indent=2))
    os.replace(tmp, path)


def get_access_token(email: str, password: str) -> str:
    """ROPC-Login mit Token-Cache pro E-Mail (inkl. Refresh).

    Der Cache ist an das Passwort gebunden: mit falschem Passwort gibt es
    keinen gecachten Token, sondern einen echten (scheiternden) Login.
    """
    cache = _cache_path(email)
    cached = _load_cache(email, password)
    if (cached and cached.get("access_token")
            and cached.get("expires_at", 0) > time.time() + 60):
        return cached["access_token"]

    tok = None
    if cached and cached.get("refresh_token"):
        try:
            tok = _b2c_token({
                "client_id": B2C_CLIENT_ID,
                "grant_type": "refresh_token",
                "scope": B2C_SCOPE,
                "refresh_token": cached["refresh_token"],
            })
        except RuntimeError:
            tok = None
    if tok is None:
        tok = _b2c_token({
            "client_id": B2C_CLIENT_ID,
            "grant_type": "password",
            "scope": B2C_SCOPE,
            "username": email,
            "password": password,
        })

    salt = secrets.token_hex(16)
    payload = {
        "email": email,
        "salt": salt,
        "pw_hash": _password_hash(password, salt),
        "access_token": tok["access_token"],
        # B2C rotiert den Refresh-Token; fehlt ein neuer, den alten behalten
        "refresh_token": tok.get("refresh_token") or (cached or {}).get("refresh_token"),
        "expires_at": time.time() + int(tok.get("expires_in", 3600)),
    }
    _write_cache(cache, payload)
    return payload["access_token"]


def gql(token: str, query: str, variables: dict | None = None,
        operation: str | None = None) -> dict:
    payload: dict = {"query": query}
    if variables:
        payload["variables"] = variables
    if operation:
        payload["operationName"] = operation
    headers = dict(GRAPH_HEADERS)
    headers["Authorization"] = f"Bearer {token}"
    resp = _post(GRAPH_URL, json.dumps(payload).encode(),
                 {"Content-Type": "application/json", **headers})
    if resp.get("errors") and not resp.get("data"):
        first = resp["errors"][0] if isinstance(resp["errors"], list) else {}
        raise RuntimeError(
            f"GraphQL-Fehler: {first.get('message', '')[:300]} "
            f"[{first.get('extensions', {}).get('code', '')}]"
        )
    return resp


# ---------------------------------------------------------------- Daten holen


def _berlin_dates() -> tuple[str, str, str, str]:
    """Mitternacht heute/morgen/übermorgen in Berlin als ISO-8601.

    Kalendertage statt "+86400 s" und Offset je Mitternacht – an den Tagen
    der Zeitumstellung (23 bzw. 25 Stunden) stimmen die Grenzen so trotzdem.
    """
    tz = _tz()
    today = datetime.fromtimestamp(time.time(), tz).date()

    def midnight(days: int) -> str:
        d = today + timedelta(days=days)
        return datetime(d.year, d.month, d.day, tzinfo=tz).isoformat()

    return midnight(0), midnight(1), midnight(1), midnight(2)


def _dig(obj, *keys):
    """Verschachtelt lesen; null/fehlende Felder ergeben None statt Exception."""
    for k in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


def find_power_contract(token: str) -> dict:
    res = gql(token, SELECTABLE_CONTRACTS, operation="SelectableContracts")
    contracts = _dig(res, "data", "b2cCustomer", "appSelectableContracts",
                     "contracts") or []
    power = [c for c in contracts
             if isinstance(c, dict) and c.get("category") == "POWER"]
    if not power:
        raise RuntimeError("Kein Strom-Vertrag im Account gefunden.")
    return power[0]


def _contract_details(c: dict) -> dict:
    """Vertragsdaten flach und ohne GraphQL-Typnamen; fehlende Felder = None."""
    def period(p):
        return {"value": p.get("value"), "unit": p.get("unit")} if isinstance(p, dict) else None

    payment = None
    if c.get("isDirectDebit"):
        payment = "DIRECT_DEBIT"
    elif c.get("isBankTransfer"):
        payment = "BANK_TRANSFER"
    return {
        "status": _dig(c, "extendedFeatures", "commodityContractStatus"),
        "statusReason": _dig(c, "extendedFeatures", "contractStatusChangingReason"),
        "createdAt": c.get("createdAt"),
        "productActiveFrom": _dig(c, "currentProduct", "activeFrom"),
        "startDate": c.get("startDate"),
        "endDate": c.get("endDate"),
        "terminationDate": c.get("terminationDate"),
        "revocationDate": c.get("revocationDate"),
        "minimumRuntime": period(c.get("minimumRuntime")),
        "terminationPeriod": period(_dig(c, "currentTariff", "terminationPeriod")),
        "fixedPriceMonths": _dig(c, "currentTariff", "fixedPriceMonths"),
        "workingPrice": _dig(c, "appTariff", "monthlyWorkingPrice"),
        "workingPriceNt": _dig(c, "appTariff", "monthlyWorkingPriceNt"),
        "yearlyBasePrice": _dig(c, "appTariff", "yearlyBasePrice"),
        "meterNumber": _dig(c, "meter", "meterNumber"),
        "deposit": {"grossAmount": _dig(c, "currentDeposit", "grossAmount"),
                    "firstDueDate": _dig(c, "currentDeposit", "firstDueDate")}
                   if c.get("currentDeposit") else None,
        "nextDeposit": {"grossAmount": _dig(c, "nextDepositPayment", "grossAmount"),
                        "dueDate": _dig(c, "nextDepositPayment", "dueDate")}
                       if c.get("nextDepositPayment") else None,
        "paymentMethod": payment,
    }


def _parse_time(s: str) -> datetime:
    # Python < 3.11 kennt kein "Z" in fromisoformat
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def current_slot(points: list[dict], now: datetime | None = None) -> dict | None:
    """Preis-Slot, in dem `now` liegt (Slotlänge aus dem Punktabstand, sonst 15 min)."""
    now = now or datetime.now(_tz())
    try:
        ts = [_parse_time(p["time"]) for p in points]
    except (KeyError, TypeError, ValueError):
        return None
    for i, (p, t) in enumerate(zip(points, ts)):
        if i + 1 < len(ts):
            end = ts[i + 1]
        else:
            end = t + ((t - ts[i - 1]) if i > 0 else timedelta(minutes=15))
        if t <= now < end:
            return {"time": p["time"], "valueInCents": p["valueInCents"],
                    "durationMinutes": int((end - t).total_seconds() // 60)}
    return None


def fetch_prices(email: str, password: str,
                 contract_id: int | None = None) -> dict:
    token = get_access_token(email, password)
    contract = {"b2cContractId": contract_id, "productName": None}
    if contract_id is None:
        contract = find_power_contract(token)
        contract_id = contract.get("b2cContractId")
        if not isinstance(contract_id, int):
            raise RuntimeError("Strom-Vertrag ohne gültige Vertrags-ID.")

    s0, e0, s1, e1 = _berlin_dates()
    curves = gql(token, ENERGY_PRICE_CURVES, {
        "contractId": contract_id,
        "startToday": s0, "endToday": e0,
        "startTomorrow": s1, "endTomorrow": e1,
    }, "EnergyPriceCurves")

    smoothed: dict = {}
    try:
        smoothed = gql(token, SMOOTHED_CURVES, {
            "input": {
                "powerCloudContractId": contract_id,
                "start": s0, "end": e1,
                "priceCurveAggregation": "DAY",
                "includeLowestAndHighestPriceTimeRangeCalculation": True,
            }
        })
    except RuntimeError:
        pass  # kein Blockfeld -> optional, Kurven reichen

    try:
        overview = gql(token, CONTRACT_OVERVIEW, {"contractId": contract_id},
                       "ContractOverview")
    except RuntimeError:
        overview = {}  # nur Zusatzinfo für die Vertragsansicht
    details = _contract_details(_dig(overview, "data", "b2cCustomer", "contract") or {})

    data = curves.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("Preisabfrage lieferte keine Daten.")
    current = _dig(smoothed, "data", "smoothedEnergyPriceCurves",
                   "currentPricePoint") or {}
    # Backend-Nullwert (Zeit 0001-01-01) = "kein Datenpunkt", nicht Preis 0
    cur_time = str(current.get("time") or "")
    cur_val = current.get("valueInCents")
    if not cur_time or cur_time.startswith("0001-"):
        cur_val = None
    # Placeholder-Ranges (Jahr 0001) zu None normalisieren
    def clean_day(day: dict) -> dict:
        r = _dig(day, "energyPriceCurvesResponse") or {}
        points = [
            {"time": p["time"], "valueInCents": p["valueInCents"]}
            for p in r.get("energyPricesInCents") or []
            if isinstance(p, dict) and p.get("time")
            and isinstance(p.get("valueInCents"), (int, float))
        ]
        avg = r.get("averagePriceInCents")
        if points and avg is None:
            avg = sum(p["valueInCents"] for p in points) / len(points)
        out = {"points": points, "averageInCents": avg if points else None}
        for key, src in (("maxRange", "maxAvgPriceRange"), ("minRange", "minAvgPriceRange")):
            rng = r.get(src) or {}
            if not rng.get("startTime") or str(rng["startTime"]).startswith("0001-"):
                out[key] = None
            else:
                out[key] = {"startTime": rng.get("startTime"),
                            "endTime": rng.get("endTime"),
                            "averagePrice": rng.get("averagePrice")}
        return out

    today, tomorrow = clean_day(data.get("todayPriceCurves")), clean_day(data.get("tomorrowPriceCurves"))
    return {
        "schemaVersion": 1,
        "fetchedAt": datetime.now(_tz()).isoformat(timespec="seconds"),
        "timezone": TZ,
        "contract": {
            "id": contract_id,
            "name": contract.get("productName"),
            "address": {
                "city": (contract.get("address") or {}).get("city"),
                "postCode": (contract.get("address") or {}).get("postCode"),
            } if contract.get("address") else None,
            "startDate": details.get("startDate"),
            "details": details,
        },
        "currentPrice": cur_val,
        "currentSlot": current_slot(today["points"] + tomorrow["points"]),
        "today": today,
        "tomorrow": tomorrow,
    }


# ------------------------------------------------------------------- CLI


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(Path(__file__).parent / "data" / "prices.json"),
                    help="Zieldatei; '-' schreibt das JSON auf stdout (z. B. für Home Assistant)")
    ap.add_argument("--prices-only", action="store_true",
                    help="Vertragsdaten weglassen (kleinere Ausgabe, z. B. für Sensor-Attribute)")
    args = ap.parse_args()

    _load_dotenv(Path(__file__).parent / ".env")
    email = os.environ.get("LB_EMAIL", "")
    password = os.environ.get("LB_PASSWORD", "")
    if not email or not password:
        print("LB_EMAIL / LB_PASSWORD fehlen (.env oder Umgebungsvariablen).",
              file=sys.stderr)
        return 2
    contract_id = os.environ.get("LB_CONTRACT_ID", "").strip()
    if contract_id and not contract_id.isdigit():
        print("LB_CONTRACT_ID muss eine Zahl sein.", file=sys.stderr)
        return 2

    try:
        result = fetch_prices(email, password,
                              int(contract_id) if contract_id else None)
    except RuntimeError as e:
        print(f"Fehler: {e}", file=sys.stderr)
        return 1

    if args.prices_only:
        result.pop("contract", None)
    if args.out == "-":
        print(json.dumps(result, ensure_ascii=False))
        return 0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    tmp.replace(out)
    n_today, n_tom = len(result["today"]["points"]), len(result["tomorrow"]["points"])
    print(f"OK: {n_today} Punkte heute, {n_tom} Punkte morgen -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
