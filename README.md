# ⚡ LichtBlick Dynamic – Strompreise für zuhause

Kleine, **komplett lokale** Webansicht auf die Viertelstunden-Strompreise des
dynamischen Tarifs **„ÖkoStrom Dynamic“** von LichtBlick – plus Python-Bibliothek
und CLI, um die Preise in Home Assistant, ein HEMS oder eigene Skripte zu holen.

Jeder nutzt **seine eigenen LichtBlick-Zugangsdaten** (die der LichtBlick-App).
Es läuft nichts auf Fremdservern; Daten gehen nur an LichtBlick selbst.

- Nur Python-Standardbibliothek, **Python ≥ 3.9** (unter Windows zusätzlich
  `pip install tzdata`)
- Inoffizielles Projekt, nicht von LichtBlick. Die API kann sich jederzeit ändern.

## Schnellstart

```bash
python3 serve.py            # startet http://127.0.0.1:8080
```

Browser öffnen, mit E-Mail und Passwort der LichtBlick-App einloggen, fertig.

- **Reiter „Preise“**: aktueller Preis, Ø, günstigstes/teuerstes Zeitfenster,
  Verlauf heute und morgen (Berliner Zeit, Tooltip beim Überfahren).
- **Reiter „Vertrag“**: Status, Lieferbeginn, Laufzeit, Kündigungsfrist,
  Abschlag, Zahlungsart sowie die **kompletten Rohdaten** der Antwort.
- Ohne Login ansehen: `http://127.0.0.1:8080/?demo=1` (Beispieldaten).
- Port/Adresse ändern: `LB_PORT=8090 python3 serve.py`.

Vor dem ersten Liefertag liefert die API noch keine Preise. Die Seite zeigt
dann den Lieferbeginn an. Die Preise für den Folgetag erscheinen nachmittags.

## CLI und Python-Bibliothek

```bash
cp .env.example .env        # LB_EMAIL / LB_PASSWORD eintragen
python3 fetch_prices.py                         # -> data/prices.json
python3 fetch_prices.py --out -                 # JSON auf stdout
python3 fetch_prices.py --out - --prices-only   # ohne Vertragsdaten (kleiner)
```

Statt `.env` gehen auch Umgebungsvariablen (`LB_EMAIL`, `LB_PASSWORD`,
optional `LB_CONTRACT_ID`).

```python
from fetch_prices import fetch_prices
daten = fetch_prices("deine@mail.de", "PASSWORT")
daten["currentSlot"]            # {"time": ..., "valueInCents": 21.5, "durationMinutes": 15}
daten["tomorrow"]["points"]     # [{"time": "2026-11-02T00:00:00+01:00", "valueInCents": 19.8}, ...]
```

### Ausgabeformat (`schemaVersion: 1`)

| Feld | Bedeutung |
|---|---|
| `fetchedAt` | Abrufzeit (ISO 8601, Berliner Zeit) |
| `currentSlot` | Preis-Slot, der *jetzt* gilt – lokal aus den Punkten berechnet, oder `null` |
| `currentPrice` | „Aktueller Preis“ laut API (geglättete Kurve; vermutlich Stundenmittel), oder `null` |
| `today` / `tomorrow` | `points` (Liste aus `time` + `valueInCents`), `averageInCents`, `minRange`/`maxRange` |
| `contract` | `id`, `name`, `address` (nur PLZ + Ort), `startDate`, `details` (Status, Laufzeiten, Abschlag …) |

Preise in **ct/kWh**. Felder, die die API nicht liefert, sind `null`. Neue
Felder können dazukommen. Bei inkompatiblen Änderungen wird `schemaVersion`
erhöht.

## Home Assistant

Projektordner nach `/config/lichtblick-preise/` kopieren, dort `.env` anlegen
und in `configuration.yaml` eintragen:

```yaml
command_line:
  - sensor:
      name: LichtBlick Strompreis
      unique_id: lichtblick_strompreis
      command: "python3 /config/lichtblick-preise/fetch_prices.py --out - --prices-only"
      command_timeout: 60
      scan_interval: 900
      unit_of_measurement: "ct/kWh"
      value_template: "{{ value_json.currentSlot.valueInCents if value_json.currentSlot else none }}"
      json_attributes: [today, tomorrow, currentPrice, fetchedAt]
```

Der Sensor zeigt den Preis des laufenden Slots. Die Attribute `today` und
`tomorrow` enthalten alle Punkte, etwa für ApexCharts oder für Automationen
nach dem günstigsten Zeitfenster. Achtung: Das Beispiel ist noch nicht in einer
echten Home-Assistant-Installation getestet.

Für andere Systeme (HEMS, Wallbox-Steuerung, eigene Skripte) gilt dasselbe:
`fetch_prices.py --out -` aufrufen und das JSON auswerten, oder `fetch_prices()`
direkt importieren.

## So funktioniert LichtBlicks Preis-API

Die offizielle App (iOS/Android) nutzt ein Apollo-GraphQL-Backend:

| | |
|---|---|
| **API** | `POST https://graph.lichtblick.de/` |
| **Pflicht-Header** | `apollographql-client-name`, `apollographql-client-version`, `Content-Type: application/json` |
| **Login** | Azure AD B2C, ROPC-Flow: `https://anmelden.lichtblick.de/lichtblickitb2cprod.onmicrosoft.com/oauth2/v2.0/token?p=b2c_1a_ropc_signin` mit `client_id=c6079bc3-9572-4da7-abb7-b69694239976` |
| **Auth** | `Authorization: Bearer <JWT>` (1 h gültig, Refresh-Token wird bei jeder Erneuerung rotiert) |

Verwendete Queries (Texte in `fetch_prices.py`, aus der Android-App übernommen):

- `EnergyPriceCurves`: Preispunkte (`time`, `valueInCents`) für ein Zeitfenster.
  `priceCurveAggregation: DAY` beschreibt nur das Abfragefenster, nicht die Schrittweite.
- `SmoothedEnergyPriceCurves`: geglättete Kurve und `currentPricePoint`
- `SelectableContracts`: Verträge des Kontos
- `ContractOverview`: aus mehreren App-Queries zusammengesetzte Vertragsübersicht

Noch nicht mit echten Preisdaten bestätigt (Stand: vor dem ersten Liefertag):

- Schrittweite der Punkte. Laut AGB 15 Minuten (Spotpreis im Viertelstundentakt).
- Ob `valueInCents` netto oder brutto ist.
- Einheit der App-Werte für Arbeits- und Grundpreis.

**Warum ein lokaler Server?** Das API erlaubt CORS nur für `*.lichtblick.de`.
Ein Browser auf `file://` oder einem anderen Origin kommt nicht direkt dran.
`serve.py` reicht die Anfrage als gleichorigiger Proxy weiter.

## Verwandte Projekte und Alternativen

Stand 21.10.2026 ist kein anderes Open-Source-Projekt bekannt, das die
LichtBlick-Kundenpreise abholt. Geprüft wurden GitHub, die HACS-Standardliste,
die Tarif-Vorlagen von [evcc](https://github.com/evcc-io/evcc/tree/master/templates/definition/tariff)
und das Home-Assistant-Forum. Hinweise auf weitere Projekte gern als Issue.

- **SMA Sunny Home Manager 2.0 / Sunny Portal**: Laut
  [SMA-Handbuch](https://manuals.sma.de/SPortal/en-US/16129800843.html) zeigt das
  Portal Tarife des Partners LichtBlick SE an. Das ist der offizielle Weg für
  SMA-Anlagen.
- **[evcc](https://docs.evcc.io/en/features/dynamic-prices)**: Es gibt keine
  LichtBlick-Vorlage, aber eingebaute Anbieter wie Tibber, Ostrom, Octopus und
  lekker. Für LichtBlick lässt sich der Börsenpreis
  ([`energy-charts-api`](https://docs.evcc.io/de/tariffs/energy-charts-market-price),
  `entsoe`) mit eigenem Aufschlag nutzen. Er bildet aber nicht die echten
  LichtBlick-Preise ab.
- **Home Assistant, Börsenpreis statt Kundenpreis**:
  [mampfes/ha_epex_spot](https://github.com/mampfes/ha_epex_spot) (EPEX Spot,
  mit konfigurierbarem Aufschlag) und
  [custom-components/nordpool](https://github.com/custom-components/nordpool).
- **Vorbild bei einem anderen Anbieter**: Integrationen für Ostrom, z. B.
  [melmager/ha_ostrom](https://github.com/melmager/ha_ostrom).

Der Unterschied zu den Börsenpreis-Lösungen: Dieses Projekt holt die Preise, die
LichtBlick für den eigenen Vertrag ausweist, statt sie aus dem Börsenpreis
nachzurechnen.

## Sicherheit

- `serve.py` lauscht standardmäßig nur auf `127.0.0.1` und liefert nur
  `index.html` und die Demo-Daten aus, nie `.env`, `.git` oder Quelltext.
- Im LAN freigeben (`LB_HOST=0.0.0.0`) nur in einem vertrauenswürdigen Netz:
  Die Verbindung ist HTTP, Zugangsdaten gehen unverschlüsselt durchs Netz.
- Zugangsdaten liegen im **localStorage des Browsers (Klartext)** bzw. in `.env`.
  Gespeichert wird erst nach erfolgreichem Login. „abmelden“ löscht sie.
- Token-Cache unter `~/.lichtblick/token-<hash>.json` (Rechte 0600), an das
  Passwort gebunden: Mit falschem Passwort gibt es keine gecachten Daten.
- Fehl-Logins werden pro E-Mail zunehmend gebremst (HTTP 429), damit LichtBlick
  das Konto nicht sperrt. Erfolgreiche Abrufe werden 2 Minuten zwischengespeichert.
- Zugriff nur auf den eigenen Vertrag. Preisangaben ohne Gewähr.

## Tests

```bash
python3 -m unittest discover -s tests -v          # offline, ohne Netz
npm install --no-save playwright-core && npx playwright install chromium
node tests/ui_test.cjs                            # Browser-Tests der Seite
```

Beide Testsuiten sprechen nie mit LichtBlick. Die API wird gemockt bzw. im
Browser abgefangen.

## Dateien

| Datei | Zweck |
|---|---|
| `index.html` | Preis- und Vertragsansicht (eine Datei, Vanilla JS) |
| `serve.py` | Lokaler Server + API-Proxy, Standard-Bind 127.0.0.1 |
| `fetch_prices.py` | Bibliothek + CLI (Login, Verträge, Preisabruf, JSON) |
| `data/prices_demo.json` | Beispieldaten im 15-Min-Takt für `?demo=1` |
| `tests/` | Offline- und Browser-Tests |
| `.env.example` | Vorlage für den CLI-Betrieb |

## Lizenz

[MIT](LICENSE) © 2026 chloepriceless.

Du darfst das Projekt frei nutzen, ändern und weitergeben, auch kommerziell und
eingebaut in andere Projekte. Bedingung: Der Copyright-Hinweis und der
Lizenztext bleiben in allen Kopien und wesentlichen Teilen erhalten. Über einen
Link zurück zu diesem Repo freue ich mich.

Inoffizielles Projekt, nicht verbunden mit LichtBlick SE. „LichtBlick“ und
„ÖkoStrom Dynamic“ werden nur zur Beschreibung der Kompatibilität genannt.
