#!/usr/bin/env python3
"""Take the raw captures `make_fixtures.py` cuts the offline suite's fixtures
from, into `captures/` (ignored by git: these are raw, unscrubbed responses).

    python3 tools/capture.py            through WEBMOTORS_PROXY (a residential exit)
    python3 tools/capture.py --cdp      also the Scraping Browser capture, through
                                        WEBMOTORS_CDP_ENDPOINT

Every capture is what the site answered to the request the engines send, from
the same kind of client: headless Chromium with the engines' own user agent,
issuing same-origin fetch() calls from a landing on /api/location. Two are
deliberately NOT that client, because they are the refusals the suite must
recognise:

    px_refusal.html          headless Chromium with its DEFAULT user agent
                             (the `HeadlessChrome` token PerimeterX refuses)
    cloudfront_403.html      plain HTTP from this machine's own address, when
                             that is a datacentre one

Needs Playwright (requirements-playwright.txt).
"""
import argparse
import json
import os
import pathlib
import sys
import urllib.request
from urllib.error import HTTPError

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import env_config  # noqa: E402
import page_flow  # noqa: E402
from product_parser import (ORIGIN_URL, Query, ad_url, request_for,  # noqa: E402
                            average_price_request)
from playwright.sync_api import sync_playwright  # noqa: E402

OUT = REPO / "captures"

FETCH_JS = """async (u) => { const r = await fetch(u, {credentials: "include"});
                            return [r.status, await r.text()]; }"""
SEARCHES = {
    # name: (listing address, sort, per_page, page, extra params)
    "search_gol_p1": ("https://www.webmotors.com.br/carros/estoque/volkswagen/gol", "relevance", 47, 1, {}),
    "search_gol_p2": ("https://www.webmotors.com.br/carros/estoque/volkswagen/gol", "relevance", 47, 2, {}),
    "search_gol_price_asc_p1": ("https://www.webmotors.com.br/carros/estoque/volkswagen/gol", "price-asc", 47, 1, {}),
    "search_motos_honda_p1": ("https://www.webmotors.com.br/motos/estoque/honda", "relevance", 47, 1, {}),
    "search_sp_toyota_p1": ("https://www.webmotors.com.br/carros/sp/toyota", "relevance", 47, 1, {}),
    "search_typo_model": ("https://www.webmotors.com.br/carros/estoque/volkswagen/gool", "relevance", 47, 1, {}),
    "search_bogus_make": ("https://www.webmotors.com.br/carros/estoque/zzzz", "relevance", 47, 1, {}),
    "search_estoque_sponsored": ("https://www.webmotors.com.br/carros/estoque", "relevance", 47, 1,
                                 {"mediaZeroKm": "true"}),
    "search_gol_past_end": ("https://www.webmotors.com.br/carros/estoque/volkswagen/gol", "relevance", 47, None, {}),
    "search_gol_far_past_end": ("https://www.webmotors.com.br/carros/estoque/volkswagen/gol", "relevance", 47, 500, {}),
}


def _proxy():
    v = os.environ.get("WEBMOTORS_PROXY")
    if not v:
        return None
    from proxy_pool import to_playwright
    return to_playwright(v)


def _url(req, extra):
    if not extra:
        return req.url
    params = tuple((k, extra.get(k, v)) for k, v in req.params)
    return type(req)(req.method, req.path, req.page, params).url


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp", action="store_true")
    a = ap.parse_args()
    env_config.load_env()
    OUT.mkdir(exist_ok=True)
    saved = {}

    def save(name, text):
        (OUT / name).write_text(text, encoding="utf-8")
        saved[name] = len(text)

    with sync_playwright() as p:
        # A residential exit is not always served: CloudFront refused one
        # now and then in the live runs, and a fresh browser gets a fresh
        # exit from the gateway. Captures of a refusal in place of data would
        # be worse than none, so the landing must be the site's JSON.
        for attempt in range(1, 6):
            b = p.chromium.launch(headless=True, proxy=_proxy())
            ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/%s Safari/537.36" % b.version)
            pg = b.new_context(locale="pt-BR", user_agent=ua).new_page()
            pg.goto(ORIGIN_URL)
            landing = pg.evaluate("() => document.body.innerText")
            if landing.lstrip().startswith("{"):
                break
            print("landing refused (attempt %d), relaunching" % attempt)
            b.close()
        else:
            sys.exit("five exits refused in a row; nothing captured")
        save("location.json", landing)

        def get(u):
            return pg.evaluate(FETCH_JS, u)

        for name, (listing, sort, per, page, extra) in SEARCHES.items():
            q = Query("search", listing_url=listing, sort=sort, per_page=per,
                      vehicle="motos" if "/motos/" in listing else "carros")
            if page is None:
                first = json.loads(get(request_for(q, 1).url)[1])
                page = first["Pagination"]["PageTotal"] + 1
            status, text = get(_url(request_for(q, page), extra))
            save(name + ".json", text)
            print(name, status, len(text))

        # Adverts: a dealer car, a private car, a motorcycle, all taken from
        # the searches just captured so the slugs are built the engines' way.
        gol = json.loads((OUT / "search_gol_price_asc_p1.json").read_text())["SearchResults"]
        moto = json.loads((OUT / "search_motos_honda_p1.json").read_text())["SearchResults"]
        dealer = next(r for r in gol if r.get("Seller", {}).get("SellerType") == "PJ")
        private = next(r for r in gol if r.get("Seller", {}).get("SellerType") == "PF")
        bike = next(r for r in moto if r.get("UniqueId"))
        ads = {"detail_car_dealer": (ad_url(dealer, "carros"), "car"),
               "detail_car_private": (ad_url(private, "carros"), "car"),
               "detail_bike": (ad_url(bike, "motos"), "bike")}
        for name, (url, kind) in ads.items():
            q = Query("ad", ads=(url,))
            status, text = get(request_for(q, 1).url)
            save(name + ".json", text)
            sku = url.rsplit("/", 1)[-1]
            s2, t2 = get(average_price_request(kind, sku, 1).url)
            save(name.replace("detail", "avg") + ".json", t2)
            print(name, status, len(text), "avg", s2, len(t2))
        s, t = get("/api/detail/car/volkswagen/gol/nao-existe/4-portas/2020/1?pandora=false")
        save("detail_gone.txt", "%d\n%s" % (s, t))
        s, t = get("/api/detail/car/1?pandora=false")
        save("gateway_403.txt", "%d\n%s" % (s, t))
        s, t = get("/carros/estoque/volkswagen/gol")
        save("listing_gol.html", t)
        s, t = get(ads["detail_car_dealer"][0][len("https://www.webmotors.com.br"):])
        save("advert_shell.html", t)
        b.close()

        # The refusal PerimeterX gives a browser that announces itself. An
        # exit CloudFront refuses answers before PerimeterX is consulted, so
        # relaunch until the page is PerimeterX's own.
        for attempt in range(1, 6):
            b = p.chromium.launch(headless=True, proxy=_proxy())
            pg = b.new_context(locale="pt-BR").new_page()
            r = pg.goto(ORIGIN_URL)
            html = pg.content()
            b.close()
            if "_pxAppId" in html:
                save("px_refusal.html", html)
                print("px_refusal", r.status if r else None)
                break
            print("not PerimeterX's page (attempt %d)" % attempt)

        if a.cdp:
            ep = os.environ.get("WEBMOTORS_CDP_ENDPOINT")
            b = p.chromium.connect_over_cdp(ep, timeout=30000)
            ctx = b.contexts[0] if b.contexts else b.new_context()
            pg = ctx.new_page()
            pg.goto("https://www.webmotors.com.br/carros/estoque/volkswagen/gol",
                    wait_until="domcontentloaded")
            pg.wait_for_timeout(5000)
            save("cdp_listing.html", pg.content())
            pg.goto(ORIGIN_URL)
            pg.wait_for_timeout(2000)
            save("cdp_landing.html", pg.content())
            pg.close()

    # CloudFront's refusal: this machine's own address, plain HTTP.
    try:
        with urllib.request.urlopen(urllib.request.Request(
                ORIGIN_URL, headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as resp:
            save("cloudfront_403.html", resp.read().decode("utf-8", "replace"))
    except HTTPError as e:
        save("cloudfront_403.html", e.read().decode("iso-8859-1", "replace"))
    for k, v in saved.items():
        print("  %-32s %8d bytes" % (k, v))
    return 0


if __name__ == "__main__":
    sys.exit(main())
