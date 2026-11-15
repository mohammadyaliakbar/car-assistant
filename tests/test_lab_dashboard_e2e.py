"""تست مرورگر واقعی داشبورد (اگر Playwright و Chromium نصب باشد؛ وگرنه skip)."""

import base64
import re
import threading

import pytest

pw = pytest.importorskip("playwright.sync_api")

from app.simulation.lab_server import LabApp, make_server  # noqa: E402


def _open_page(tmp_path, roads_db, setup=None):
    app = LabApp(history_dir=tmp_path / "hist", roads_db=roads_db)
    srv = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:
            pytest.skip(f"Chromium در دسترس نیست: {str(exc)[:60]}")
        pg = browser.new_page(viewport={"width": 1400, "height": 950})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text)
              if m.type == "error" and "ERR_" not in m.text and "tile" not in m.text.lower()
              and "Failed to load resource" not in m.text else None)
        if setup:
            setup(pg)
        pg.goto(f"http://127.0.0.1:{srv.server_address[1]}/")
        pg.wait_for_selector("#scn option", state="attached")
        yield pg, errors
        browser.close()
    app.close()
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def page(tmp_path):
    # دیتابیس جاده‌ی واقعی کاربر (data/roads_tehran.sqlite) نباید روی تست اثر بگذارد
    yield from _open_page(tmp_path, tmp_path / "no_roads.sqlite")


@pytest.fixture
def page_roads(tmp_path):
    from lab_roads_fixture import make_grid_db
    db = tmp_path / "roads.sqlite"
    make_grid_db(db)
    yield from _open_page(tmp_path, db)


TILE_HOSTS = r"https://(tile\.openstreetmap\.org|[a-c]\.basemaps\.cartocdn\.com|server\.arcgisonline\.com)/.*"
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


@pytest.fixture
def page_no_tiles(tmp_path):
    """همه‌ی ارائه‌دهنده‌های کاشی خراب‌اند (مثل اینترنت قطع یا مسدود)."""
    yield from _open_page(tmp_path, tmp_path / "no_roads.sqlite",
                          lambda pg: pg.route(re.compile(TILE_HOSTS), lambda r: r.abort()))


@pytest.fixture
def page_tiles_ok(tmp_path):
    yield from _open_page(
        tmp_path, tmp_path / "no_roads.sqlite",
        lambda pg: pg.route(re.compile(TILE_HOSTS),
                            lambda r: r.fulfill(status=200, content_type="image/png", body=PNG_1X1)))


def test_dashboard_runs_stoppage_scenario_and_shows_detection(page):
    pg, errors = page
    pg.select_option("#scn", "highway_stoppage")
    pg.click("#speeds button[data-x='10']")
    pg.click("#b-start")
    pg.wait_for_function("document.getElementById('emerg').innerText.includes('فاز')", timeout=30000)
    assert "Detector: stoppage" in pg.inner_text("#alerts")
    assert "خاموش" in pg.inner_text("#chips")
    pg.wait_for_function("document.getElementById('summary').innerText.includes('تشخیص واقعی')",
                         timeout=30000)
    pg.click("#b-reset")
    pg.wait_for_function("document.getElementById('t-state').innerText==='idle'")
    assert "هشداری نیست" in pg.inner_text("#alerts")
    assert errors == []


def test_dashboard_pause_resume_and_gps_loss(page):
    pg, errors = page
    pg.select_option("#scn", "gps_signal_loss")
    pg.click("#speeds button[data-x='10']")
    pg.click("#b-start")
    pg.wait_for_function("document.getElementById('t-time').textContent>='00:10'", timeout=20000)
    pg.click("#b-pause")
    pg.wait_for_function("document.getElementById('t-state').innerText==='paused'")
    t = pg.inner_text("#t-time")
    pg.wait_for_timeout(1000)
    assert pg.inner_text("#t-time") == t
    pg.click("#b-resume")
    pg.wait_for_function("document.getElementById('chips').innerText.includes('Disconnected')",
                         timeout=30000)
    assert "قدیمی" in pg.inner_text("#map-info")          # موقعیت قدیمی «زنده» نشان داده نمی‌شود
    assert errors == []


def test_history_replay_and_report_page(page):
    pg, errors = page
    base = pg.url.rstrip("/")
    pg.select_option("#scn", "ecu_trouble_code")
    pg.click("#speeds button[data-x='10']")
    pg.click("#b-start")
    pg.wait_for_function("document.getElementById('summary').innerText.includes('تشخیص واقعی')",
                         timeout=30000)
    pg.wait_for_function("document.querySelectorAll('#hist option[value]').length>0 "
                         "&& document.getElementById('hist').value!==''", timeout=10000)
    assert "کد خطای ECU" in pg.inner_text("#hist")

    # بازپخش همان اجرا از تاریخچه
    pg.click("#b-reset")
    pg.wait_for_function("document.getElementById('t-state').innerText==='idle'")
    pg.click("#b-replay")
    pg.wait_for_function("!document.getElementById('rb').hidden", timeout=10000)
    pg.wait_for_function("document.getElementById('events').innerText.includes('new_trouble_code')",
                         timeout=30000)
    assert "P0301" in pg.inner_text("#events")
    pg.wait_for_function("document.getElementById('t-state').innerText==='finished'", timeout=30000)
    pg.wait_for_timeout(1200)                                   # بازپخش اجرای تازه‌ای نمی‌سازد
    assert pg.evaluate("document.querySelectorAll('#hist option').length") == 1

    # صفحه‌ی گزارش
    pg.goto(base + "/report.html")
    pg.wait_for_function("document.getElementById('cards').innerText.includes('سناریوهای اجراشده')")
    assert "کد خطای ECU" in pg.inner_text("#scn")
    assert "تشخیص واقعی توسط Detector" in pg.inner_text("#runs")
    with pg.expect_download() as dl:
        pg.click("text=CSV همه‌ی اجراها")
    assert dl.value.suggested_filename == "lab-report-run.csv"

    # باز کردن مستقیم بازپخش با لینک گزارش
    pg.goto(pg.locator("a:text('بازپخش')").first.get_attribute("href").replace("/?", base + "/?"))
    pg.wait_for_function("!document.getElementById('rb').hidden", timeout=10000)
    assert errors == []


def test_crash_scenario_driver_gate_in_browser(page):
    pg, errors = page
    pg.select_option("#scn", "crash_detection")
    assert pg.is_visible("#crash-opts")                       # گزینه‌های راننده فقط برای تصادف
    pg.fill("#to", "90")
    pg.click("#speeds button[data-x='5']")
    pg.click("#b-start")
    pg.wait_for_function("!document.getElementById('gate').hidden", timeout=40000)
    pg.wait_for_function("document.getElementById('gate-t').innerText.includes('در انتظار')",
                         timeout=20000)
    assert pg.is_enabled("#g-yes") and pg.is_visible("#cg-wrap")   # نمودار شتاب گوشی
    assert "crash_suspected" in pg.inner_text("#events")
    assert "Detector: crash" in pg.inner_text("#alerts")
    pg.screenshot(path="/tmp/dash_crash.png", full_page=True)
    pg.click("#g-yes")                                        # تأیید واقعی راننده با کلیک
    pg.wait_for_function("document.getElementById('gate-t').innerText.includes('تأیید کرد')",
                         timeout=10000)
    assert "شما" in pg.inner_text("#gate-t")
    assert pg.is_disabled("#g-yes")
    pg.wait_for_function("document.getElementById('summary').innerText.includes('تشخیص واقعی')",
                         timeout=40000)
    assert errors == []


def test_crash_decline_via_button_and_other_scenarios_hide_crash_ui(page):
    pg, errors = page
    pg.select_option("#scn", "crash_detection")
    pg.click("#speeds button[data-x='10']")
    pg.click("#b-start")
    pg.wait_for_function("document.getElementById('gate-t').innerText.includes('در انتظار')",
                         timeout=40000)
    pg.click("#g-no")
    pg.wait_for_function("document.getElementById('gate-t').innerText.includes('رد کرد')",
                         timeout=10000)
    pg.click("#b-reset")
    pg.wait_for_function("document.getElementById('t-state').innerText==='idle'")
    assert pg.is_hidden("#gate") and pg.is_hidden("#cg-wrap")
    pg.select_option("#scn", "normal_driving")
    assert pg.is_hidden("#crash-opts")
    assert errors == []


def test_map_falls_back_through_providers_when_no_tile_loads(page_no_tiles):
    pg, errors = page_no_tiles
    pg.wait_for_function("document.getElementById('tile-st').innerText.includes('فقط جاده')", timeout=40000)
    assert "ندارد" in pg.inner_text("#tile-st")               # وضعیت صریح، نه نقشه‌ی سیاه بی‌توضیح
    assert errors == []


def test_map_keeps_provider_when_tiles_load(page_tiles_ok):
    pg, errors = page_tiles_ok
    pg.wait_for_function("document.getElementById('tile-st').innerText.includes('کاشی بارگذاری شد')",
                         timeout=20000)
    pg.wait_for_timeout(1500)
    text = pg.inner_text("#tile-st")
    assert "OpenStreetMap" in text and "خطا" not in text and "فقط جاده" not in text
    pg.select_option("#basemap", "none")                       # انتخاب دستی هم کار می‌کند
    assert "ندارد" in pg.inner_text("#tile-st")
    assert errors == []


def test_route_follows_roads_and_roads_overlay_draws_offline(page_roads):
    from lab_roads_fixture import LAT0, LON0, STEP, node
    pg, errors = page_roads
    start = f"{LAT0 + 40 * STEP + 0.00003}, {LON0 + 5 * STEP + 0.0002}"
    dest = "%s, %s" % node(40, 55)
    pg.fill("#c-start", start)
    pg.fill("#c-dest", dest)
    pg.dispatch_event("#c-dest", "change")
    pg.wait_for_function("document.getElementById('route-st').innerText.includes('مسیر روی جاده‌ها:')",
                         timeout=15000)
    assert pg.evaluate("routeLine.getLatLngs().length") > 20      # نه یک خط مستقیم دو نقطه‌ای

    # اجرای واقعی روی همان مسیر: ردّ خودرو دنبال همان نقطه‌های جاده است
    pg.select_option("#scn", "normal_driving")
    pg.select_option("#c-type", "urban")
    pg.click("#speeds button[data-x='10']")
    pg.click("#b-start")
    pg.wait_for_function("trail.getLatLngs().length>40", timeout=30000)
    assert pg.evaluate("""() => {
        const R = routeLine.getLatLngs(), T = trail.getLatLngs();
        const dist = (p, a, b) => {
            const ax = b.lng - a.lng, ay = b.lat - a.lat, L = ax * ax + ay * ay;
            let t = L ? ((p.lng - a.lng) * ax + (p.lat - a.lat) * ay) / L : 0;
            t = Math.max(0, Math.min(1, t));
            return Math.hypot(p.lng - (a.lng + t * ax), p.lat - (a.lat + t * ay));
        };
        return T.length > 40 && T.every(p => R.some((q, i) => i && dist(p, R[i - 1], q) < 2e-5));
    }""")

    # جاده‌های آفلاین (بدون هیچ کاشی/اینترنت) روی نقشه کشیده می‌شوند
    pg.evaluate("map.setView([35.72, 51.32], 15)")
    pg.wait_for_function("roadsLayer.getLayers().length>0", timeout=10000)
    assert "جاده" in pg.inner_text("#road-st")
    pg.uncheck("#roads-on")
    pg.wait_for_function("roadsLayer.getLayers().length===0", timeout=5000)
    assert errors == []


def test_route_warning_is_shown_when_roads_cannot_be_used(page_roads):
    pg, errors = page_roads
    pg.fill("#c-start", "35.70, 51.30")
    pg.fill("#c-dest", "34.0, 51.3")                               # خارج از محدوده‌ی دیتابیس
    pg.dispatch_event("#c-dest", "change")
    pg.wait_for_function("document.getElementById('route-st').innerText.includes('خط مستقیم')",
                         timeout=15000)
    assert "خارج از محدوده" in pg.inner_text("#route-st")
    assert errors == []
