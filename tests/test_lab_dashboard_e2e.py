"""تست مرورگر واقعی داشبورد (اگر Playwright و Chromium نصب باشد؛ وگرنه skip)."""

import threading

import pytest

pw = pytest.importorskip("playwright.sync_api")

from app.simulation.lab_server import LabApp, make_server  # noqa: E402


@pytest.fixture
def page(tmp_path):
    app = LabApp(history_dir=tmp_path)
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
              if m.type == "error" and "ERR_" not in m.text and "tile" not in m.text.lower() else None)
        pg.goto(f"http://127.0.0.1:{srv.server_address[1]}/")
        pg.wait_for_selector("#scn option", state="attached")
        yield pg, errors
        browser.close()
    app.close()
    srv.shutdown()
    srv.server_close()


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
