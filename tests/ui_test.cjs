// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 chloepriceless – https://github.com/chloepriceless/Lichtblick-Lokal-Preis
// Browser-Tests für index.html (Playwright). /api/prices wird im Browser abgefangen –
// es geht nichts an LichtBlick. Startet serve.py selbst auf einem freien Port.
//
//   npm install --no-save playwright-core && npx playwright install chromium
//   node tests/ui_test.cjs            (Screenshots landen in $UI_SHOTS, falls gesetzt)
const { chromium } = require(process.env.PLAYWRIGHT_CORE || "playwright-core");
const { spawn } = require("child_process");
const net = require("net");
const path = require("path");
const SHOTS = process.env.UI_SHOTS || null;

function freePort() {
  return new Promise(res => { const s = net.createServer().listen(0, "127.0.0.1", () => { const p = s.address().port; s.close(() => res(p)); }); });
}
async function startServer() {
  const port = await freePort();
  const proc = spawn("python3", [path.join(__dirname, "..", "serve.py")], { env: { ...process.env, LB_PORT: String(port) }, stdio: "ignore" });
  const base = `http://127.0.0.1:${port}/`;
  for (let i = 0; i < 50; i++) {
    try { if ((await fetch(base + "api/health")).ok) return { proc, base }; } catch {}
    await new Promise(r => setTimeout(r, 100));
  }
  proc.kill();
  throw new Error("serve.py startet nicht");
}

function day(dateStr, offsetH, n, f) {
  // n Viertelstunden ab Mitternacht (lokal Berlin) – offsetH = UTC-Offset der Mitternacht
  const start = Date.parse(`${dateStr}T00:00:00+0${offsetH}:00`);
  return Array.from({ length: n }, (_, i) => ({ time: new Date(start + i * 900e3).toISOString(), valueInCents: f(i) }));
}
const fixture = (today, tomorrow, extra = {}) => ({
  schemaVersion: 1, fetchedAt: "2026-11-02T14:00:00+0100", timezone: "Europe/Berlin",
  contract: { id: 1, name: "ÖkoStrom Dynamic", address: null }, currentPrice: 21.3,
  today: { points: today, averageInCents: null, maxRange: null, minRange: null },
  tomorrow: { points: tomorrow, averageInCents: 0, maxRange: null, minRange: null }, ...extra,
});

const results = [];
const check = (name, ok, info = "") => { results.push({ name, ok, info }); console.log(`${ok ? "PASS" : "FAIL"}  ${name}${info ? "  – " + info : ""}`); };

let server;
(async () => {
  server = await startServer();
  const BASE = server.base;
  const browser = await chromium.launch();
  const mk = async (opts = {}) => {
    const ctx = await browser.newContext({ timezoneId: "Europe/Berlin", locale: "de-DE", ...opts });
    const page = await ctx.newPage();
    const errs = [];
    page.on("pageerror", e => errs.push(e.message));
    page.on("console", m => m.type() === "error" && errs.push(m.text()));
    return { ctx, page, errs };
  };

  // 1) Demo
  {
    const { ctx, page, errs } = await mk();
    await page.goto(BASE + "?demo=1");
    await page.waitForSelector("#chart-today svg");
    const bars = await page.locator("#chart-today rect.bar").count();
    check("Demo: 96 Balken heute", bars === 96, `bars=${bars}`);
    check("Demo: keine JS-Fehler", errs.length === 0, errs.join(" | "));
    if (SHOTS) await page.screenshot({ path: SHOTS + "/demo.png", fullPage: true });
    // Tooltip
    await page.locator("#chart-today rect.bar").nth(40).hover();
    await page.waitForTimeout(300);
    const op = await page.$eval("#tooltip", el => getComputedStyle(el).opacity + "|" + el.textContent);
    check("Tooltip erscheint beim Hover", !op.startsWith("0|"), `opacity|text=${op}`);
    await ctx.close();
  }

  // 2) Login-Flow + localStorage + Auto-Refresh nach frischem Login
  {
    const { ctx, page, errs } = await mk();
    await page.clock.install({ time: new Date("2026-11-02T14:00:00+01:00") });
    let calls = 0;
    await page.route("**/api/prices", r => { calls++; r.fulfill({ json: fixture(day("2026-11-02", 1, 96, i => 20 + Math.sin(i / 8) * 8), []) }); });
    await page.goto(BASE);
    await page.fill("#email", "test@example.org");
    await page.fill("#password", "geheim123");
    await page.click("#btn-login");
    await page.waitForSelector("#chart-today svg");
    const ls = await page.evaluate(() => localStorage.getItem("lichtblick-preise/creds"));
    check("Creds nach erfolgreichem Login gespeichert", !!ls && ls.includes("test@example.org"));
    const loginVisible = await page.isVisible("#login");
    check("Login-Karte wird nach erfolgreichem Login ausgeblendet", !loginVisible);
    const avg = await page.locator(".kpi", { hasText: "Heute Ø" }).textContent();
    check("Ø heute wird aus Punkten berechnet, wenn API keinen Ø liefert (nicht 0,00)", !/0,00/.test(avg), avg);
    await page.clock.runFor(16 * 60 * 1000);
    await page.waitForTimeout(200);
    check("Auto-Refresh alle 15 min auch nach frischem Login", calls >= 2, `API-Aufrufe=${calls}`);
    check("Login-Flow: keine JS-Fehler", errs.length === 0, errs.join(" | "));
    await ctx.close();
  }

  // 3) Gespeicherte Creds -> Auto-Refresh nach Reload
  {
    const { ctx, page } = await mk();
    await page.clock.install({ time: new Date("2026-11-02T14:00:00+01:00") });
    let calls = 0;
    await page.route("**/api/prices", r => { calls++; r.fulfill({ json: fixture(day("2026-11-02", 1, 96, () => 20), []) }); });
    await page.goto(BASE);
    await page.evaluate(() => localStorage.setItem("lichtblick-preise/creds", JSON.stringify({ email: "a@b.de", password: "x" })));
    await page.reload();
    await page.waitForSelector("#chart-today svg");
    await page.clock.runFor(16 * 60 * 1000);
    await page.waitForTimeout(200);
    check("Auto-Refresh nach Reload mit gespeicherten Creds", calls >= 2, `calls=${calls}`);
    await ctx.close();
  }

  // 4) Fehlerfall 502 (falsches Passwort)
  {
    const { ctx, page } = await mk();
    await page.route("**/api/prices", r => r.fulfill({ status: 502, json: { error: "B2C-Login fehlgeschlagen: invalid_grant: AADB2C90225" } }));
    await page.goto(BASE);
    await page.fill("#email", "a@b.de"); await page.fill("#password", "falsch");
    await page.click("#btn-login");
    await page.waitForSelector("#login-error:not(.hidden)");
    const msg = await page.textContent("#login-error");
    const btn = await page.isEnabled("#btn-login");
    const ls = await page.evaluate(() => localStorage.getItem("lichtblick-preise/creds"));
    check("Fehlermeldung bei falschem Login sichtbar", msg.includes("fehlgeschlagen"), msg);
    check("Button nach Fehler wieder aktiv", btn);
    check("Falsche Creds werden nicht dauerhaft gespeichert", ls === null, `localStorage=${ls ? "gesetzt" : "leer"}`);
    await ctx.close();
  }

  // 5) Server nicht erreichbar / kaputte Antwort (Verbindung abgerissen)
  {
    const { ctx, page } = await mk();
    await page.route("**/api/prices", r => r.abort("connectionreset"));
    await page.goto(BASE);
    await page.fill("#email", "a@b.de"); await page.fill("#password", "x");
    await page.click("#btn-login");
    await page.waitForSelector("#login-error:not(.hidden)", { timeout: 5000 }).catch(() => {});
    const msg = await page.textContent("#login-error");
    check("Netzfehler -> verständliche Meldung", !!msg && msg.length > 0, msg);
    await ctx.close();
  }

  // 6) Leere Daten (Vertrag noch nicht aktiv – Stand heute live)
  {
    const { ctx, page, errs } = await mk();
    await page.route("**/api/prices", r => r.fulfill({ json: fixture([], [], { currentPrice: null }) }));
    await page.goto(BASE);
    await page.fill("#email", "a@b.de"); await page.fill("#password", "x");
    await page.click("#btn-login");
    await page.waitForSelector("#notice .empty");
    check("Leerzustand: Hinweis 'Noch keine Stundenpreise'", (await page.textContent("#notice")).includes("Noch keine"));
    check("Leerzustand: keine KPIs mit Unsinn", (await page.locator(".kpi").count()) === 0);
    check("Leerzustand: keine JS-Fehler", errs.length === 0, errs.join(" | "));
    await ctx.close();
  }

  // 7) Zeitumstellung (25.10. = 100 Viertelstunden) + negative Preise + Browser in anderer Zeitzone
  for (const tz of ["Europe/Berlin", "America/New_York"]) {
    const { ctx, page, errs } = await mk({ timezoneId: tz });
    await page.clock.install({ time: new Date("2026-10-25T12:00:00+01:00") });
    await page.route("**/api/prices", r => r.fulfill({ json: fixture(
      day("2026-10-25", 2, 100, i => (i > 44 && i < 60 ? -5.2 : 18 + i / 10)), []) }));
    await page.goto(BASE);
    await page.fill("#email", "a@b.de"); await page.fill("#password", "x");
    await page.click("#btn-login");
    await page.waitForSelector("#chart-today svg");
    const n = await page.locator("#chart-today rect.bar").count();
    const labels = await page.$$eval("#chart-today svg text.tick", ts => ts.map(t => t.textContent).join(","));
    const cheap = await page.locator(".kpi", { hasText: "Heute günstigstes" }).textContent();
    check(`DST-Tag ${tz}: 100 Balken`, n === 100, `n=${n}`);
    check(`DST-Tag ${tz}: Achse 00–21 Uhr alle 3 h (Berliner Zeit)`, labels === "00,03,06,09,12,15,18,21", labels);
    check(`DST-Tag ${tz}: negativer Preis als günstigster`, cheap.includes("-5,20"), cheap);
    check(`DST-Tag ${tz}: keine JS-Fehler`, errs.length === 0, errs.join(" | "));
    if (SHOTS && tz === "Europe/Berlin") await page.screenshot({ path: SHOTS + "/dst_negativ.png", fullPage: true });
    await ctx.close();
  }

  // 8) Reiter Vertrag: alle Felder + Rohdaten
  {
    const { ctx, page, errs } = await mk();
    const d = fixture([], [], { currentPrice: null });
    d.contract = { id: 4711, name: "ÖkoStrom Dynamic", address: { city: "Beispielstadt", postCode: "12345" }, startDate: "2026-11-01",
      details: { status: "WAITING", statusReason: "ACTIVATING", createdAt: "2026-10-08", productActiveFrom: "2026-10-08",
        startDate: "2026-11-01", endDate: null, terminationDate: null, revocationDate: null,
        minimumRuntime: { value: 0, unit: "MONTHS" }, terminationPeriod: { value: 14, unit: "DAYS" }, fixedPriceMonths: 0,
        workingPrice: null, workingPriceNt: null, yearlyBasePrice: null, meterNumber: "<script>x</script>",
        deposit: { grossAmount: 87, firstDueDate: "2026-11-15" }, nextDeposit: null, paymentMethod: "DIRECT_DEBIT" } };
    await page.route("**/api/prices", r => r.fulfill({ json: d }));
    await page.goto(BASE);
    await page.fill("#email", "a@b.de"); await page.fill("#password", "x");
    await page.click("#btn-login");
    await page.waitForSelector("#dash:not(.hidden)");
    check("Reiter: Preise ist Standard", await page.isVisible("#view-preise") && !(await page.isVisible("#view-vertrag")));
    await page.click(".tabs button[data-tab=vertrag]");
    const txt = await page.textContent("#view-vertrag");
    check("Vertrag: Status übersetzt", txt.includes("in Vorbereitung · Belieferung wird eingerichtet"));
    check("Vertrag: Lieferbeginn formatiert", txt.includes("01.11.2026"));
    check("Vertrag: Kündigungsfrist", txt.includes("14 Tage"));
    check("Vertrag: Abschlag in €", /87,00\s€ ab 15\.11\.2026/.test(txt), txt.match(/Abschlag[^Z]*/)?.[0]);
    check("Vertrag: Zahlungsart", txt.includes("Lastschrift"));
    check("Vertrag: fehlende Werte als –", (await page.locator("#contract-terms dd", { hasText: "–" }).count()) >= 3);
    check("Vertrag: HTML in Daten wird escaped", (await page.locator("#view-vertrag script").count()) === 0 && txt.includes("<script>x</script>"));
    await page.click("#view-vertrag summary");
    const raw = await page.textContent("#raw-json");
    check("Rohdaten: komplette JSON-Antwort", JSON.parse(raw).contract.id === 4711);
    await page.reload();
    await page.waitForSelector("#dash:not(.hidden)");
    check("Reiter bleibt nach Reload (#vertrag)", await page.isVisible("#view-vertrag"));
    check("Vertrag: keine JS-Fehler", errs.length === 0, errs.join(" | "));
    if (SHOTS) await page.screenshot({ path: SHOTS + "/vertrag.png", fullPage: true });
    await ctx.close();
  }

  // 9) Handy-Breite
  {
    const { ctx, page } = await mk({ viewport: { width: 375, height: 800 } });
    await page.goto(BASE + "?demo=1");
    await page.waitForSelector("#chart-today svg");
    const ov = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    check("Mobil 375px: kein horizontales Scrollen", ov <= 0, `overflow=${ov}px`);
    if (SHOTS) await page.screenshot({ path: SHOTS + "/mobil.png", fullPage: true });
    await ctx.close();
  }

  // 10) Logout
  {
    const { ctx, page } = await mk();
    await page.route("**/api/prices", r => r.fulfill({ json: fixture([], []) }));
    await page.goto(BASE);
    await page.evaluate(() => localStorage.setItem("lichtblick-preise/creds", JSON.stringify({ email: "a@b.de", password: "x" })));
    await page.reload();
    await page.click("#btn-logout");
    await page.waitForLoadState();
    const ls = await page.evaluate(() => localStorage.getItem("lichtblick-preise/creds"));
    check("Logout löscht Creds", ls === null);
    await ctx.close();
  }

  await browser.close();
  server.proc.kill();
  const f = results.filter(r => !r.ok).length;
  console.log(`\n${results.length - f}/${results.length} bestanden`);
  process.exit(f ? 1 : 0);
})().catch(e => { console.error(e); server?.proc.kill(); process.exit(1); });
