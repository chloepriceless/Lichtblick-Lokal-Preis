# SPDX-License-Identifier: MIT
# Copyright 2026 chloepriceless – https://github.com/chloepriceless/Lichtblick-Lokal-Preis
"""Offline-Tests für lichtblick-preise (kein Netz, LichtBlick-API gemockt).

Aufruf im Projektordner:  python3 -m unittest discover -s tests -v
"""
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ["LB_TOKEN_CACHE_DIR"] = tempfile.mkdtemp()
import fetch_prices as fp  # noqa: E402
import serve  # noqa: E402

BERLIN = ZoneInfo("Europe/Berlin")


def berlin_ts(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=BERLIN).timestamp()


def expected_window(ts):
    """Referenz: Mitternachtsgrenzen heute/morgen/übermorgen in Berlin, korrekt mit DST."""
    from datetime import timedelta
    d0 = datetime.fromtimestamp(ts, BERLIN).date()
    out = []
    for k in (0, 1, 1, 2):
        dd = d0 + timedelta(days=k)
        out.append(datetime(dd.year, dd.month, dd.day, tzinfo=BERLIN).isoformat())
    return tuple(out)


class BerlinDates(unittest.TestCase):
    def setUp(self):
        self._tz = os.environ.get("TZ")

    def tearDown(self):
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()

    def check(self, ts):
        with mock.patch.object(fp.time, "time", return_value=ts):
            got = fp._berlin_dates()
        self.assertEqual(got, expected_window(ts))

    def test_normaler_tag(self):
        self.check(berlin_ts(2026, 11, 5, 14))

    def test_tag_vor_zeitumstellung_herbst(self):
        # 25.10.2026 hat 25 h -> Ende "morgen" muss +01:00 haben
        self.check(berlin_ts(2026, 10, 24, 14))

    def test_kurz_nach_mitternacht_am_herbst_umstellungstag(self):
        self.check(berlin_ts(2026, 10, 25, 0, 30))

    def test_spaet_abends_vor_fruehjahrsumstellung(self):
        # 28.03.2027 hat 23 h -> "morgen" darf nicht übersprungen werden
        self.check(berlin_ts(2027, 3, 27, 23, 30))

    def test_tag_vor_fruehjahrsumstellung(self):
        self.check(berlin_ts(2027, 3, 27, 14))

    def test_TZ_bereits_auf_UTC_gesetzt(self):
        os.environ["TZ"] = "UTC"
        time.tzset()
        self.check(berlin_ts(2026, 11, 5, 0, 30))  # 23:30 UTC am Vortag


class TokenCache(unittest.TestCase):
    def setUp(self):
        fp.CACHE_STEM = Path(tempfile.mkdtemp())

    def _write_cache(self, email, expires_in, refresh="RT-alt", password="pw"):
        p = fp._cache_path(email)
        p.parent.mkdir(parents=True, exist_ok=True)
        salt = "00" * 16
        p.write_text(json.dumps({"email": email, "access_token": "AT-alt",
                                 "salt": salt, "pw_hash": fp._password_hash(password, salt),
                                 "refresh_token": refresh,
                                 "expires_at": time.time() + expires_in}))

    def test_gueltiger_token_mit_richtigem_passwort_ohne_login(self):
        self._write_cache("a@b.de", 3000)
        with mock.patch.object(fp, "_b2c_token", side_effect=AssertionError("kein Login erwartet")):
            self.assertEqual(fp.get_access_token("a@b.de", "pw"), "AT-alt")

    def test_alter_cache_ohne_passwort_hash_fuehrt_zu_neuem_login(self):
        p = fp._cache_path("x@y.de")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"access_token": "AT", "expires_at": time.time() + 3000}))
        with mock.patch.object(fp, "_b2c_token", return_value={
                "access_token": "neu", "refresh_token": "r", "expires_in": 3600}) as m:
            self.assertEqual(fp.get_access_token("x@y.de", "pw"), "neu")
        self.assertEqual(m.call_args[0][0]["grant_type"], "password")

    def test_refresh_fehlgeschlagen_faellt_auf_passwort_zurueck(self):
        self._write_cache("a@b.de", -10)
        calls = []

        def fake(fields):
            calls.append(fields["grant_type"])
            if fields["grant_type"] == "refresh_token":
                raise RuntimeError("B2C-Login fehlgeschlagen: invalid_grant")
            return {"access_token": "AT-neu", "expires_in": 3600}
        with mock.patch.object(fp, "_b2c_token", side_effect=fake):
            fp.get_access_token("a@b.de", "pw")
        self.assertEqual(calls, ["refresh_token", "password"])

    def test_abgelaufener_token_wird_per_refresh_erneuert(self):
        self._write_cache("a@b.de", -10)
        calls = []

        def fake(fields):
            calls.append(fields["grant_type"])
            return {"access_token": "AT-neu", "refresh_token": "RT-neu", "expires_in": 3600}

        with mock.patch.object(fp, "_b2c_token", side_effect=fake):
            fp.get_access_token("a@b.de", "pw")
        self.assertEqual(calls, ["refresh_token"], "Refresh-Pfad wird nie benutzt")

    def test_falsches_passwort_bekommt_keinen_gecachten_token(self):
        self._write_cache("a@b.de", 3000)  # gültiger Token für das richtige Passwort
        with mock.patch.object(fp, "_b2c_token",
                               side_effect=RuntimeError("B2C-Login fehlgeschlagen")):
            with self.assertRaises(RuntimeError):
                fp.get_access_token("a@b.de", "FALSCH")

    def test_cache_datei_hat_0600(self):
        with mock.patch.object(fp, "_b2c_token", return_value={
                "access_token": "x", "refresh_token": "y", "expires_in": 3600}):
            fp.get_access_token("c@d.de", "pw")
        mode = fp._cache_path("c@d.de").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


def fake_gql_factory(contracts_payload=None, points_today=(), points_tomorrow=(),
                     current=None):
    def resp_day(points):
        return {"energyPriceCurvesResponse": {
            "energyPricesInCents": list(points),
            "maxAvgPriceRange": {"startTime": "0001-01-01T00:00:00.000Z", "endTime": "0001-01-01T00:00:00.000Z", "averagePrice": 0},
            "minAvgPriceRange": {"startTime": "0001-01-01T00:00:00.000Z", "endTime": "0001-01-01T00:00:00.000Z", "averagePrice": 0},
            "averagePriceInCents": 0}}

    def fake(token, query, variables=None, operation=None):
        if "SelectableContracts" in query:
            return contracts_payload
        if "smoothedEnergyPriceCurves" in query:
            return {"data": {"smoothedEnergyPriceCurves": {
                "currentPricePoint": current or {"time": "0001-01-01T00:00:00.000Z", "valueInCents": 0}}}}
        return {"data": {"todayPriceCurves": resp_day(points_today),
                         "tomorrowPriceCurves": resp_day(points_tomorrow)}}
    return fake


class FetchPrices(unittest.TestCase):
    def run_with(self, **kw):
        with mock.patch.object(fp, "get_access_token", return_value="T"), \
             mock.patch.object(fp, "gql", side_effect=fake_gql_factory(**kw)):
            return fp.fetch_prices("a@b.de", "pw")

    OK_CONTRACTS = {"data": {"b2cCustomer": {"appSelectableContracts": {"contracts": [
        {"b2cContractId": 1, "productName": "Gas", "category": "GAS"},
        {"b2cContractId": 2, "productName": "ÖkoStrom Dynamic", "category": "POWER",
         "address": {"city": "X", "postCode": "1", "street": "S", "houseNumber": "1"}}]}}}}

    def test_inaktiver_vertrag_wie_live_beobachtet(self):
        d = self.run_with(contracts_payload=self.OK_CONTRACTS)
        self.assertEqual(d["contract"]["id"], 2)
        self.assertIsNone(d["currentPrice"])
        self.assertIsNone(d["today"]["minRange"])
        self.assertNotIn("street", json.dumps(d))

    def test_preis_null_cent_ist_gueltig(self):
        d = self.run_with(contracts_payload=self.OK_CONTRACTS,
                          current={"time": "2026-11-01T12:00:00Z", "valueInCents": 0})
        self.assertEqual(d["currentPrice"], 0)

    def test_vertragsdaten_normalisiert(self):
        overview = {"data": {"b2cCustomer": {"contract": {
            "startDate": "2026-11-01", "createdAt": "2026-10-08",
            "extendedFeatures": {"commodityContractStatus": "WAITING", "contractStatusChangingReason": "ACTIVATING"},
            "minimumRuntime": {"__typename": "B2cPeriod", "value": 0, "unit": "MONTHS"},
            "currentTariff": {"fixedPriceMonths": 0, "terminationPeriod": {"value": 0, "unit": "DAYS"}},
            "appTariff": {"monthlyWorkingPrice": None}, "meter": {"meterNumber": None},
            "currentDeposit": None, "isDirectDebit": True, "isBankTransfer": False}}}}
        base = fake_gql_factory(contracts_payload=self.OK_CONTRACTS)

        def fake(token, query, variables=None, operation=None):
            return overview if "ContractOverview" in query else base(token, query, variables, operation)
        with mock.patch.object(fp, "get_access_token", return_value="T"), \
             mock.patch.object(fp, "gql", side_effect=fake):
            d = fp.fetch_prices("a@b.de", "pw")
        x = d["contract"]["details"]
        self.assertEqual(d["contract"]["startDate"], "2026-11-01")
        self.assertEqual(x["status"], "WAITING")
        self.assertEqual(x["minimumRuntime"], {"value": 0, "unit": "MONTHS"})
        self.assertEqual(x["paymentMethod"], "DIRECT_DEBIT")
        self.assertIsNone(x["deposit"])
        self.assertNotIn("__typename", json.dumps(x))

    def test_vertragsuebersicht_fehler_bricht_preise_nicht_ab(self):
        base = fake_gql_factory(contracts_payload=self.OK_CONTRACTS,
                                points_today=[{"time": "2026-11-05T00:00:00+01:00", "valueInCents": 20.0}])

        def fake(token, query, variables=None, operation=None):
            if "ContractOverview" in query:
                raise RuntimeError("GraphQL-Fehler")
            return base(token, query, variables, operation)
        with mock.patch.object(fp, "get_access_token", return_value="T"), \
             mock.patch.object(fp, "gql", side_effect=fake):
            d = fp.fetch_prices("a@b.de", "pw")
        self.assertEqual(len(d["today"]["points"]), 1)
        self.assertIsNone(d["contract"]["details"]["status"])

    def test_durchschnitt_aus_punkten_wenn_api_keinen_liefert(self):
        pts = [{"time": "2026-11-05T00:00:00+01:00", "valueInCents": 10.0},
               {"time": "2026-11-05T00:15:00+01:00", "valueInCents": 20.0}]
        base = fake_gql_factory(contracts_payload=self.OK_CONTRACTS, points_today=pts)

        def fake(token, query, variables=None, operation=None):
            r = base(token, query, variables, operation)
            if "todayPriceCurves" in json.dumps(r):
                r["data"]["todayPriceCurves"]["energyPriceCurvesResponse"]["averagePriceInCents"] = None
            return r
        with mock.patch.object(fp, "get_access_token", return_value="T"), \
             mock.patch.object(fp, "gql", side_effect=fake):
            d = fp.fetch_prices("a@b.de", "pw")
        self.assertEqual(d["today"]["averageInCents"], 15.0)

    def test_b2cCustomer_null_gibt_saubere_fehlermeldung(self):
        with self.assertRaises(RuntimeError):
            self.run_with(contracts_payload={"data": {"b2cCustomer": None}})

    def test_data_null_mit_teilfehler_gibt_saubere_fehlermeldung(self):
        with self.assertRaises(RuntimeError):
            self.run_with(contracts_payload={"data": None})


class CurrentSlot(unittest.TestCase):
    PTS = [{"time": "2026-11-04T23:00:00Z", "valueInCents": 10.0},   # 00:00 Berlin
           {"time": "2026-11-04T23:15:00Z", "valueInCents": 11.0},
           {"time": "2026-11-04T23:30:00Z", "valueInCents": 12.0}]

    def at(self, h, m):
        return datetime(2026, 11, 5, h, m, tzinfo=BERLIN)

    def test_slot_mitten_drin(self):
        s = fp.current_slot(self.PTS, self.at(0, 20))
        self.assertEqual((s["valueInCents"], s["durationMinutes"]), (11.0, 15))

    def test_letzter_slot_nutzt_abstand(self):
        self.assertEqual(fp.current_slot(self.PTS, self.at(0, 44))["valueInCents"], 12.0)
        self.assertIsNone(fp.current_slot(self.PTS, self.at(0, 45)))

    def test_vor_dem_ersten_und_leer(self):
        self.assertIsNone(fp.current_slot(self.PTS, self.at(0, 0).replace(day=4)))
        self.assertIsNone(fp.current_slot([], self.at(0, 0)))

    def test_stundenwerte(self):
        pts = [{"time": "2026-11-05T00:00:00+01:00", "valueInCents": 1},
               {"time": "2026-11-05T01:00:00+01:00", "valueInCents": 2}]
        self.assertEqual(fp.current_slot(pts, self.at(1, 59))["durationMinutes"], 60)


class EvccRates(unittest.TestCase):
    def test_format_wie_evcc_custom_tarif(self):
        pts = [{"time": "2026-11-04T23:00:00Z", "valueInCents": 25.335},
               {"time": "2026-11-04T23:15:00Z", "valueInCents": -1.5}]
        r = fp.evcc_rates(pts)
        self.assertEqual([set(x) for x in r], [{"start", "end", "value"}] * 2)
        self.assertEqual(r[0]["value"], 0.25335)
        self.assertEqual(r[1]["value"], -0.015)
        # lückenlos: Ende = Start des nächsten Slots; letzter Slot mit gleichem Abstand
        self.assertEqual(datetime.fromisoformat(r[0]["end"]), datetime.fromisoformat(r[1]["start"]))
        self.assertEqual(datetime.fromisoformat(r[1]["end"]) - datetime.fromisoformat(r[1]["start"]),
                         datetime.fromisoformat(r[0]["end"]) - datetime.fromisoformat(r[0]["start"]))

    def test_rfc3339_mit_offset(self):
        import re
        for x in fp.evcc_rates([{"time": "2026-11-05T00:00:00+01:00", "valueInCents": 20}]):
            for k in ("start", "end"):
                self.assertRegex(x[k], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d([+-]\d\d:\d\d)$")

    def test_leer(self):
        self.assertEqual(fp.evcc_rates([]), [])


class Cli(unittest.TestCase):
    def run_main(self, argv, env):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["fetch_prices.py", *argv]), \
             mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(fp, "_load_dotenv"), \
             mock.patch.object(fp, "fetch_prices", return_value={
                 "schemaVersion": 1, "contract": {"id": 1},
                 "today": {"points": []}, "tomorrow": {"points": []}}), \
             mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = fp.main()
        return code, out.getvalue(), err.getvalue()

    def test_stdout_json(self):
        code, out, _ = self.run_main(["--out", "-"], {"LB_EMAIL": "a", "LB_PASSWORD": "b"})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["schemaVersion"], 1)

    def test_prices_only_ohne_vertrag(self):
        _, out, _ = self.run_main(["--out", "-", "--prices-only"], {"LB_EMAIL": "a", "LB_PASSWORD": "b"})
        self.assertNotIn("contract", json.loads(out))

    def test_ungueltige_contract_id(self):
        code, _, err = self.run_main(["--out", "-"], {"LB_EMAIL": "a", "LB_PASSWORD": "b", "LB_CONTRACT_ID": "abc"})
        self.assertEqual(code, 2)
        self.assertIn("Zahl", err)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        serve.Handler.log_message = lambda *a: None  # Testausgabe ruhig halten
        cls.port = free_port()
        cls.httpd = serve.ThreadingHTTPServer(("127.0.0.1", cls.port), serve.Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        serve._last_fetch.clear()
        serve._failures.clear()

    def req(self, path, body=None, headers=None, method=None, timeout=5):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
        r = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"))
        for k, v in (headers or {"Content-Type": "application/json"}).items():
            r.add_header(k, v)
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read()

    def test_dotenv_wird_nicht_ausgeliefert(self):
        self.assertEqual(self.req("/.env")[0], 404)

    def test_git_wird_nicht_ausgeliefert(self):
        self.assertEqual(self.req("/.git/config")[0], 404)

    def test_quelltext_und_pycache_nicht_gelistet(self):
        self.assertEqual(self.req("/__pycache__/")[0], 404)

    def test_index_und_demo_erreichbar(self):
        self.assertEqual(self.req("/")[0], 200)
        self.assertEqual(self.req("/data/prices_demo.json")[0], 200)

    def test_nur_freigegebene_dateien(self):
        for path in ("/serve.py", "/fetch_prices.py", "/README.md", "/data/", "/data/prices.json",
                     "/.env.example", "/../serve.py", "/%2e%2e/serve.py", "/tests/"):
            self.assertEqual(self.req(path)[0], 404, path)

    def test_fehlversuch_gibt_429_mit_retry_after(self):
        with mock.patch.object(serve.fetch_prices, "fetch_prices",
                               side_effect=RuntimeError("B2C-Login fehlgeschlagen")):
            self.assertEqual(self.req("/api/prices", {"email": "z@b.de", "password": "x"})[0], 502)
            st, body = self.req("/api/prices", {"email": "z@b.de", "password": "x"})
        self.assertEqual(st, 429)
        self.assertIn("Fehlversuche", json.loads(body)["error"])

    def test_api_fehler_ohne_login_bezug_wird_nicht_gedrosselt(self):
        with mock.patch.object(serve.fetch_prices, "fetch_prices",
                               side_effect=RuntimeError("Netzwerkfehler")):
            for _ in range(3):
                self.assertEqual(self.req("/api/prices", {"email": "n@b.de", "password": "x"})[0], 502)

    def test_rate_limit_cache_prueft_passwort(self):
        calls = []

        def fake(email, pw, cid):
            calls.append(pw)
            if pw != "richtig":
                raise RuntimeError("B2C-Login fehlgeschlagen")
            return {"geheim": True}
        with mock.patch.object(serve.fetch_prices, "fetch_prices", side_effect=fake):
            self.assertEqual(self.req("/api/prices", {"email": "a@b.de", "password": "richtig"})[0], 200)
            st, body = self.req("/api/prices", {"email": "a@b.de", "password": "falsch"})
        self.assertNotEqual(st, 200, "falsches Passwort bekommt gecachte Daten: " + body.decode())

    def test_rate_limit_cache_beachtet_contract_id(self):
        def fake(email, pw, cid):
            return {"contract": {"id": cid}}
        with mock.patch.object(serve.fetch_prices, "fetch_prices", side_effect=fake):
            self.req("/api/prices", {"email": "a@b.de", "password": "x", "contractId": 1})
            _, body = self.req("/api/prices", {"email": "a@b.de", "password": "x", "contractId": 2})
        self.assertEqual(json.loads(body)["contract"]["id"], 2)

    def test_unerwarteter_fehler_gibt_json_statt_abbruch(self):
        with mock.patch.object(serve.fetch_prices, "fetch_prices",
                               side_effect=AttributeError("'NoneType' object has no attribute 'get'")), \
             mock.patch.object(serve.traceback, "print_exc"):
            try:
                st, body = self.req("/api/prices", {"email": "a@b.de", "password": "x"})
            except Exception as e:  # Verbindung abgerissen
                self.fail(f"keine HTTP-Antwort: {e!r}")
        self.assertGreaterEqual(st, 500)
        json.loads(body)

    def test_contract_id_kein_int(self):
        with mock.patch.object(serve.fetch_prices, "fetch_prices", return_value={}):
            try:
                st, _ = self.req("/api/prices", {"email": "a@b.de", "password": "x", "contractId": "abc"})
            except Exception as e:
                self.fail(f"keine HTTP-Antwort: {e!r}")
        self.assertEqual(st, 400)

    def test_negative_content_length_haengt_nicht(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        s.sendall(b"POST /api/prices HTTP/1.1\r\nHost: x\r\nContent-Length: -1\r\n\r\n{}")
        try:
            data = s.recv(200)
        except socket.timeout:
            self.fail("Server hängt bei Content-Length: -1")
        finally:
            s.close()
        self.assertTrue(data.startswith(b"HTTP/"))

    def test_fehlversuche_werden_gedrosselt(self):
        n = []

        def fake(email, pw, cid):
            n.append(1)
            raise RuntimeError("B2C-Login fehlgeschlagen")
        with mock.patch.object(serve.fetch_prices, "fetch_prices", side_effect=fake):
            for _ in range(10):
                self.req("/api/prices", {"email": "a@b.de", "password": "x"})
        self.assertLess(len(n), 10, "10 Fehl-Logins in Folge gehen alle an LichtBlick durch (Kontosperre-Risiko)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
