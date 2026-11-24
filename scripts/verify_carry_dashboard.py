"""Exercise the published dashboard in Chromium. Requires the optional Playwright package."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

REPORT = Path(__file__).resolve().parents[1] / "reports/funding-carry/final"


def verify(url, channel):
    with (REPORT / "metrics.csv").open(encoding="utf8") as data:
        base = {
            (r["window"], r["policy"]): r for r in csv.DictReader(data) if r["scenario"] == "base"
        }
    with sync_playwright() as driver:
        browser = driver.chromium.launch(channel=channel)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_selector("body[data-loaded]")
        canonical = page.url
        selector = page.locator("#win")

        def select_window(key):
            selector.select_option(key)
            page.wait_for_function("k=>document.body.dataset.loaded===k", arg=key)

        select_window("holdout_1")
        page.locator("#plot0").hover()
        page.locator('#plot0 [data-title="Zoom in"]').click()
        select_window("holdout_2")
        assert page.evaluate("plot0._fullLayout.xaxis.range[0].startsWith('2025-10')")
        assert "holdout_2" in page.url
        shared = page.url
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector('body[data-loaded="holdout_2"]')
        select_window("holdout_1")
        page.go_back()
        page.wait_for_selector('body[data-loaded="holdout_2"]')
        page.go_forward()
        page.wait_for_selector('body[data-loaded="holdout_1"]')

        switches = page.locator("section").first.locator("button.trace-key")
        switches.nth(2).focus()
        page.keyboard.press("Space")
        page.wait_for_function("plot0.data[2].visible===false")
        assert switches.nth(2).get_attribute("aria-pressed") == "false"
        page.keyboard.press("Enter")
        page.wait_for_function("plot0.data[2].visible===true")
        keys = selector.locator("option").evaluate_all("o=>o.map(x=>x.value)")
        for k in keys:
            select_window(k)
            assert (
                page.locator(".js-plotly-plot svg.main-svg .cartesianlayer .subplot").count() == 12
            )
            assert page.evaluate(
                "[4,5,9,10].every(i=>document.getElementById('plot'+i)._fullLayout.xaxis.type==='category')"
            )
            for i in [4, 5, 10]:
                graph = page.locator(f"#plot{i}")
                assert graph.locator(".barlayer .point path").evaluate_all(
                    "ps=>ps.some(p=>p.getBBox().height>0.1 && p.getBBox().width>0.1)"
                ), (k, i)
            assert page.locator("#plot9 .heatmaplayer image").evaluate(
                "im=>im.getBBox().width>100 && im.getBBox().height>100"
            )
            peaks = page.evaluate("plot6.data.map(t=>Math.max(...t.y))")
            assert peaks == [float(base[k, p]["drawdown"]) for p in "ABC"]
            assert page.locator("#summary .result").count() == 3
            assert "identical" in page.locator("section .chart-note").first.inner_text()

        select_window("Confirmation")
        risk_section = page.locator("section").nth(11)
        risk_section.locator("summary").click()
        total = risk_section.locator("tbody tr").count()
        assert total > 0
        assert page.evaluate("plot11.data.at(-1).text.some(t=>t.startsWith('×'))")
        scenario = page.locator("#risk-filter option").nth(1)
        name = scenario.inner_text()
        page.select_option("#risk-filter", scenario.get_attribute("value"))
        labels = risk_section.locator("tbody tr td:nth-child(3)").all_text_contents()
        assert 0 < len(labels) < total and set(labels) == {name}

        for width in (390, 320):
            page.set_viewport_size({"width": width, "height": 844})
            page.wait_for_function("document.documentElement.scrollWidth<=innerWidth+1")
            page.wait_for_function(
                "plot9._fullLayout._size.w>innerWidth*0.48 && plot9.clientWidth<innerWidth"
            )
            assert page.locator("#plot9 .heatmaplayer image").evaluate("im=>im.getBBox().width>150")
        for link in ["Research paper", "Benchmark audit"]:
            target = page.get_by_role("link", name=link, exact=True).get_attribute("href")
            doc = browser.new_page()
            doc.goto(urljoin(canonical, target))
            assert doc.locator("main h1").count() == 1 and doc.locator("main table").count() > 0
            assert doc.locator("main img").evaluate_all(
                "imgs=>imgs.every(im=>im.complete && im.naturalWidth>0)"
            )
            doc.close()
        assert not errors, errors
        print(
            f"Passed all {len(keys)} windows and 12 panels, navigation, zoom reset, keyboard, risks, peaks, reports and mobile layout. {shared}"
        )
        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", default=(REPORT / "index.html").as_uri())
    parser.add_argument("--channel", help="Installed Chromium channel, e.g. msedge")
    args = parser.parse_args()
    verify(args.url, args.channel)
