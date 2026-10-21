#!/usr/bin/env python3
"""
smoke_test.py — the offline suite for webmotors-scraper.

One file of plain functions. `tests/test_smoke.py` wraps it as a single
pytest test so `pytest` works as an entry point without a second copy of the
checks.

    python3 smoke_test.py            run everything
    python3 smoke_test.py -v         print every check as it passes

It must pass with NO engine library installed at all: every
`import playwright_scraper` / `selenium_scraper` / `puppeteer_scraper` is
guarded and the skip is RECORDED, because "skipped, engine absent" reads
identically to a real import error. CI installs each engine in its own venv
and checks that engine imports.

THE FIXTURES ARE IN `fixtures_generated.json`, NOT INLINE. They are real
responses captured 2026-09-28 with tools/capture.py, trimmed and scrubbed by
make_fixtures.py, which proves each one parses identically to its untrimmed
original. NOT verbatim: a private seller's id, first name, postal code and
own description; the exit's IP and location; PerimeterX's per-visit UUID;
CloudFront's Request ID. See make_fixtures.py for why each.
"""

import argparse
import ast
import contextlib
import copy
import csv
import inspect
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import types
from dataclasses import asdict, fields

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FAILURES = []
PASSED = 0
SKIPS = []
VERBOSE = False


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        if VERBOSE:
            print("  ok   %s" % name)
    else:
        FAILURES.append("%s%s" % (name, (" — " + detail) if detail else ""))
        print("  FAIL %s%s" % (name, (" — " + detail) if detail else ""))


def equal(name, got, want):
    check(name, got == want, "got %r, want %r" % (got, want))


def skip(group, reason):
    SKIPS.append("%s: %s" % (group, reason))
    print("  SKIP %s — %s" % (group, reason))


FIXTURES_PATH = os.path.join(HERE, "fixtures_generated.json")
FIXTURES = json.load(open(FIXTURES_PATH, encoding="utf-8"))


def fx(name) -> str:
    """A fixture as the text an endpoint returns."""
    value = FIXTURES[name]
    if isinstance(value, dict) and set(value) == {"status", "body"}:
        return value["body"]
    return value if isinstance(value, str) else json.dumps(value)


def fx_status(name) -> int:
    return FIXTURES[name]["status"]


ENGINES = ("playwright_scraper", "selenium_scraper", "puppeteer_scraper")
DRIVER_IMPORTS = {"playwright_scraper": "playwright",
                  "selenium_scraper": "selenium",
                  "puppeteer_scraper": "pyppeteer"}

GOL = "https://www.webmotors.com.br/carros/estoque/volkswagen/gol"


def _import_engine(name):
    try:
        return __import__(name)
    except ImportError as e:
        skip(name, "engine library absent (%s)" % e)
        return None


def _q(url=GOL, **kw):
    import product_parser as P
    q, why = P.query_from_url(url, **kw)
    assert q is not None, why
    return q


def _ad_q(*names):
    """An ad-mode query whose adverts are the given detail fixtures."""
    import product_parser as P
    urls = []
    for name in names:
        rec = FIXTURES[name]
        vehicle = "motos" if rec.get("Type") == "bike" else "carros"
        urls.append(P.ad_url(rec, vehicle))
    return P.Query("ad", ads=tuple(urls))


# ---------------------------------------------------------------------------
# The fixtures themselves
# ---------------------------------------------------------------------------

def check_fixture_corpus_is_real_and_scrubbed():
    import make_fixtures
    missing = set(make_fixtures.SOURCES) - set(FIXTURES)
    check("every fixture make_fixtures.py cuts is in fixtures_generated.json",
          not missing, "missing %s" % sorted(missing))
    blob = json.dumps(FIXTURES, ensure_ascii=False)
    check("the corpus is not empty (a scan of nothing passes for the wrong reason)",
          len(blob) > 50000, "%d bytes" % len(blob))
    check("no 32-hex string survived the scrub",
          not re.search(r"\b[0-9a-f]{32}\b", blob))
    ips = set(re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", blob))
    check("no IP address survived except documentation and loopback ones",
          ips <= {"192.0.2.1", "127.0.0.1"}, repr(sorted(ips)))
    private = [r for name in ("search_gol_price_asc_p1", "search_gol_p1")
               for r in FIXTURES[name]["SearchResults"]
               if r.get("Seller", {}).get("SellerType") == "PF"]
    check("the corpus holds private sellers (the scrub below is not vacuous)",
          len(private) >= 3, "%d" % len(private))
    for r in private:
        s = r["Seller"]
        check("private seller %s: id is a placeholder" % s["Id"], s["Id"] >= 900000)
        check("private seller %s: postal code is a placeholder" % s["Id"],
              all(loc.get("ZipCode") in (None, "00000000")
                  for loc in s.get("Localization") or []))
        check("private seller %s: their own text is gone" % s["Id"],
              r.get("LongComment") in (None, "", make_fixtures.PRIVATE_TEXT))
    pd = FIXTURES["detail_car_private"]["Seller"]
    equal("the private advert's first name is a placeholder", pd.get("FirstName"), "Fixture")


# ---------------------------------------------------------------------------
# Parsing: VALUES on real fixtures, not coverage
# ---------------------------------------------------------------------------

def check_a_search_page_parses_to_the_captured_values():
    import product_parser as P
    rows = P.parse_page(fx("search_gol_p1"), _q(), 1)
    equal("five listings kept, five rows", len(rows), 5)
    r = rows[0]
    equal("sku is UniqueId", r.sku, "74012141")
    equal("the url is the site's own, built as it builds it", r.url,
          "https://www.webmotors.com.br/comprar/volkswagen/gol/"
          "16-mi-city-8v-flex-4p-manual/4-portas/2013-2014/74012141")
    equal("title", r.title, "VOLKSWAGEN GOL 1.6 MI CITY 8V FLEX 4P MANUAL")
    equal("price", r.price, 35900.0)
    equal("version", r.version, "1.6 MI CITY 8V FLEX 4P MANUAL")
    equal("years", (r.year_fabrication, r.year_model), (2013, 2014))
    equal("odometer", r.odometer_km, 173000)
    equal("private seller, by the site's own type", (r.seller_type, r.seller_kind),
          ("private", "Pessoa Física"))
    equal("...whose name is never written", r.seller_name, None)
    equal("city and UF", (r.city, r.state), ("São Paulo", "SP"))
    equal("mode, sort and source", (r.mode, r.sort, r.data_source, r.source),
          ("search", "relevance", "api-search", "webmotors.com.br"))
    equal("the currency is not guessed: null until the run reads it", r.currency, None)
    check("a photo url on the site's own image host",
          (r.image_url or "").startswith(P.PHOTO_BASE))
    dealers = [x for x in rows if x.seller_type == "dealer"]
    check("a dealer keeps its trading name",
          dealers and all(x.seller_name for x in dealers))


def check_price_asc_rows_are_private_sellers_and_scrubbed_ids_survive():
    import product_parser as P
    rows = P.parse_page(fx("search_gol_price_asc_p1"), _q(sort="price-asc"), 1)
    equal("sort is the column value", {r.sort for r in rows}, {"price-asc"})
    prices = [r.price for r in rows]
    equal("price-asc really is ascending on the captured page", prices, sorted(prices))
    check("no private seller carries a name",
          all(r.seller_name is None for r in rows if r.seller_type == "private"))


def check_a_motorcycle_page_parses():
    import product_parser as P
    q = _q("https://www.webmotors.com.br/motos/estoque/honda")
    rows = P.parse_page(fx("search_motos_honda_p1"), q, 1)
    equal("four rows", len(rows), 4)
    check("every row is a motorcycle", all(r.vehicle == "motorcycle" for r in rows))
    check("the displacement is read", all(r.engine_cc for r in rows),
          repr([r.engine_cc for r in rows]))
    check("the url has the cc segment and no doors",
          all(re.search(r"/\d*cc/", r.url) and "-portas" not in r.url for r in rows))
    check("no car-only column is filled",
          all(r.doors is None and r.version is None for r in rows))
    check("the transmission comes from `Shift`", all(r.transmission for r in rows))


def check_the_slug_rule_matches_the_sites_own_links():
    """Verified live against 660 links (product_parser.slugify). Pinned here
    on the shapes that each broke an earlier version of the rule."""
    import product_parser as P
    cases = {"4.4 SE SDV8 4X4 TURBO DIESEL 4P AUTOMÁTICO":
             "44-se-sdv8-4x4-turbo-diesel-4p-automatico",
             "CITROËN": "citroen",
             "1.6 MI 8V GASOLINA 2P MANUAL G.III": "16-mi-8v-gasolina-2p-manual-giii",
             "4.0 V8 TURBO PHEV E PERFORMANCE 4MATIC+ SPEEDSHIFT":
             "40-v8-turbo-phev-e-performance-4matic-speedshift",
             "C 100 BIZ+": "c-100-biz", "RANGE ROVER VOGUE": "range-rover-vogue"}
    for text, want in cases.items():
        equal("slugify(%r)" % text, P.slugify(text), want)
    equal("equal years collapse to one", P._years("2024", 2024.0), "2024")
    equal("different years are a range", P._years("2013", 2014.0), "2013-2014")
    rec = {"UniqueId": 1, "Specification": {"Make": {"Value": "BMW"},
           "Model": {"Value": "G 310 R"}, "YearFabrication": "2017",
           "YearModel": 2017.0, "CubicCentimeter": 0.0}}
    equal("a motorcycle stated at 0 cc gets a bare `cc` segment",
          P.ad_url(rec, "motos"), "https://www.webmotors.com.br/comprar/bmw/g-310-r/cc/2017/1")


def check_sponsored_tiles_are_skipped_and_do_not_shift_positions():
    import product_parser as P
    payload = fx("search_estoque_sponsored")
    equal("the fixture holds three sponsored tiles (not vacuous)",
          P.sponsored_count(payload), 3)
    rows = P.parse_page(payload, _q("https://www.webmotors.com.br/carros/estoque"), 1)
    equal("only the listing becomes a row", [r.sku for r in rows], ["80269940"])
    equal("...at position 1, not 4", rows[0].position, 1)
    # The FILTER is what is tested, not id recovery: a tile with a real-looking
    # id but MediaZeroKm set must still be skipped (a check on one field alone is weak).
    tile = copy.deepcopy(json.loads(payload))
    tile["SearchResults"][0]["UniqueId"] = 12345
    tile["SearchResults"][0]["Seller"] = {"SellerType": "PJ"}
    rows = P.parse_page(json.dumps(tile), _q("https://www.webmotors.com.br/carros/estoque"), 1)
    check("a sponsored tile wearing an id is still skipped",
          "12345" not in [r.sku for r in rows])


def check_values_the_site_did_not_state_stay_null():
    import product_parser as P
    rec = copy.deepcopy(FIXTURES["search_gol_p1"]["SearchResults"][0])
    rec["Specification"].pop("Odometer", None)
    rec.pop("FipePercent", None)
    rec.pop("GoodDeal", None)
    rec["PhotoPath"] = ""
    rec["Media"] = {"Photos": []}
    row = P.parse_listing(rec, _q(), page=1, position=1)
    equal("no odometer stated: null, not 0", row.odometer_km, None)
    equal("no FIPE percentage: null", row.fipe_pct, None)
    equal("no badge: False (the badge is either shown or not)", row.good_deal, False)
    equal("no photo: null, not the site's placeholder", row.image_url, None)
    equal("a motorcycle's 0 cc is null", P._vehicle_fields(
        {"Specification": {"CubicCentimeter": 0.0}}, "motos")["engine_cc"], None)


def check_an_advert_parses_and_writes_no_personal_data():
    import product_parser as P
    q = _ad_q("detail_car_private", "detail_car_dealer", "detail_bike")
    private = P.parse_page(fx("detail_car_private"), q, 1)[0]
    equal("private: seller_name is null", private.seller_name, None)
    equal("private: the description is not written", private.description, None)
    check("private: the raw record DID carry a name (the null is a decision)",
          FIXTURES["detail_car_private"]["Seller"].get("FirstName"))
    check("private: no field holds the first name",
          "Fixture" not in json.dumps(asdict(private), ensure_ascii=False))
    dealer = P.parse_page(fx("detail_car_dealer"), q, 2)[0]
    check("dealer: the description is kept", dealer.description)
    check("dealer: fuel, optionals and FIPE", dealer.fuel and dealer.optionals
          and dealer.fipe_code and dealer.fipe_price)
    check("created_at is a real date", re.match(r"20\d\d-\d\d-\d\d", dealer.created_at or ""))
    equal("the advert's url is the one asked for", dealer.url, q.ads[1])
    bike = P.parse_page(fx("detail_bike"), q, 3)[0]
    equal("a bike detail is a motorcycle row", bike.vehicle, "motorcycle")
    check("...with its displacement", bike.engine_cc)
    equal("the site's placeholder date is null", P._iso_date("0001-01-01T00:00:00"), None)


def check_market_prices_fold_into_an_advert():
    import product_parser as P
    q = _ad_q("detail_car_dealer")
    row = P.parse_page(fx("detail_car_dealer"), q, 1)[0]
    check("applied", P.apply_market_prices(row, fx("avg_car_dealer")))
    avg = FIXTURES["avg_car_dealer"]
    equal("min / avg / max are the site's own figures",
          (row.market_price_min, row.market_price_avg, row.market_price_max),
          (avg["SmallestPrice"], avg["MediumPrice"], avg["BiggestPrice"]))
    equal("the state the figures cover", row.market_state, avg["State"])
    check("an empty answer applies nothing", not P.apply_market_prices(row, ""))


def check_currency_is_read_from_the_page_that_states_it():
    import product_parser as P
    equal("a listing's JSON-LD states BRL", P.currency_from_html(fx("listing_gol_jsonld")), "BRL")
    equal("an advert's server-rendered shell states nothing (so ad mode reads a listing)",
          P.currency_from_html(fx("advert_shell_jsonld")), None)
    equal("no page, no currency — never a default", P.currency_from_html(""), None)
    equal("a code outside the allowlist is not read",
          P.currency_from_html('<script type="application/ld+json">{"priceCurrency": "XXL"}</script>'),
          None)
    equal("ad mode reads the listing of the advert's own type",
          P.currency_page(_ad_q("detail_bike")), "https://www.webmotors.com.br/motos/estoque")


def check_totals_are_read_from_page_one_and_planned():
    import page_flow as F
    import product_parser as P
    equal("the site's count", P.total_results(fx("search_gol_p1")), 1844)
    equal("the site's page count", P.pages_available(fx("search_gol_p1")), 40)
    equal("asking for 99 pages plans the 40 there are", F.pages_to_plan(99, 40), 40)
    equal("asking for 3 plans 3", F.pages_to_plan(3, 40), 3)
    equal("PageCurrent past the end is only an echo",
          json.loads(fx("search_gol_far_past_end"))["Pagination"]["PageCurrent"], 500)


def check_filter_echo_catches_what_the_site_did_not_apply():
    import product_parser as P
    equal("a model the site applied: no problem",
          P.filter_mismatch(fx("search_gol_p1"), _q()), None)
    problem = P.filter_mismatch(fx("search_typo_model"),
                                _q("https://www.webmotors.com.br/carros/estoque/volkswagen/gool"))
    check("a misspelt model is caught", problem and "gool" in problem, repr(problem))
    check("...and the refusal says the site applied every model of the make",
          problem and "every VOLKSWAGEN model" in problem)
    problem = P.filter_mismatch(fx("search_bogus_make"),
                                _q("https://www.webmotors.com.br/carros/estoque/zzzz"))
    check("an unknown make is caught", problem and "zzzz" in problem, repr(problem))
    equal("a state the site applied: no problem",
          P.filter_mismatch(fx("search_sp_toyota_p1"),
                            _q("https://www.webmotors.com.br/carros/sp/toyota")), None)
    other = P.filter_mismatch(fx("search_sp_toyota_p1"),
                              _q("https://www.webmotors.com.br/carros/rj/toyota"))
    check("a state the site did NOT apply is caught", other and "'RJ'" in other, repr(other))
    equal("hyphens and spaces spell one model", P._norm("onix-plus"), P._norm("ONIX PLUS"))


def check_url_shapes():
    import product_parser as P
    q = _q("https://www.webmotors.com.br/carros/sp-sao-paulo/toyota/corolla?anode=2020&page=3")
    equal("state, make and model are read", (q.state, q.make, q.model),
          ("SP", "toyota", "corolla"))
    equal("page= is dropped, the other filters kept", q.listing_url,
          "https://www.webmotors.com.br/carros/sp-sao-paulo/toyota/corolla?anode=2020")
    equal("motorcycles", _q("https://webmotors.com.br/motos/estoque/honda").vehicle, "motos")
    equal("used/new prefixes", _q("https://www.webmotors.com.br/carros-usados/estoque").vehicle,
          "carros")
    q, why = P.query_from_url("https://www.webmotors.com.br/carros/xx/fiat")
    check("an unknown state is refused BEFORE anything is sent", q is None and "xx" in why)
    q, why = P.query_from_url("https://www.example.com/carros/estoque")
    check("another host is refused", q is None)
    q, why = P.query_from_url("https://www.webmotors.com.br/comprar/honda/elite-125i/125cc/2021-2022/3015706")
    check("an advert address is an ad-mode query", q is not None and q.mode == "ad")
    ad = P.parse_ad_url("https://www.webmotors.com.br/comprar/honda/elite-125i/125cc/2021-2022/3015706")
    equal("...of a motorcycle (no doors segment)", (ad.kind, ad.sku), ("bike", "3015706"))
    ad = P.parse_ad_url(json.loads(json.dumps(P.ad_url(FIXTURES["detail_car_dealer"], "carros"))))
    equal("a car advert is a car", ad.kind, "car")
    equal("the site's own sort values", P.SORTS,
          {"relevance": 1, "price-desc": 6, "price-asc": 5, "year-desc": 3, "km-asc": 4})
    url, why = P.listing_url_from_parts("carros", "used", "sp", "Volkswagen", "Gol")
    equal("flags build the site's own address", url,
          "https://www.webmotors.com.br/carros-usados/sp/volkswagen/gol")
    url, why = P.listing_url_from_parts("motos", "new")
    check("--condition on motorcycles is refused (not measured)", url is None)


def check_ads_file_reads_a_search_run_and_plain_lines():
    import product_parser as P
    rows = [asdict(r) for r in P.parse_page(fx("search_gol_p1"), _q(), 1)]
    got = P.ads_from_text(json.dumps(rows))
    equal("a search run's JSON gives its adverts, in order", len(got), 5)
    text = "\n".join([got[0], "not a url", got[0], got[1]])
    equal("plain lines: order kept, duplicates and junk dropped",
          P.ads_from_text(text), got[:2])


def check_page_states_on_real_captures():
    import page_flow as F
    equal("a search with rows is content", F.classify(fx("search_gol_p1"), 200), "content")
    equal("a page one past the end is EMPTY, an answer",
          F.classify(fx("search_gol_past_end"), 200), "empty")
    equal("an advert is content", F.classify(fx("detail_car_dealer"), 200), "content")
    equal("an advert that is gone: 404 `null`",
          F.classify(fx("detail_gone"), fx_status("detail_gone")), "gone")
    equal("the gateway refusing a path is REJECTED, not blocked",
          F.classify(fx("gateway_403"), fx_status("gateway_403")), "rejected")
    equal("PerimeterX's refusal is a challenge", F.classify(fx("px_refusal"), 403), "challenge")
    equal("...recognised by its markers with no status (Selenium's landing)",
          F.classify(fx("px_refusal"), None), "challenge")
    equal("CloudFront's refusal is blocked", F.classify(fx("cloudfront_403"), 403), "blocked")
    equal("...with no status too", F.classify(fx("cloudfront_403"), None), "blocked")
    equal("429 is throttled", F.classify("", 429), "throttled")
    equal("anything else is unknown", F.classify("<html>hello</html>", 200), "unknown")
    import product_parser as P
    equal("the refusals are named by vendor",
          (P.detect_bot_challenge(fx("px_refusal")), P.detect_bot_challenge(fx("cloudfront_403"))),
          ("perimeterx", "cloudfront"))


def check_markers_do_not_match_a_page_the_scraping_browser_served():
    """The Scraping Browser's auto-solve extension injects captcha
    hunters into EVERY page. The marker set must score zero against a page
    fetched that way, WITHOUT any strip."""
    import product_parser as P
    html = fx("cdp_landing")
    check("the fixture really carries the extension's injection (not vacuous)",
          html.count("chrome-extension://") >= 10 and "captcha" in html.lower())
    equal("no refusal marker fires on it", P.detect_bot_challenge(html), None)
    for name in ("search_gol_p1", "search_motos_honda_p1", "detail_car_dealer",
                 "listing_gol_jsonld", "location"):
        equal("no marker fires on served data (%s)" % name, P.detect_bot_challenge(fx(name)), None)
    check("the bare words are not markers", not any(
        m.lower() in ("captcha", "perimeterx", "px-captcha") for m in P.PERIMETERX_MARKERS))


def check_state_policy():
    import page_flow as F
    equal("every state has a policy", sorted(F.STATE_POLICY),
          ["blocked", "challenge", "content", "empty", "gone", "rejected",
           "throttled", "unknown"])
    check("content and empty are parsed, never retried or blocked",
          all(F.should_parse(s) and not F.should_retry(s) and not F.counts_as_blocked(s)
              for s in ("content", "empty")))
    check("gone and rejected: not retried, not blocked (not a proxy problem)",
          all(not F.should_retry(s) and not F.counts_as_blocked(s) for s in ("gone", "rejected")))
    check("challenge and blocked: retried from a fresh browser, and blocked",
          all(F.should_retry(s) and F.counts_as_blocked(s) for s in ("challenge", "blocked")))
    check("nothing is ever solved: this repo implements no solve for this site",
          not any(F.should_solve(s) for s in F.STATE_POLICY))
    check("throttled: retried, NOT blocked",
          F.should_retry("throttled") and not F.counts_as_blocked("throttled"))
    check("the advice for PerimeterX names what was measured",
          "HeadlessChrome" in F.refusal_advice("challenge"))
    check("...and says 'does not implement', never 'cannot'",
          "does not implement" in F.refusal_advice("challenge")
          and "cannot" not in F.refusal_advice("challenge").lower())
    check("the advice for CloudFront names the ADDRESS",
          "ADDRESS" in F.refusal_advice("blocked", fx("cloudfront_403")))
    # Reworded: the hint used to promise that credentials "last about a
    # day", which a ten-day-old working endpoint disproved. It now says the
    # credentials were refused without guessing why.
    hint = F.cdp_connect_hint("WebSocket error: 401 Unauthorized")
    check("a CDP 401 is explained as refused credentials, not a held pid",
          "refused" in hint and "pid" not in hint)
    check("...without claiming how long credentials last",
          "a day" not in hint)


def check_the_identity_override_is_complete():
    """pyppeteer's setUserAgent and a bare Selenium override drop every
    client hint, and PerimeterX refused that 2 of 2 (page_flow.ua_override)."""
    import page_flow as F
    o = F.ua_override("HeadlessChrome/153.0.8010.12", "pt-BR")
    check("the UA string carries no HeadlessChrome token", "Headless" not in o["userAgent"])
    check("...and names the browser's own version", "Chrome/153.0.8010.12" in o["userAgent"])
    meta = o.get("userAgentMetadata") or {}
    check("the client hints are sent WITH it",
          bool(meta.get("brands")) and meta.get("platform") == "Windows", repr(sorted(o)))
    check("...no brand says HeadlessChrome",
          not any("Headless" in b.get("brand", "") for b in meta.get("brands") or []))
    equal("the language is the run's locale", o["acceptLanguage"], "pt-BR")
    for module in ("puppeteer_scraper", "selenium_scraper"):
        src = open(os.path.join(HERE, module + ".py"), encoding="utf-8").read()
        check("%s sends the COMPLETE override" % module, "page_flow.ua_override(" in src)
        check("%s never sends a bare UA string" % module,
              "setUserAgent(_chrome_ua" not in src and '{"userAgent": _chrome_ua' not in src)
    pw = open(os.path.join(HERE, "playwright_scraper.py"), encoding="utf-8").read()
    check("playwright keeps its UA override (load-bearing: HeadlessChrome is refused)",
          '"user_agent": _chrome_ua(browser.version)' in pw)


def check_policy_constants_have_a_consumer():
    """A policy constant nothing reads is the same defect as dead code."""
    src = open(os.path.join(HERE, "page_flow.py"), encoding="utf-8").read()
    engines = "".join(open(os.path.join(HERE, m + ".py"), encoding="utf-8").read()
                      for m in ENGINES)
    for constant in ("RETRY_ON_BLOCKED", "BLOCK_RETRIES_WITHOUT_POOL",
                     "THROTTLE_RETRIES", "THROTTLE_WAIT_S", "FETCH_TIMEOUT_MS",
                     "CORE_FIELD_FLOOR", "CDP_CONNECT_ATTEMPTS", "CDP_LOCKED_WAIT_S"):
        uses = len(re.findall(r"\b%s\b" % constant, src))
        check("page_flow.%s is READ, not only defined" % constant,
              uses >= 2 or constant in engines, "%d occurrence(s)" % uses)


# ---------------------------------------------------------------------------
# The shared fetch loop, driven end to end with a fake driver
# ---------------------------------------------------------------------------

class _FakeOps:
    """page_flow's named operations, answering from fixtures.

    `answers` maps a request's path+page to a queue of (status, text). The
    landing answers `landing` (the served /api/location by default).
    """

    def __init__(self, answers, landing=None):
        self.answers = answers
        self.landing = landing if landing is not None else [fx("location")]
        self.landed = False
        self.pool = None
        self.gotos = self.relaunches = 0
        self.fetches = []

    def _key(self, req):
        if req.path.startswith("/api/search/"):
            return ("search", req.page)
        if req.path.startswith("/api/detail/averageprice/"):
            return ("avg", req.path.rsplit("/", 1)[-1])
        if req.path.startswith("/api/detail/"):
            return ("detail", req.page)
        return ("page", req.path)

    def goto(self, url):
        self.gotos += 1
        return 200, None

    def document_text(self):
        return self.landing.pop(0) if len(self.landing) > 1 else self.landing[0]

    def wait_ms(self, ms):
        pass

    def fetch(self, req):
        key = self._key(req)
        self.fetches.append(key)
        if key[0] == "page":
            return 200, fx("listing_gol_jsonld"), None, None
        queue = self.answers.get(key)
        if not queue:
            if key[0] == "avg":
                return 200, fx("avg_car_dealer"), None, None
            return 200, fx("search_gol_past_end"), None, None
        status, text = queue.pop(0) if len(queue) > 1 else queue[0]
        return status, text, None, None

    def relaunch(self):
        self.relaunches += 1
        self.landed = False

    def proxy_failure(self, text):
        return "ERR_PROXY_CONNECTION_FAILED" if "ERR_PROXY" in (text or "") else ""

    def close(self):
        pass


def _search_page(n):
    """Page n of the Gol search: the captured page with page-unique ids."""
    payload = copy.deepcopy(FIXTURES["search_gol_p1"])
    for rec in payload["SearchResults"]:
        rec["UniqueId"] = int("%d%02d" % (rec["UniqueId"], n))
    return json.dumps(payload)


def _run(answers, pages=3, query=None, landing=None, **extra):
    import page_flow
    with tempfile.TemporaryDirectory() as tmp:
        args = types.SimpleNamespace(
            pages=pages, retries=2, retry_delay=0, delay=0,
            proxy_block_retries=2, out=os.path.join(tmp, "out"), format="json",
            allow_empty=False, dump_html=None, url=None, cdp_endpoint=None,
            concurrency=1)
        for k, v in extra.items():
            setattr(args, k, v)
        ops = _FakeOps(answers, landing)
        q = query or _q()
        if q.mode == "ad":
            args.pages = len(q.ads)
        rc = page_flow.run_pages(lambda: ops, lambda o: None,
                                 lambda pages: ([], [], False), args, None, q, 1)
        meta_path = args.out + ".meta.json"
        meta = json.load(open(meta_path)) if os.path.exists(meta_path) else None
        rows = (json.load(open(args.out + ".json"))
                if os.path.exists(args.out + ".json") else None)
    return rc, meta, rows, ops


def check_the_shared_loop_end_to_end():
    rc, meta, rows, ops = _run({("search", 1): [(200, fx("search_gol_p1"))]}, pages=1)
    equal("a served page: exit 0", rc, 0)
    equal("...complete", meta and meta["status"], "complete")
    equal("...the site's total in the sidecar", meta and meta["total_results"], 1844)
    equal("...and whether the cap bit", meta and meta["capped_by_site"], False)
    equal("...rows written", len(rows or []), 5)
    equal("...each with the currency the listing page states",
          {r["currency"] for r in rows or []}, {"BRL"})
    equal("the browser landed ONCE for the run", ops.gotos, 1)

    rc, meta, rows, ops = _run({("search", 1): [(200, fx("search_typo_model"))]},
                               query=_q("https://www.webmotors.com.br/carros/estoque/volkswagen/gool"))
    equal("a misspelt model: exit 2 before anything is written", (rc, rows, meta), (2, None, None))
    check("...and the currency page was never fetched for it",
          ("page", "/carros/estoque/volkswagen/gool") not in ops.fetches)

    rc, meta, rows, ops = _run({("search", 1): [(fx_status("gateway_403"), fx("gateway_403"))]})
    equal("a REJECTED page 1: exit 5, the data never arrived", rc, 5)
    equal("...fetched once, not retried", ops.fetches, [("search", 1)])

    import page_flow as F
    rc, meta, rows, ops = _run({}, landing=[fx("cloudfront_403")])
    equal("CloudFront on the landing: exit 3", rc, 3)
    equal("...after the measured number of fresh browsers",
          ops.relaunches, F.BLOCK_RETRIES_WITHOUT_POOL)

    rc, meta, rows, ops = _run({("search", 1): [(200, fx("search_gol_p1"))]},
                               landing=[fx("px_refusal"), fx("location")], pages=1)
    equal("PerimeterX once, then a fresh browser is served: exit 0", rc, 0)
    equal("...one relaunch", ops.relaunches, 1)

    rc, meta, rows, ops = _run({("search", 1): [(403, fx("px_refusal")),
                                                (200, fx("search_gol_p1"))]}, pages=1)
    equal("PerimeterX on a fetch AFTER the landing: the session lands again", rc, 0)
    equal("...two landings", ops.gotos, 2)

    rc, meta, rows, ops = _run({("search", 1): [(200, fx("search_gol_past_end"))]})
    equal("an EMPTY search: exit 4, and nothing written", (rc, rows), (4, None))

    rc, meta, rows, ops = _run({("search", 1): [(429, ""), (200, fx("search_gol_p1"))]},
                               pages=1, retries=1)
    equal("a throttle wait spends its OWN budget, not --retries: "
          "with --retries 1 the page still arrives", rc, 0)
    equal("...at the SAME exit (no relaunch)", ops.relaunches, 0)


def check_a_multi_page_run_merges_in_page_order_and_ends_on_data():
    answers = {("search", 1): [(200, _search_page(1))],
               ("search", 2): [(200, _search_page(2))],
               ("search", 3): [(200, fx("search_gol_past_end"))]}
    rc, meta, rows, ops = _run(answers, pages=5)
    equal("an empty page 3 of a planned 5 ends the run: exit 0", rc, 0)
    equal("...as a complete run", meta["status"], "complete")
    equal("...stopped on the DATA", meta["stop_reason"], "end_of_listing")
    equal("...pages 1-3 fetched, not 4-5",
          [f for f in ops.fetches if f[0] == "search"], [("search", n) for n in (1, 2, 3)])
    equal("rows are in page order", [r["page"] for r in rows], [1] * 5 + [2] * 5)
    equal("page+position is unique across the run",
          len({(r["page"], r["position"]) for r in rows}), len(rows))

    answers = {("search", 1): [(200, fx("search_gol_p1"))],
               ("search", 2): [(200, fx("search_gol_far_past_end"))]}
    rc, meta, rows, ops = _run(answers, pages=3)
    equal("a page answered with page 1's rows again (the site past its end) "
          "ends the run as complete", (rc, meta["stop_reason"]), (0, "end_of_listing"))
    equal("...page 3 is never asked for",
          [f for f in ops.fetches if f[0] == "search"], [("search", 1), ("search", 2)])
    equal("...and no row is written twice", len(rows), 5)


def check_ad_mode_end_to_end():
    import product_parser as P
    q = _ad_q("detail_car_dealer", "detail_car_private", "detail_bike")
    gone = "https://www.webmotors.com.br/comprar/volkswagen/gol/nao-existe/4-portas/2020/1"
    q = P.Query("ad", ads=q.ads[:1] + (gone,) + q.ads[1:])
    answers = {("detail", 1): [(200, fx("detail_car_dealer"))],
               ("detail", 2): [(fx_status("detail_gone"), fx("detail_gone"))],
               ("detail", 3): [(200, fx("detail_car_private"))],
               ("detail", 4): [(200, fx("detail_bike"))]}
    rc, meta, rows, ops = _run(answers, query=q)
    equal("four adverts, one gone: exit 0", rc, 0)
    equal("...complete: a gone advert is an answer, not a failure", meta["status"], "complete")
    equal("...named in the sidecar", meta["ads_gone"], [gone])
    equal("...three rows", [r["sku"] for r in rows],
          [str(FIXTURES[n]["UniqueId"]) for n in ("detail_car_dealer", "detail_car_private",
                                                  "detail_bike")])
    check("every advert got its market prices", all(r["market_price_avg"] for r in rows))
    equal("the currency came from a LISTING page", {r["currency"] for r in rows}, {"BRL"})
    check("...the car listing, since the first advert is a car",
          ("page", "/carros/estoque") in ops.fetches)


def _with_renamed(name, field, only=None):
    """A real capture with `field` renamed on its records (all, or the first
    `only`): the payload's shape moving, which no live response showed."""
    d = copy.deepcopy(FIXTURES[name])
    for rec in d["SearchResults"][:only]:
        rec["Renamed" + field] = rec.pop(field)
    return json.dumps(d)


def check_a_moved_shape_is_never_an_empty_listing():
    """Third-party audit, 2026-10-08: with UniqueId renamed, v0.1.0 called
    every listing 'sponsored', the page EMPTY, and a 3-page run ended
    complete, exit 0, on page 2."""
    import product_parser as P
    moved = _with_renamed("search_gol_p2", "UniqueId")
    equal("a page of listings without a usable id is CONTENT, not empty",
          P.detect_page_state(moved, 200), "content")
    equal("...and none of it is counted as advertising", P.sponsored_count(moved), 0)

    rc, meta, rows, ops = _run({("search", 1): [(200, fx("search_gol_p1"))],
                                ("search", 2): [(200, moved)]}, pages=3)
    equal("page 2 unreadable: exit 6, page 1's rows kept", (rc, len(rows or [])), (6, 5))
    equal("...named as the parser finding nothing", meta["stop_reason"], "parser_found_nothing")
    equal("...partial, with the page named", (meta["status"], meta["pages_failed"]),
          ("partial", [2]))
    equal("...and counted", meta["malformed_records"], 5)

    rc, meta, rows, ops = _run({("search", 1): [(200, _with_renamed("search_gol_p1", "UniqueId"))]},
                               pages=1)
    equal("page 1 unreadable: exit 5 (nothing obtained), never 4 (nothing listed)", rc, 5)

    rc, meta, rows, ops = _run({("search", 1): [(200, _with_renamed("search_gol_p1", "UniqueId", 1))]},
                               pages=1)
    equal("ONE unreadable record among five: four rows written", len(rows or []), 4)
    equal("...and the one counted in the sidecar", meta["malformed_records"], 1)
    equal("...the run itself is complete: nothing was left unfetched",
          (rc, meta["status"]), (0, "complete"))

    rc, meta, rows, ops = _run({("search", 1): [(200, _with_renamed("search_gol_p1", "Seller"))]},
                               pages=1)
    equal("a moved Seller: rows still written", len(rows or []), 5)
    check("...and the columns it emptied are in the sidecar, not only the log",
          {"seller_type", "city", "state"} <= set(meta["columns_below_floor"]),
          repr(meta["columns_below_floor"]))
    rc, meta, rows, ops = _run({("search", 1): [(200, fx("search_gol_p1"))]}, pages=1)
    equal("a healthy page: no shortfall, no malformed record",
          (meta["columns_below_floor"], meta["malformed_records"]), ([], 0))


def check_an_ad_runs_identity_is_its_set_of_adverts():
    """Third-party audit, 2026-10-08: v0.1.0 recorded only how MANY adverts a
    run asked for, so two different lists of one length compared."""
    import page_flow as F
    import product_parser as P
    a = _ad_q("detail_car_dealer", "detail_bike")
    b = _ad_q("detail_car_private", "detail_bike")
    flipped = P.Query("ad", ads=tuple(reversed(a.ads)))
    qa, qb = F.query_summary(a), F.query_summary(b)
    equal("same length", (qa["ads"], qb["ads"]), (2, 2))
    check("...different sets, different identity", qa != qb)
    equal("the same set in another order is the same question", F.query_summary(flipped), qa)
    import diff_runs as D
    rows = [asdict(r) for r in P.parse_page(fx("detail_car_dealer"), a, 1)]
    with tempfile.TemporaryDirectory() as tmp:
        x, y = os.path.join(tmp, "x.json"), os.path.join(tmp, "y.json")
        for path, q in ((x, qa), (y, qb)):
            json.dump(rows, open(path, "w"))
            json.dump({"status": "complete", "mode": "ad", "query": q},
                      open(path[:-5] + ".meta.json", "w"))
        check("diff_runs refuses two different advert lists",
              not D._check_comparable(types.SimpleNamespace(old=x, new=y)))
        srows = [asdict(r) for r in P.parse_page(fx("search_gol_p1"), _q())]
        for path, pages in ((x, 3), (y, 5)):
            json.dump(srows, open(path, "w"))
            json.dump({"status": "complete", "mode": "search", "pages_requested": pages,
                       "query": {"sort": "relevance"}}, open(path[:-5] + ".meta.json", "w"))
        check("diff_runs refuses a 3-page and a 5-page run of one search",
              not D._check_comparable(types.SimpleNamespace(old=x, new=y)))
        json.dump({"status": "complete", "mode": "search", "pages_requested": 3,
                   "query": {"sort": "relevance"}}, open(y[:-5] + ".meta.json", "w"))
        check("...and accepts the same depth", D._check_comparable(
            types.SimpleNamespace(old=x, new=y)))


def check_every_engine_implements_the_operations_page_flow_uses():
    """The fetch loop is shared, so an engine missing ONE operation fails
    only when a live run reaches it. The set is DERIVED from page_flow's own
    source (every `ops.<name>`), not listed by hand."""
    tree = ast.parse(open(os.path.join(HERE, "page_flow.py"), encoding="utf-8").read())
    used = {n.attr for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "ops"}
    check("page_flow drives the engines through named operations (not vacuous)",
          {"goto", "fetch", "document_text", "relaunch"} <= used, repr(sorted(used)))
    check("...and the fake driver this suite uses implements every one",
          all(hasattr(_FakeOps({}), name) for name in used),
          repr(sorted(n for n in used if not hasattr(_FakeOps({}), n))))
    for module in ENGINES:
        tree = ast.parse(open(os.path.join(HERE, module + ".py"), encoding="utf-8").read())
        ops_cls = next((n for n in tree.body
                        if isinstance(n, ast.ClassDef) and n.name == "_Ops"), None)
        if ops_cls is None:
            check("%s defines _Ops" % module, False)
            continue
        methods = {n.name for n in ops_cls.body if isinstance(n, ast.FunctionDef)}
        attrs = {t.attr for n in ast.walk(ops_cls) if isinstance(n, ast.Assign)
                 for target in n.targets for t in ast.walk(target)
                 if isinstance(t, ast.Attribute)
                 and isinstance(t.value, ast.Name) and t.value.id == "self"}
        missing = sorted(used - methods - attrs)
        check("%s._Ops provides every operation page_flow uses" % module,
              not missing, "missing %s" % missing)


# ---------------------------------------------------------------------------
# The output contract
# ---------------------------------------------------------------------------

def check_row_schema():
    from output_writer import Ad, Listing, Product, ROW_CLASS_BY_MODE, UNIQUE_BY_SKU_MODES
    for cls in (Listing, Ad):
        names = [f.name for f in fields(cls)]
        equal("%s: the family prefix is byte-identical and in order" % cls.__name__,
              names[:7], ["source", "scraped_at", "url", "sku", "title", "price", "currency"])
        check("%s: the run-describing tail is present" % cls.__name__,
              {"page", "position", "mode", "data_source"} <= set(names))
        check("%s: no column this site never fills" % cls.__name__,
              not ({"brand", "original_price", "discount_pct", "rating", "dealer_score"}
                   & set(names)))
    lst, ad = [f.name for f in fields(Listing)], [f.name for f in fields(Ad)]
    shared = [n for n in lst if n in ad]
    equal("the vehicle columns an advert shares come in the listing's order",
          [n for n in ad if n in shared], shared)
    equal("every mode maps to its row class", sorted(ROW_CLASS_BY_MODE), ["ad", "search"])
    equal("every mode is one row per sku", sorted(UNIQUE_BY_SKU_MODES), ["ad", "search"])
    check("the family name `Product` still imports (CI steps use it)", Product is Listing)
    check("the ordering is a COLUMN on search rows", "sort" in lst)


def check_csv_and_json_writers():
    from output_writer import Listing, write_csv, write_json
    import product_parser as P
    rows = P.parse_page(fx("search_gol_p1"), _q(), 1)
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = os.path.join(tmp, "out.csv")
        write_csv(rows, csv_path, row_cls=Listing)
        reader = list(csv.reader(open(csv_path, encoding="utf-8")))
        equal("CSV header matches the dataclass, in order", reader[0],
              [f.name for f in fields(Listing)])
        equal("CSV holds every row", len(reader) - 1, len(rows))
        check("no Python list repr leaked into the CSV",
              not any(cell.startswith("['") for row in reader[1:] for cell in row))
        empty_csv = os.path.join(tmp, "empty.csv")
        write_csv([], empty_csv, row_cls=Listing)
        equal("an EMPTY csv still carries its header",
              len(list(csv.reader(open(empty_csv, encoding="utf-8")))), 1)
        json_path = os.path.join(tmp, "out.json")
        write_json(rows, json_path)
        loaded = json.load(open(json_path, encoding="utf-8"))
        check("a list column stays a real list in JSON",
              any(isinstance(r["attributes"], list) for r in loaded))
        check("Portuguese stays readable in JSON", "Pessoa Física" in open(json_path, encoding="utf-8").read())


def check_exit_codes():
    import output_writer as O
    equal("3 blocked / 4 empty / 5 never obtained / 6 partial",
          (O.EXIT_BLOCKED, O.EXIT_NO_PRODUCTS, O.EXIT_FETCH_FAILED, O.EXIT_PARTIAL),
          (3, 4, 5, 6))
    check("end_of_listing is a COMPLETE stop reason",
          "end_of_listing" in O.COMPLETE_STOP_REASONS)
    check("api_rejected is NOT complete", "api_rejected" not in O.COMPLETE_STOP_REASONS)


def check_a_run_that_finds_nothing_writes_nothing():
    from output_writer import save
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "out")
        with open(prefix + ".json", "w", encoding="utf-8") as f:
            f.write('[{"sku": "yesterday"}]')
        equal("an empty run exits 4", save([], prefix, "json", allow_empty=False), 4)
        equal("...and leaves the previous good file alone",
              open(prefix + ".json", encoding="utf-8").read(), '[{"sku": "yesterday"}]')
        equal("--allow-empty writes it, and still reports exit 4",
              save([], prefix, "json", allow_empty=True), 4)


def check_diff_runs_tracks_the_real_columns():
    """A sibling family's diff reported 0 changed on real changes for weeks
    because its column list was copied from a repo with different rows."""
    import diff_runs as D
    for mode in ("search", "ad"):
        check("%s: tracked columns are derived and non-empty" % mode,
              len(D.tracked_fields(mode)) >= 5, repr(D.tracked_fields(mode)))
    check("price is tracked", "price" in D.tracked_fields("search"))
    check("position is NOT tracked (a live listing reorders itself)",
          "position" not in D.tracked_fields("search"))
    import product_parser as P
    old = [asdict(r) for r in P.parse_page(fx("search_gol_p1"), _q())]
    new = copy.deepcopy(old)
    new[0]["price"] = 1.0
    del new[1]
    result = D.diff_products(old, new)
    equal("one changed", [c["sku"] for c in result["changed"]], [old[0]["sku"]])
    equal("...with the price named", list(result["changed"][0]["changes"]), ["price"])
    equal("one removed", len(result["removed"]), 1)
    with tempfile.TemporaryDirectory() as tmp:
        a, b = os.path.join(tmp, "a.json"), os.path.join(tmp, "b.json")
        json.dump(old, open(a, "w"))
        json.dump(old, open(b, "w"))
        json.dump({"status": "complete", "mode": "search", "query": {"sort": "relevance"}},
                  open(a[:-5] + ".meta.json", "w"))
        json.dump({"status": "complete", "mode": "search", "query": {"sort": "price-asc"}},
                  open(b[:-5] + ".meta.json", "w"))
        args = types.SimpleNamespace(old=a, new=b)
        check("two different ORDERINGS are refused (different samples of a capped search)",
              not D._check_comparable(args))
        json.dump({"status": "complete", "mode": "ad", "query": {"ads": 3}},
                  open(b[:-5] + ".meta.json", "w"))
        check("two different MODES are refused", not D._check_comparable(args))


def check_sidecar_shape():
    from output_writer import run_meta
    meta = run_meta(status="complete", stop_reason="completed", pages_requested=3,
                    pages_completed=3, pages_failed=[], products=141,
                    mode="search", source="webmotors.com.br",
                    start_url=GOL, final_url=GOL,
                    extra={"total_results": 1844, "pages_available": 40,
                           "capped_by_site": False, "query": {"sort": "relevance"}})
    for key in ("status", "stop_reason", "pages_requested", "pages_completed",
                "pages_failed", "mode", "source", "total_results", "capped_by_site", "query"):
        check("the sidecar records %r" % key, key in meta)
    check("pages_failed is a LIST", isinstance(meta["pages_failed"], list))


# ---------------------------------------------------------------------------
# The engines: checks worth copying to any sibling
# ---------------------------------------------------------------------------

def check_engines_import_their_driver_at_module_level():
    for module, driver in DRIVER_IMPORTS.items():
        tree = ast.parse(open(os.path.join(HERE, module + ".py"), encoding="utf-8").read())
        top = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top.add(node.module.split(".")[0])
        check("%s imports %s at MODULE level" % (module, driver), driver in top,
              "top-level imports: %s" % sorted(top))


def check_shared_calls_bind_against_the_real_signature():
    """Every call from an engine (and the Scraper API client,
    diff_runs, page_flow and the fixture tools) into a shared module is bound
    against the callee's real signature. A name that does not exist FAILS
   . A name bound in the calling file shadows a same-named module."""
    import output_writer
    import page_flow
    import product_parser
    import proxy_pool
    import env_config
    targets = {"page_flow": page_flow, "product_parser": product_parser,
               "output_writer": output_writer, "proxy_pool": proxy_pool,
               "env_config": env_config}
    bound = 0
    for module in ENGINES + ("scraper_api_client", "diff_runs", "page_flow",
                             "make_fixtures", "tools/capture"):
        tree = ast.parse(open(os.path.join(HERE, module + ".py"), encoding="utf-8").read())
        local_names = {n.arg for n in ast.walk(tree) if isinstance(n, ast.arg)}
        direct = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in targets:
                for alias in node.names:
                    direct[alias.asname or alias.name] = (targets[node.module], alias.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func, owner, attr = node.func, None, None
            if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                    and func.value.id in targets and func.value.id not in local_names):
                owner, attr = targets[func.value.id], func.attr
            elif isinstance(func, ast.Name) and func.id in direct:
                owner, attr = direct[func.id]
            if owner is None:
                continue
            if not hasattr(owner, attr):
                check("%s.%s exists (called from %s:%d)" % (owner.__name__, attr,
                      module, node.lineno), False, "AttributeError on a live run")
                continue
            callee = getattr(owner, attr)
            if not callable(callee):
                continue
            try:
                sig = inspect.signature(callee)
            except (TypeError, ValueError):
                continue
            if any(kw.arg is None for kw in node.keywords) or any(
                    isinstance(a, ast.Starred) for a in node.args):
                continue
            try:
                sig.bind(*[None] * len(node.args), **{kw.arg: None for kw in node.keywords})
                bound += 1
            except TypeError as e:
                check("%s:%d %s.%s(...) binds against its real signature"
                      % (module, node.lineno, owner.__name__, attr), False,
                      "%s; signature is %s" % (e, sig))
    check("the binding walk checked something (%d calls)" % bound, bound > 60,
          "only %d calls were bound — is the walk finding them?" % bound)


def _argparse_flags(module_name):
    tree = ast.parse(open(os.path.join(HERE, module_name + ".py"), encoding="utf-8").read())
    parsers = {"p"}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr in ("add_argument_group",
                                             "add_mutually_exclusive_group")):
            parsers.update(t.id for t in node.targets if isinstance(t, ast.Name))
    flags = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in parsers):
            flags.update(a.value for a in node.args if isinstance(a, ast.Constant)
                         and isinstance(a.value, str) and a.value.startswith("--"))
    return flags


# The family's flag contract (including the five it once omitted for
# months), plus this repo's own query flags.
CONTRACT_FLAGS = {
    "--url", "--pages", "--category", "--format", "--out", "--delay",
    "--retries", "--retry-delay", "--concurrency", "--proxy", "--proxy-file",
    "--proxy-rotate", "--proxy-shuffle", "--proxy-block-retries",
    "--twocaptcha-key", "--captcha-api", "--solve-captcha", "--min-score",
    "--cdp-endpoint", "--allow-empty", "--dump-html", "--headless", "--headful",
    "--fingerprint", "--fp-country", "--fp-tags", "--locale", "--mode",
}
SITE_FLAGS = {"--condition", "--state", "--make", "--model", "--sort",
              "--per-page", "--ads-file"}


def check_engine_flag_sets():
    """Flag sets: against the contract AND against each other, both
    ways. The exception list IS the documentation."""
    sets = {m: _argparse_flags(m) for m in ENGINES}
    for module, flags in sets.items():
        missing = (CONTRACT_FLAGS | SITE_FLAGS) - flags
        check("%s defines every contract flag" % module, not missing,
              "missing %s" % sorted(missing))
    DOCUMENTED_DIFFERENCES = {"puppeteer_scraper": {"--chromium-path"}}
    names = sorted(sets)
    for i in range(len(names) - 1):
        a, b = names[i], names[i + 1]
        only_a = sets[a] - sets[b] - DOCUMENTED_DIFFERENCES.get(a, set())
        only_b = sets[b] - sets[a] - DOCUMENTED_DIFFERENCES.get(b, set())
        check("%s and %s define the same flags" % (a, b), not only_a and not only_b,
              "only in %s: %s; only in %s: %s" % (a, sorted(only_a), b, sorted(only_b)))
    check("the documented difference still exists (closing it must be a decision)",
          "--chromium-path" in sets["puppeteer_scraper"])


def check_the_cli_refuses_what_the_site_would_answer_wrongly():
    engine = _import_engine("playwright_scraper")
    if engine is None:
        return

    def refused(argv):
        try:
            import contextlib
            import io
            with contextlib.redirect_stderr(io.StringIO()):
                engine.parse_args(argv)
        except SystemExit as e:
            return e.code == 2
        return False

    before = dict(os.environ)
    os.environ.pop("WEBMOTORS_URL", None)
    try:
        check("--state xx is refused", refused(["--state", "xx"]))
        check("--model without --make is refused", refused(["--model", "gol"]))
        check("--url plus a filter flag is refused",
              refused(["--url", GOL, "--make", "fiat"]))
        check("an unknown --sort is refused by argparse", refused(["--sort", "win-rate"]))
        check("--per-page 1000 is refused (the cap is on results, not pages)",
              refused(["--per-page", "1000"]))
        check("--condition new on motorcycles is refused",
              refused(["--category", "motos", "--condition", "new"]))
        check("--mode ad without adverts is refused", refused(["--mode", "ad"]))
        args = engine.parse_args(["--make", "volkswagen", "--model", "gol", "--sort", "price-asc"])
        equal("flags build the site's own address", args.query.listing_url, GOL)
        equal("...and the ordering", args.query.sort, "price-asc")
    finally:
        os.environ.clear()
        os.environ.update(before)


def check_banned_and_removed_flags():
    """Scoped to the engines. `--country` is banned: it could disagree with
    the --url, and a search's region is its --state."""
    for module in ENGINES:
        source = open(os.path.join(HERE, module + ".py"), encoding="utf-8").read()
        for flag in ("--antidetect", "--country", "--country-code"):
            check("%s does not define %s" % (module, flag), '"%s"' % flag not in source)


def _py_files():
    out = []
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "captures", ".claude",
                                                "live", ".venv", "venv")]
        out += [os.path.join(root, f) for f in files if f.endswith(".py")]
    return sorted(out)


def check_undefined_names_in_every_module():
    """compileall proves a file PARSES, not that its names RESOLVE.
    Kept COARSE (pooled bindings) so it under-reports rather than invents."""
    import builtins
    for path in _py_files():
        tree = ast.parse(open(path, encoding="utf-8").read())
        defined = set(dir(builtins)) | {"__file__", "__name__", "__doc__",
                                        "__package__", "__spec__"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    defined.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                defined.add(node.id)
            elif isinstance(node, ast.arg):
                defined.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                defined.add(node.name)
        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        unresolved = sorted(used - defined)
        check("%s: every name resolves" % os.path.relpath(path, HERE), not unresolved,
              repr(unresolved))


def check_no_statement_is_unreachable():
    """A statement after a return/raise/break/continue in the SAME block."""
    for path in _py_files():
        tree = ast.parse(open(path, encoding="utf-8").read())
        dead = []
        for node in ast.walk(tree):
            for fld in ("body", "orelse", "finalbody"):
                block = getattr(node, fld, None)
                if not isinstance(block, list):
                    continue
                for i, stmt in enumerate(block[:-1]):
                    if isinstance(stmt, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
                        dead.append(block[i + 1].lineno)
                        break
        check("%s: no statement the control flow can never reach" % os.path.relpath(path, HERE),
              not dead, "first at line %d" % min(dead) if dead else "")


def _import_graph(entrypoint):
    local = {f[:-3] for f in os.listdir(HERE) if f.endswith(".py")}
    seen, todo = set(), [entrypoint]
    while todo:
        name = todo.pop()
        if name in seen or name not in local:
            continue
        seen.add(name)
        tree = ast.parse(open(os.path.join(HERE, name + ".py"), encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                todo.extend(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                todo.append(node.module.split(".")[0])
    return seen


def check_dockerfile_copies_everything_the_entrypoint_imports():
    dockerfile = open(os.path.join(HERE, "Dockerfile"), encoding="utf-8").read()
    copy_lines, joining = [], False
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if joining or stripped.upper().startswith("COPY "):
            copy_lines.append(stripped)
            joining = stripped.endswith("\\")
    copied = set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\.py", " ".join(copy_lines)))
    missing = sorted(_import_graph("playwright_scraper") - copied)
    check("the Dockerfile COPYs every module playwright_scraper.py imports",
          not missing, "missing %s" % missing)
    check("the image does not carry the test suite", "smoke_test" not in copied)
    stale = sorted(m for m in copied if not os.path.exists(os.path.join(HERE, m + ".py")))
    check("the Dockerfile COPYs no module that does not exist", not stale, repr(stale))


def check_env_example_documents_exactly_what_the_loader_reads():
    import env_config
    text = open(os.path.join(HERE, ".env.example"), encoding="utf-8").read()
    # A variable may be documented as a commented-out example line.
    documented = set(re.findall(r"^(?:# )?([A-Z][A-Z0-9_]+)=", text, re.M))
    read = set(env_config.ENV_KEYS)
    equal("the example and the loader name the same variables",
          sorted(documented), sorted(read))
    # An active WEBMOTORS_URL in a copied .env picked a search for every run
    # and made the README's own flag examples exit 2.
    check("the example ships WEBMOTORS_URL commented out, not active",
          not re.search(r"^WEBMOTORS_URL=", text, re.M))
    check("the per-site variables carry the WEBMOTORS_ prefix",
          {"WEBMOTORS_CDP_ENDPOINT", "WEBMOTORS_PROXY", "WEBMOTORS_URL"} <= read)


def check_a_copied_env_example_reads_as_UNSET():
    import env_config
    text = open(os.path.join(HERE, ".env.example"), encoding="utf-8").read()
    values = dict(re.findall(r"^([A-Z][A-Z0-9_]+)=(.*)$", text, re.M))
    credentials = {"TWOCAPTCHA_KEY", "WEBMOTORS_CDP_ENDPOINT", "WEBMOTORS_PROXY"}
    before = dict(os.environ)
    try:
        for name, raw in values.items():
            os.environ[name] = raw
            got = env_config.env_value(name)
            if name in credentials:
                check("a copied .env.example leaves %s unset" % name, got is None, repr(got))
            else:
                check("...while %s stays a usable default" % name, got == raw.strip(), repr(got))
    finally:
        os.environ.clear()
        os.environ.update(before)


def check_credential_scan_is_one_implementation_invoked_from_both():
    script = os.path.join(HERE, ".github", "ci_checks.py")
    if not os.path.isdir(os.path.join(HERE, ".github")):
        # Inside the Docker image, which copies no .github at all. Triggered
        # by the WHOLE directory being absent, never by one file in it.
        skip("ci_checks", "no .github directory (the image)")
        return
    check("the credential scan exists as a script", os.path.exists(script))
    workflow = open(os.path.join(HERE, ".github", "workflows", "tests.yml"),
                    encoding="utf-8").read()
    check("CI INVOKES the script rather than reimplementing it", "ci_checks.py" in workflow)
    result = subprocess.run([sys.executable, script, "--secret-check", "--sample-check"],
                            cwd=HERE, capture_output=True, text=True)
    check("the credential scan and sample check pass on this tree",
          result.returncode == 0, (result.stdout + result.stderr)[-600:])


def check_no_workflow_imports_the_code_inline():
    """A workflow calls ci_checks.py or the CLIs; it does not carry its own
    copy of a check that imports the code."""
    wf_dir = os.path.join(HERE, ".github", "workflows")
    if not os.path.isdir(wf_dir):
        skip("workflows", "no .github directory (the image)")
        return
    local = {f[:-3] for f in os.listdir(HERE) if f.endswith(".py")}
    pattern = re.compile(r"^\s*(?:from|import)\s+(%s)\b" % "|".join(sorted(local)), re.M)
    for name in sorted(os.listdir(wf_dir)):
        hits = pattern.findall(open(os.path.join(wf_dir, name), encoding="utf-8").read())
        check("%s imports no local module inline" % name, not hits, repr(hits))


# Assembled from pieces, so this file can be scanned like every other rather
# than exempted (the file most likely to acquire a stray phrase is the
# one a wholesale exemption never reads).
BANNED_WORDING = (
    "cloud" + " browser", "anti" + "detect browser", "2scraper " + "Anti" + "detect Browser",
    "gate." + "2prx.com", "ANTI" + "DETECT_LOCAL_API",
)


def check_banned_wording():
    """Banned product wording, enforced by this test rather than by review."""
    scanned = 0
    for root, dirs, files in os.walk(HERE):
        # A virtualenv is someone else's text (site-packages), recognised by
        # its pyvenv.cfg rather than by a name like ".venv".
        dirs[:] = [d for d in dirs
                   if d not in (".git", "__pycache__", ".pytest_cache",
                                "live", "captures", ".claude")
                   and not os.path.exists(os.path.join(root, d, "pyvenv.cfg"))]
        for filename in files:
            if not filename.endswith((".py", ".md", ".yml", ".yaml", ".txt",
                                      ".toml", ".html", ".example", ".json")):
                continue
            path = os.path.join(root, filename)
            text = open(path, encoding="utf-8", errors="replace").read().lower()
            scanned += 1
            for phrase in BANNED_WORDING:
                if phrase.lower() in text:
                    check("%s contains no banned phrase #%d" % (
                        os.path.relpath(path, HERE), BANNED_WORDING.index(phrase)), False)
    check("the banned-wording scan read the repo (%d files)" % scanned, scanned > 20)


# References to private notes this public repo cannot link to: a reader
# can open none of them, so a rule is stated in this repo's own words instead.
# Assembled from pieces so this file is scanned like every other.
PRIVATE_CITATIONS = (
    "CLAUDE" + ".md", "claude_" + "2scraper", "2scraper_" + "repo_rules",
    "play" + "book", "template " + "\u00a7",
)
SECTION_SIGN_REF = re.compile("\u00a7" + r"\s*\d")


def check_no_private_citations_in_shipped_files():
    scanned = 0
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs
                   if d not in (".git", "__pycache__", ".pytest_cache", "live",
                                "captures", ".claude")
                   and not os.path.exists(os.path.join(root, d, "pyvenv.cfg"))]
        for filename in files:
            if not filename.endswith((".py", ".md", ".yml", ".yaml", ".txt",
                                      ".toml", ".cfg", ".example", ".json",
                                      ".gitignore", ".dockerignore")) \
                    and filename != "Dockerfile":
                continue
            path = os.path.join(root, filename)
            text = open(path, encoding="utf-8", errors="replace").read()
            scanned += 1
            rel = os.path.relpath(path, HERE)
            for token in PRIVATE_CITATIONS:
                if token.lower() in text.lower():
                    check("%s cites no private note (%r)" % (rel, token), False)
            m = SECTION_SIGN_REF.search(text)
            if m:
                line = text.count("\n", 0, m.start()) + 1
                check("%s:%d carries no bare section-number reference" % (rel, line), False)
    check("the private-citation scan read the repo (%d files)" % scanned, scanned > 20)


def check_concurrency_with_the_browser_stubbed():
    """A live run cannot always reach this machinery. Driven through the
    SHARED worker loop with the fetch replaced."""
    import page_flow
    import queue as queue_mod
    fetched, lock = [], threading.Lock()
    real = page_flow.fetch_one_page

    def fake(ops, args, pool, query, page_num, mask=None):
        with lock:
            fetched.append(page_num)
        o = page_flow.PageOutcome(page_num=page_num, url="u")
        o.products = [] if page_num >= 6 else [types.SimpleNamespace(currency=None)]
        o.state = "empty" if page_num >= 6 else "content"
        return o

    work = queue_mod.Queue()
    for n in range(2, 51):
        work.put(n)
    results, rlock, exhausted = [], threading.Lock(), threading.Event()
    args = types.SimpleNamespace(delay=0)
    q = _q()
    q.currency = "BRL"
    page_flow.fetch_one_page = fake
    try:
        threads = [threading.Thread(target=page_flow.worker_loop,
                                    args=(_FakeOps({}), args, q, work, results,
                                          rlock, exhausted, "w%d" % i))
                   for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
    finally:
        page_flow.fetch_one_page = real
    check("every page fetched was fetched exactly once", len(fetched) == len(set(fetched)))
    check("dispatch STOPPED at the end of the listing", exhausted.is_set())
    check("...so 49 queued pages cost far fewer fetches", len(fetched) < 15,
          "fetched %d" % len(fetched))
    equal("attempted + unattempted covers the whole queue",
          len(set(fetched)) + work.qsize(), 49)
    check("a worker's rows carry the run's currency",
          all(r.currency == "BRL" for o in results for r in o.products))


def check_a_dead_worker_neither_hangs_nor_loses_its_siblings():
    engine = _import_engine("playwright_scraper")
    if engine is None:
        return
    import page_flow

    def exploding(ops, args, pool, query, page_num, mask=None):
        if page_num == 3:
            raise RuntimeError("worker died")
        o = page_flow.PageOutcome(page_num=page_num, url="u")
        o.products = [types.SimpleNamespace(currency=None)]
        o.state = "content"
        return o

    class FakeOps(_FakeOps):
        def __init__(self, *a, **k):
            super().__init__({})

        def open(self):
            return self

    class FakePlaywright:
        def __enter__(self):
            return None

        def __exit__(self, *a):
            return False

    real = (page_flow.fetch_one_page, engine._Ops, engine.sync_playwright)
    page_flow.fetch_one_page = exploding
    engine._Ops = FakeOps
    engine.sync_playwright = lambda: FakePlaywright()
    try:
        results, unattempted, exhausted = engine._fetch_pages_concurrently(
            types.SimpleNamespace(delay=0), None, _q(), list(range(2, 8)), 3)
    finally:
        page_flow.fetch_one_page, engine._Ops, engine.sync_playwright = real
    check("the dead worker's siblings still delivered their pages",
          len(results) >= 3, "%d results" % len(results))
    check("page 3 is not reported as a success", 3 not in [o.page_num for o in results])


def check_worker_pools_start_on_different_exits():
    import page_flow
    from proxy_pool import ProxyPool
    pool = ProxyPool(["http://a:1", "http://b:2", "http://c:3"], rotate="per-run")
    equal("three workers start on three different exits",
          len({page_flow.worker_pool(pool, i).current for i in range(3)}), 3)
    equal("a missing pool stays missing", page_flow.worker_pool(None, 0), None)


def check_fingerprint_kwargs_are_ones_the_driver_accepts():
    engine = _import_engine("playwright_scraper")
    if engine is None:
        return
    from fingerprint_client import playwright_context_kwargs
    import playwright.sync_api as pw_api
    sample = {"id": "x", "country": "US", "userAgent": "Mozilla/5.0 Chrome/140.0.0.0",
              "screen": {"width": 1920, "height": 1080},
              "timezone": "America/New_York", "language": "en-US", "devicePixelRatio": 2}
    kwargs = playwright_context_kwargs(sample)
    signature = inspect.signature(pw_api.Browser.new_context)
    unknown = [k for k in kwargs if k not in signature.parameters]
    check("every fingerprint kwarg is one new_context accepts", not unknown, repr(unknown))


def check_engines_do_not_evaluate_a_string_in_the_browser():
    """wait_for_function evaluates a string, which a CSP without
    unsafe-eval kills. page.evaluate with a real function is fine."""
    for module in ENGINES:
        tree = ast.parse(open(os.path.join(HERE, module + ".py"), encoding="utf-8").read())
        called = {n.func.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        for banned in ("wait_for_function", "waitForFunction", "waitFor"):
            check("%s never CALLS %s" % (module, banned), banned not in called)


def check_fetch_js_is_one_request_in_three_dialects():
    """The one piece of JavaScript each engine spells its own way. It must
    make the same request, with the same timeout and the same cookie rule,
    and set no header a browser asking for a page would not send."""
    for module in ENGINES:
        src = open(os.path.join(HERE, module + ".py"), encoding="utf-8").read()
        js = re.search(r'FETCH_JS = """(.*?)"""', src, re.S)
        check("%s defines FETCH_JS" % module, js is not None)
        if not js:
            continue
        body = js.group(1)
        for needle in ('credentials: "include"', "AbortController"):
            check("%s's fetch() carries %s" % (module, needle), needle in body)
        check("%s's fetch() sets no accept header" % module, '"accept"' not in body)


def check_credentials_never_reach_a_log():
    for module in ENGINES:
        engine = _import_engine(module)
        if engine is None:
            continue
        masked = engine._mask_credentials(
            "tried ws://u:supersecret@h1:9222 and ws://u:supersecret@h2:9222 "
            "and again ws://u:supersecret@h1:9222")
        check("%s masks EVERY occurrence" % module, "supersecret" not in masked, masked)
        check("%s keeps host and port" % module, "h1:9222" in masked and "h2:9222" in masked)
    from proxy_pool import mask
    masked = mask("http://user:secret@exit.example.com:2334")
    check("proxy_pool.mask hides the password", "secret" not in masked)
    check("proxy_pool.mask keeps the exit", "exit.example.com:2334" in masked)


def check_scraper_api_sends_waitfor_as_an_object_and_reads_http_code():
    """Measured 2026-09-23 against the live Scraper API: a JSON-encoded
    STRING waitFor is answered HTTP 422 and still billed; the target's status
    is `http_code`, while `status` is the API's own verdict."""
    try:
        import scraper_api_client as sac
    except ImportError as e:
        skip("scraper_api_client", str(e))
        return
    sent = {}

    class Resp:
        status_code = 200
        headers = {}
        text = ""

        def json(self):
            return {"status": "success", "http_code": 403, "headers": {},
                    "body": "<html></html>"}

    def post(url, **kw):
        sent.update(kw.get("json") or {})
        return Resp()

    args = types.SimpleNamespace(fetch_url="https://www.webmotors.com.br/api/location",
                                 key="k" * 8, timeout=60, cdp_url=None,
                                 wait_text="SearchResults", wait_element=None, wait_state=None)
    real = sac.requests.post
    sac.requests.post = post
    try:
        _html, status = sac.fetch_html(args)
    finally:
        sac.requests.post = real
    equal("--wait-text sends waitFor as an OBJECT", sent.get("waitFor"), {"text": "SearchResults"})
    equal("the target status handed onward is http_code", status, 403)
    equal("the JSON is unwrapped from a viewer <pre>",
          sac.json_text('<html><body><pre>{"Count":1}</pre></body></html>'), '{"Count":1}')
    cf = fx("cloudfront_403")
    equal("...but CloudFront's own <pre> is NOT unwrapped (its markers live outside it)",
          sac.json_text(cf), cf.strip())


def check_x_debug_header_is_redacted():
    try:
        import scraper_api_client as sac
    except ImportError:
        return
    pw = "SeCr" + "EtPw"
    key = "abcdef01" * 4
    raw = ("cdpurl=ws://acct-zone-scraping_browser-pid-7:" + pw
           + "@cb.2captcha.com:9222 cost=0.00145 key=" + key + " status=200")
    out = sac._redact_debug_header(raw)
    check("x-debug: the credential and the key are gone", pw not in out and key not in out)
    check("x-debug: the cost, host and status survive",
          "cost=0.00145" in out and "cb.2captcha.com:9222" in out and "status=200" in out)
    check("x-debug: the log line calls the redactor",
          'logger.info("x-debug: %s", _redact_debug_header(debug))' in inspect.getsource(sac))


def check_captcha_capability_claims_are_honest():
    """The most expensive bug this family can ship is a SENTENCE."""
    readme = open(os.path.join(HERE, "README.md"), encoding="utf-8").read()
    low = readme.lower()
    for phrase in ("cannot be solved", "can't be solved", "is not solvable",
                   "solver is inapplicable", "no solver can", "unsolvable"):
        check("README: no %r — write 'this repo does not implement X'" % phrase,
              phrase not in low)
    check("README says what this repo does not implement, in those words",
          "does not implement" in low)
    check("no captcha solver ships (nothing on this site is solved)",
          not os.path.exists(os.path.join(HERE, "captcha_solver.py")))


def check_readme_numbers_are_not_stale():
    """A column count claimed in the README is a class's."""
    readme = open(os.path.join(HERE, "README.md"), encoding="utf-8").read()
    from output_writer import ROW_CLASS_BY_MODE
    sizes = {len(fields(c)) for c in ROW_CLASS_BY_MODE.values()}
    numbers = re.findall(r"(\d+)\s+columns", readme)
    for number in numbers:
        check("the README's '%s columns' is a row class's size" % number,
              int(number) in sizes, "sizes are %s" % sorted(sizes))
    import product_parser as P
    if "47 per page" in readme:
        equal("the README's '47 per page' matches the code", P.DEFAULT_PER_PAGE, 47)


_TREE_BEFORE = None


def _tree_state():
    result = subprocess.run(["git", "status", "--porcelain"], cwd=HERE,
                            capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return sorted(line for line in result.stdout.splitlines() if not line.endswith(".pyc"))


def check_no_test_mutates_the_working_tree():
    if _TREE_BEFORE is None:
        skip("git status", "not a git repository")
        return
    changed = sorted(set(_tree_state()) - set(_TREE_BEFORE))
    check("the suite itself changed nothing in the working tree", not changed, repr(changed))


def _retries_validation_problems():
    """Every shipped --retries flag must go through page_flow.attempts.

    With `--retries 0` the load loop never runs, nothing is fetched, and the
    engine reports the browser's blank page as a result. This
    reads the SOURCE of every entry point rather than a hand-kept list, so a
    new engine that declares the flag with a plain int is caught the day it is
    added, and it also CALLS the validator rather than trusting its name.

    scraper_api_client.py is out of scope on purpose: it counts EXTRA attempts
    (`retries + 1`), so 0 is legal there and means one try.
    """
    import argparse as _ap
    import ast as _ast
    import importlib as _il
    import pathlib as _pl

    root = _pl.Path(__file__).resolve().parent
    pf = _il.import_module("page_flow")
    problems, seen = [], 0
    for path in sorted(root.glob("*.py")):
        if path.name in ("smoke_test.py", "scraper_api_client.py"):
            continue
        tree = _ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in _ast.walk(tree):
            if not (isinstance(node, _ast.Call)
                    and getattr(node.func, "attr", "") == "add_argument"
                    and node.args and isinstance(node.args[0], _ast.Constant)
                    and node.args[0].value == "--retries"):
                continue
            seen += 1
            kind = next((_ast.unparse(k.value) for k in node.keywords if k.arg == "type"), None)
            want = "attempts" if path.name == "page_flow.py" else "page_flow.attempts"
            if kind != want:
                problems.append("%s:%d declares --retries with type=%s, expected %s"
                                % (path.name, node.lineno, kind, want))
    if not seen:
        problems.append("found no --retries flag in any entry point, so this check "
                        "would pass for the wrong reason")
    for bad in (0, -1, "x"):
        try:
            pf.attempts(bad)
        except _ap.ArgumentTypeError:
            continue
        problems.append("page_flow.attempts accepted %r" % (bad,))
    if pf.attempts(1) != 1 or pf.attempts("3") != 3:
        problems.append("page_flow.attempts rejects a valid count")
    return problems


def check_retries_is_validated_in_every_entry_point():
    problems = _retries_validation_problems()
    check("--retries is validated in every entry point, so 0 is a usage error", not problems, "; ".join(problems))


def _one_listing(**kw):
    from output_writer import Listing
    base = dict(url="https://www.webmotors.com.br/comprar/x/1", sku="1",
                title="VOLKSWAGEN GOL", price=50000.0, currency="BRL",
                page=1, position=1, mode="search")
    base.update(kw)
    return Listing(**base)


def check_output_writes_are_atomic_and_keep_modes():
    """A failed write must leave the previous good file intact, with no
    temporary file beside it; a new file gets `0o666 & ~umask` and an
    existing file keeps its own mode."""
    import output_writer as O
    rows = [_one_listing()]
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "out")
        O.write_json(rows, prefix + ".json")
        O.write_csv(rows, prefix + ".csv")
        O.write_run_meta(prefix, {"status": "complete"})
        before = {ext: open(prefix + ext, encoding="utf-8").read()
                  for ext in (".json", ".csv", ".meta.json")}

        real_dump, real_writerow = O.json.dump, O.csv.DictWriter.writerow

        def half_dump(obj, f, **kw):
            f.write('[{"sku": "half')
            raise OSError(28, "No space left on device")

        def half_row(self, row):
            self.writer.writerow(["half"])
            raise OSError(28, "No space left on device")

        O.json.dump, O.csv.DictWriter.writerow = half_dump, half_row
        try:
            for ext, call in ((".json", lambda: O.write_json(rows, prefix + ".json")),
                              (".csv", lambda: O.write_csv(rows, prefix + ".csv")),
                              (".meta.json", lambda: O.write_run_meta(prefix, {"status": "x"}))):
                try:
                    call()
                    raised = False
                except OSError:
                    raised = True
                check("a failed %s write raises" % ext, raised)
                equal("a failed %s write leaves the previous file intact" % ext,
                      open(prefix + ext, encoding="utf-8").read(), before[ext])
        finally:
            O.json.dump, O.csv.DictWriter.writerow = real_dump, real_writerow
        equal("no temporary file is left behind",
              sorted(os.listdir(tmp)), ["out.csv", "out.json", "out.meta.json"])

        for umask in (0o022, 0o002, 0o077):
            old = os.umask(umask)
            try:
                path = os.path.join(tmp, "new_%o.json" % umask)
                O.write_json(rows, path)
                mode = os.stat(path).st_mode & 0o777
            finally:
                os.umask(old)
            equal("a new file under umask %03o is %03o" % (umask, 0o666 & ~umask),
                  oct(mode), oct(0o666 & ~umask))
        os.chmod(prefix + ".csv", 0o640)
        O.write_csv(rows, prefix + ".csv")
        equal("an existing file keeps its own mode",
              oct(os.stat(prefix + ".csv").st_mode & 0o777), oct(0o640))


def check_csv_cells_are_formula_escaped():
    import output_writer as O
    rows = [_one_listing(title='=HYPERLINK("http://x","y")', city="+SP",
                         seller_name="@me", attributes=["-cmd", "ok"],
                         version="\tTAB", odometer_km=-5)]
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "out")
        rc = O.finish_run(rows, prefix, "both", False, blocked=False,
                          stop_reason="completed", pages_requested=1,
                          pages_completed=1, start_url="u", final_url="u",
                          mode="search", extra={"total_results": 7})
        equal("the run still exits 0", rc, 0)
        cells = list(csv.DictReader(open(prefix + ".csv", encoding="utf-8")))[0]
        equal("a formula title is prefixed", cells["title"], "'" + rows[0].title)
        equal("a '+' cell is prefixed", cells["city"], "'+SP")
        equal("an '@' cell is prefixed", cells["seller_name"], "'@me")
        equal("a joined list is escaped AFTER joining", cells["attributes"], "'-cmd | ok")
        equal("a tab-led cell is prefixed", cells["version"], "'\tTAB")
        equal("a negative NUMBER is left alone", cells["odometer_km"], "-5")
        data = json.load(open(prefix + ".json", encoding="utf-8"))[0]
        equal("JSON keeps the site's bytes", data["title"], rows[0].title)
        equal("JSON keeps a negative number", data["odometer_km"], -5)
        meta = json.load(open(prefix + ".meta.json", encoding="utf-8"))
        equal("the sidecar counts the escaped cells", meta.get("csv_cells_escaped"), 5)
        equal("the caller's extra is still merged", meta.get("total_results"), 7)
        O.finish_run(rows, prefix, "csv", False, blocked=False,
                     stop_reason="completed", pages_requested=1,
                     pages_completed=1, start_url="u", final_url="u",
                     mode="search", extra={"csv_cells_escaped": "caller"})
        meta = json.load(open(prefix + ".meta.json", encoding="utf-8"))
        equal("housekeeping goes UNDER the caller's extra",
              meta.get("csv_cells_escaped"), "caller")


def _query_args(**kw):
    base = dict(url=None, typed_url=None, sort=None, per_page=None,
                category=None, condition=None, state=None, make=None,
                model=None, ads_file=None, mode=None, pages=1)
    base.update(kw)
    return types.SimpleNamespace(**base)


class _UsageError(Exception):
    pass


def _raise_usage(message):
    raise _UsageError(message)


def check_an_env_url_gives_way_to_typed_flags():
    """WEBMOTORS_URL is a default. Search flags typed on the command line
    replace it; a TYPED --url with flags is still refused."""
    import page_flow
    env_url = "https://www.webmotors.com.br/carros/estoque/toyota"
    try:
        q = page_flow.build_query(_query_args(url=env_url, make="volkswagen",
                                              model="gol"), _raise_usage)
        got = q.listing_url
    except _UsageError as e:
        got = "refused: %s" % e
    check("an environment URL gives way to typed --make/--model",
          "volkswagen/gol" in got, got)
    try:
        q = page_flow.build_query(_query_args(url=env_url), _raise_usage)
        got = q.listing_url
    except _UsageError as e:
        got = "refused: %s" % e
    check("an environment URL alone is still the default search",
          "toyota" in got, got)
    try:
        page_flow.build_query(_query_args(url=env_url, typed_url=env_url,
                                          make="volkswagen"), _raise_usage)
        refused = False
    except _UsageError:
        refused = True
    check("a TYPED --url plus a filter flag is still refused", refused)


def _flag_types(module_name):
    tree = ast.parse(open(os.path.join(HERE, module_name + ".py"), encoding="utf-8").read())
    out = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument"
                and node.args and isinstance(node.args[0], ast.Constant)):
            kind = next((ast.unparse(k.value) for k in node.keywords if k.arg == "type"), None)
            out[node.args[0].value] = kind
    return out


def check_counts_and_delays_reject_negative_values():
    """A negative delay crashed `time.sleep` or was read as zero, and a
    negative count was read as 'none'. Each is now a usage error naming the
    flag, in every entry point (read off disk, so it runs without drivers)."""
    import page_flow
    want = {"--delay": 0, "--retry-delay": 0, "--concurrency": 1,
            "--proxy-block-retries": 0, "--pages": 1}
    for module in ENGINES:
        types_ = _flag_types(module)
        for flag, minimum in want.items():
            check("%s %s is validated (at least %d)" % (module, flag, minimum),
                  (types_.get(flag) or "").startswith(
                      "page_flow.at_least('%s', %d" % (flag, minimum)),
                  types_.get(flag))
    api = _flag_types("scraper_api_client")
    for flag, minimum in {"--delay": 0, "--retry-delay": 0, "--retries": 0,
                          "--pages": 1}.items():
        check("scraper_api_client %s is validated (at least %d)" % (flag, minimum),
              (api.get(flag) or "").startswith("at_least('%s', %d" % (flag, minimum)),
              api.get(flag))
    for flag, minimum, kind, bad in (("--delay", 0, float, "-1"),
                                     ("--concurrency", 1, int, "0"),
                                     ("--pages", 1, int, "x")):
        try:
            page_flow.at_least(flag, minimum, kind)(bad)
            message = None
        except argparse.ArgumentTypeError as e:
            message = str(e)
        check("at_least rejects %s %s and names the flag" % (flag, bad),
              message is not None and flag in message, message)
    equal("at_least accepts the minimum", page_flow.at_least("--delay", 0, float)("0"), 0.0)
    # The Scraper API client imports no driver, so its parser runs here.
    import scraper_api_client
    import contextlib
    import io
    saved = sys.argv
    try:
        for argv in (["--retries", "-1"], ["--delay", "-2"]):
            sys.argv = ["scraper_api_client.py", "--url", GOL] + argv
            try:
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    scraper_api_client.parse_args()
                code = 0
            except SystemExit as e:
                code = e.code
            check("scraper_api_client %s exits 2" % " ".join(argv),
                  code == 2 and argv[0] in err.getvalue(), err.getvalue()[-200:])
    finally:
        sys.argv = saved


def check_the_canary_fails_without_a_secret():
    """A canary that skipped green reported a scraper as tested when the site
    was never contacted. Without a secret it must fail, naming the secret."""
    text = open(os.path.join(HERE, ".github", "workflows", "canary.yml"),
                encoding="utf-8").read()
    check("the canary has no green skip branch",
          "::notice::" not in text and "SKIPPED" not in text)
    check("the canary fails by name without a secret",
          "::error::" in text and "WEBMOTORS_PROXY" in text
          and "WEBMOTORS_CDP_ENDPOINT" in text and "exit 1" in text)
    check("both secrets are mapped into the job's environment",
          "WEBMOTORS_PROXY: ${{ secrets.WEBMOTORS_PROXY }}" in text
          and "WEBMOTORS_CDP_ENDPOINT: ${{ secrets.WEBMOTORS_CDP_ENDPOINT }}" in text)
    check("live steps are not gated on a secret (no silent skip)",
          "env.WEBMOTORS_PROXY != ''" not in text)



# ---------------------------------------------------------------------------
# Proxy traffic savers: the pre-check and media blocking
# ---------------------------------------------------------------------------
_SAVERS_URL = GOL
_SAVERS_SECRET = "s3cret-Pw-not-real"


class _StubProxy:
    """A one-port HTTP proxy on 127.0.0.1 that answers every request with a
    fixed status and records the request heads it received."""

    def __init__(self, status):
        import socket
        import threading
        self.status, self.heads = status, []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                self.heads.append(data.decode("latin-1"))
                reason = {407: "Proxy Authentication Required",
                          402: "Payment Required", 200: "OK"}.get(
                              self.status, "X")
                conn.sendall(f"HTTP/1.1 {self.status} {reason}\r\n"
                             "Content-Length: 0\r\nConnection: close\r\n\r\n"
                             .encode())

    def url(self, secret=_SAVERS_SECRET):
        # Concatenated, so no line here looks like a credentialled URL.
        return "http://user-zone-x:" + secret + "@127.0.0.1:" + str(self.port)

    def close(self):
        self.sock.close()


def _closed_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextlib.contextmanager
def _captured_output():
    """stdout and every log record, as one string."""
    import logging
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        with contextlib.redirect_stdout(buf):
            yield buf
    finally:
        root.removeHandler(handler)


class _FakeRoute:
    def __init__(self, resource_type):
        self.request = type("R", (), {"resource_type": resource_type})()
        self.verdict = None

    def abort(self):
        self.verdict = "abort"

    def continue_(self):
        self.verdict = "continue"


def _media_verdicts(handler):
    out = {}
    for rtype in ("image", "media", "font", "document", "script", "xhr",
                  "fetch", "stylesheet"):
        route = _FakeRoute(rtype)
        handler(route)
        out[rtype] = route.verdict
    return out


def _engine_args(engine, tmp, proxy_url, *extra):
    """The engine's own parser, with no .env in play."""
    from unittest import mock
    import env_config
    argv = ["--url", _SAVERS_URL, "--out", os.path.join(tmp, "out")]
    if proxy_url:
        argv += ["--proxy", proxy_url]
    argv += list(extra)
    with mock.patch.object(env_config, "load_env", lambda *a, **k: None):
        return engine.parse_args(argv)


def check_proxy_traffic_savers():
    import argparse
    import asyncio
    from unittest import mock
    import env_config
    import output_writer
    import proxy_check
    from proxy_pool import ProxyPool

    # -- the pre-check, against a stub proxy --------------------------------
    stub = _StubProxy(407)
    try:
        res = proxy_check.precheck(stub.url(), _SAVERS_URL, timeout=5)
        check("a 407 from the proxy is auth_failed and fatal "
                    f"({res.outcome})", res.outcome == "auth_failed"
                    and res.fatal and res.stop_reason == "proxy_auth_failed")
        head = stub.heads[-1] if stub.heads else ""
        check("the pre-check is ONE CONNECT to the target host, with "
                    "the credential", len(stub.heads) == 1
                    and head.startswith("CONNECT www.webmotors.com.br:443 ")
                    and "Proxy-Authorization: Basic " in head)
        with _captured_output() as buf:
            gated = proxy_check.gate(ProxyPool([stub.url()]), _SAVERS_URL,
                                     timeout=5)
        text = buf.getvalue()
        check("gate() returns the fatal check on a 407",
                    gated is not None and gated.fatal)
        check("the 407 message names the variable and the cause",
                    proxy_check.PROXY_SOURCES.split()[0] in text
                    and "refused the credential" in text
                    and "traffic balance is exhausted" in text)
        check("the 407 message never prints the credential",
                    _SAVERS_SECRET not in text and "user-zone-x" not in text)
    finally:
        stub.close()

    stub = _StubProxy(402)
    try:
        res = proxy_check.precheck(stub.url(), _SAVERS_URL, timeout=5)
        msg = proxy_check.failure_message(res, stub.url())
        check("a 402 is balance_exhausted and fatal",
                    res.outcome == "balance_exhausted" and res.fatal
                    and res.stop_reason == "proxy_balance_exhausted")
        check("the 402 message says traffic/balance exhausted, no "
                    "credential", "traffic/balance exhausted" in msg
                    and _SAVERS_SECRET not in msg)
    finally:
        stub.close()

    stub = _StubProxy(200)
    try:
        res = proxy_check.precheck(stub.url(), "http://127.0.0.1:9/x",
                                   timeout=5)
        head = stub.heads[-1] if stub.heads else ""
        check("a plain-http target is one HEAD for its robots.txt",
                    res.outcome == "ok" and not res.fatal
                    and head.startswith("HEAD http://127.0.0.1:9/robots.txt "))
    finally:
        stub.close()

    dead = "http://u:" + _SAVERS_SECRET + "@127.0.0.1:" + str(_closed_port())
    res = proxy_check.precheck(dead, _SAVERS_URL, timeout=5)
    check("a proxy that does not answer is a transport error, NOT "
                "fatal (existing retry behaviour applies)",
                res.outcome == "transport_error" and not res.fatal)
    with _captured_output() as buf:
        gated = proxy_check.gate(ProxyPool([dead]), _SAVERS_URL, timeout=5)
    check("gate() lets a transport error through, credential unprinted",
                gated is None and _SAVERS_SECRET not in buf.getvalue())
    check("no pool means no pre-check",
                proxy_check.gate(None, _SAVERS_URL) is None)
    check("a socks5 exit is skipped, not refused",
                proxy_check.precheck("socks5://127.0.0.1:1", _SAVERS_URL)
                .outcome == "skipped")

    # -- the attempt sidecar ------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out")
        for name, body in (("out.json", "[1]"), ("out.meta.json", "{}")):
            with open(os.path.join(tmp, name), "w") as f:
                f.write(body)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = output_writer.finish_without_fetch(
                out, stop_reason="proxy_auth_failed", pages_requested=3,
                start_url=_SAVERS_URL)
        with open(out + ".latest_attempt.meta.json") as f:
            meta = json.load(f)
        untouched = (open(out + ".json").read() == "[1]"
                     and open(out + ".meta.json").read() == "{}")
    check(f"a refused proxy exits 5 ({rc})", rc == 5)
    check("...and records status failed / proxy_auth_failed in the "
                "attempt sidecar", meta.get("status") == "failed"
                and meta.get("stop_reason") == "proxy_auth_failed"
                and meta.get("pages_completed") == 0)
    check("...leaving the previous output and its .meta.json alone",
                untouched)

    # -- when media is blocked ----------------------------------------------
    pool = ProxyPool(["http://127.0.0.1:1"])
    ns = argparse.Namespace
    check("blocked through a local proxy by default",
                proxy_check.block_media(ns(load_media=False, block_media=False),
                                        pool, remote=False)
                is proxy_check.BLOCK_MEDIA_BY_DEFAULT)
    check("not blocked without a proxy",
                not proxy_check.block_media(
                    ns(load_media=False, block_media=False), None, False))
    check("not blocked over --cdp-endpoint, even when forced",
                not proxy_check.block_media(
                    ns(load_media=False, block_media=True), pool, True))
    check("--load-media turns it off",
                not proxy_check.block_media(
                    ns(load_media=True, block_media=False), pool, False))
    check("an env value of 0 does not count as --load-media",
                proxy_check.block_media(ns(load_media="0", block_media=False),
                                        pool, False)
                is proxy_check.BLOCK_MEDIA_BY_DEFAULT)
    check("--block-media turns it on, even without a proxy",
                proxy_check.block_media(ns(load_media=False, block_media=True),
                                        None, False))
    parser = argparse.ArgumentParser()
    proxy_check.add_media_flags(parser)
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args(["--load-media", "--block-media"])
        exclusive = False
    except SystemExit:
        exclusive = True
    check("--load-media and --block-media are mutually exclusive",
                exclusive)
    check("both --block-media (opt in here) and --load-media are in --help",
          "--block-media" in parser.format_help()
          and "--load-media" in parser.format_help())
    check("media LOADS by default on this site (PerimeterX not measured "
          "with it blocked)", proxy_check.BLOCK_MEDIA_BY_DEFAULT is False)
    check(f"{proxy_check.LOAD_MEDIA_ENV} maps onto --load-media",
                env_config.ENV_KEYS.get(proxy_check.LOAD_MEDIA_ENV)
                == "load_media")

    # -- the route handler, on a stub page ----------------------------------
    ctx = mock.MagicMock()
    with _captured_output():
        proxy_check.install_playwright(ctx)
    pattern, handler = ctx.route.call_args[0]
    verdicts = _media_verdicts(handler)
    check(f"Playwright: route on every URL ({pattern})", pattern == "**/*")
    check("Playwright: image/media/font aborted, everything else "
                f"continues {verdicts}",
                all(verdicts[t] == "abort" for t in ("image", "media", "font"))
                and all(v == "continue" for t, v in verdicts.items()
                        if t not in ("image", "media", "font")))

    cdp = mock.MagicMock()
    cdp.send = mock.AsyncMock()
    page = mock.MagicMock()
    page.target.createCDPSession = mock.AsyncMock(return_value=cdp)

    async def _drive_pyppeteer():
        await proxy_check.install_pyppeteer(page)
        on_paused = cdp.on.call_args[0][1]
        on_paused({"requestId": "r1", "resourceType": "Image"})
        await asyncio.sleep(0)

    with _captured_output():
        asyncio.run(_drive_pyppeteer())
    calls = [c[0] for c in cdp.send.await_args_list]
    enable = next((c[1] for c in calls if c[0] == "Fetch.enable"), {})
    kinds = sorted(pt.get("resourceType") for pt in enable.get("patterns", []))
    check("pyppeteer: Fetch pauses ONLY Image/Media/Font, on its own "
                f"CDP session {kinds}", cdp.on.call_args[0][0] ==
                "Fetch.requestPaused" and kinds == ["Font", "Image", "Media"])
    check("pyppeteer: a paused request is failed, not continued",
                ("Fetch.failRequest", {"requestId": "r1",
                                       "errorReason": "BlockedByClient"})
                in calls)
    check("pyppeteer: never the removed Network.setRequestInterception",
                "setRequestInterception" not in inspect.getsource(
                    proxy_check.install_pyppeteer).split('"""')[-1])

    driver = mock.MagicMock()
    with _captured_output():
        installed = proxy_check.install_selenium(driver)
    sent = {c[0][0]: c[0][1] for c in driver.execute_cdp_cmd.call_args_list}
    urls = sent.get("Network.setBlockedURLs", {}).get("urls", [])
    check("Selenium: Network.setBlockedURLs with image/video/font "
                "patterns", installed and "*.png" in urls and "*.woff2" in urls
                and "*.mp4?*" in urls)
    check("Selenium: no pattern touches a document, script, JSON or "
                "stylesheet", not [u for u in urls if re.search(
                    r"\.(html?|js|json|css)\b", u)])
    broken = mock.MagicMock()
    broken.execute_cdp_cmd.side_effect = RuntimeError("no CDP")
    with _captured_output():
        check("Selenium: a driver that refuses CDP is not fatal",
                    proxy_check.install_selenium(broken) is False)

    # -- each engine, driven: no browser is started on a 407 ----------------
    for name in ENGINES:
        try:
            engine = __import__(name)
        except ImportError as e:
            skip(f"{name} proxy savers", "engine library absent (%s)" % e)
            continue
        stub = _StubProxy(407)
        launched = []
        if name == "playwright_scraper":
            target = "sync_playwright"
        else:
            target = "_Ops"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                args = _engine_args(engine, tmp, stub.url())
                with mock.patch.object(
                        engine, target,
                        side_effect=lambda *a, **k: launched.append(1)), \
                        _captured_output() as buf:
                    try:
                        rc = engine.scrape(args)
                    except Exception as e:  # noqa: BLE001 — fail BY NAME
                        rc = f"raised {type(e).__name__}"
                side = os.path.join(tmp, "out.latest_attempt.meta.json")
                meta = json.load(open(side)) if os.path.exists(side) else {}
                wrote_rows = any(os.path.exists(os.path.join(tmp, "out" + s))
                                 for s in (".json", ".csv", ".meta.json"))
        finally:
            stub.close()
        check(f"{name}: a 407 exits 5 without starting a browser "
                    f"(rc={rc}, launched={len(launched)})",
                    rc == 5 and not launched)
        check(f"{name}: ...with stop_reason proxy_auth_failed in the "
                    "attempt sidecar and no output written",
                    meta.get("stop_reason") == "proxy_auth_failed"
                    and meta.get("status") == "failed" and not wrote_rows)
        check(f"{name}: ...and the credential never reaches the log",
                    _SAVERS_SECRET not in buf.getvalue())

    # -- each engine installs blocking only through a local proxy -----------
    for name in ENGINES:
        try:
            engine = __import__(name)
        except ImportError:
            continue  # already recorded as a skip above
        for label, proxy, extra, want in (
                ("through a proxy", "http://127.0.0.1:1", (), None),
                ("with --load-media", "http://127.0.0.1:1",
                 ("--load-media",), False),
                ("without a proxy", None, (), False),
                ("forced, no proxy", None, ("--block-media",), True),
                ("--block-media through a proxy", "http://127.0.0.1:1",
                 ("--block-media",), True)):
            if want is None:
                want = proxy_check.BLOCK_MEDIA_BY_DEFAULT
            with tempfile.TemporaryDirectory() as tmp:
                args = _engine_args(engine, tmp, proxy, *extra)
            pool = ProxyPool([proxy]) if proxy else None
            got = _engine_installs_blocking(engine, args, pool)
            check(f"{name}: media blocking {label} = {want} (got {got})",
                        got is want)


def _engine_installs_blocking(engine, args, pool):
    """Open the engine's local session on fakes; True if blocking went in."""
    from unittest import mock
    name = engine.__name__
    with _captured_output():
        if name == "playwright_scraper":
            pw = mock.MagicMock()
            browser = pw.chromium.launch.return_value
            browser.version = "120.0.0.0"
            ctx = browser.new_context.return_value
            engine._launch_local(pw, args, pool)
            return ctx.route.called
        if name == "selenium_scraper":
            with mock.patch.object(engine.webdriver, "Chrome") as chrome:
                driver = chrome.return_value
                driver.capabilities = {"browserVersion": "120.0.0.0"}
                engine._Ops(args, pool).open()
            return any(c[0][0] == "Network.setBlockedURLs"
                       for c in driver.execute_cdp_cmd.call_args_list)
        page = mock.MagicMock()
        for attr in ("setUserAgent", "setViewport", "authenticate",
                     "setRequestInterception", "evaluateOnNewDocument",
                     "setExtraHTTPHeaders"):
            setattr(page, attr, mock.AsyncMock())
        cdp = mock.MagicMock()
        cdp.send = mock.AsyncMock()
        page.target.createCDPSession = mock.AsyncMock(return_value=cdp)
        browser = mock.MagicMock()
        browser.newPage = mock.AsyncMock(return_value=page)
        browser.version = mock.AsyncMock(return_value="HeadlessChrome/120.0")

        async def fake_launch(*a, **k):
            return browser
        bridge = engine._AsyncBridge()
        try:
            with mock.patch.object(engine, "launch", fake_launch):
                engine._Ops(bridge, args, pool).open()
        finally:
            closer = getattr(bridge, "close", None)
            if closer:
                closer()
        return any(c[0][0] == "Fetch.enable" and any(
            "resourceType" in pt for pt in c[0][1].get("patterns", []))
            for c in cdp.send.await_args_list)



# ---------------------------------------------------------------------------
# Proxy traffic meter and budget: adapters onto this suite's check()
# ---------------------------------------------------------------------------
from proxy_pool import ProxyPool  # noqa: E402
import env_config  # noqa: E402
REPO_ROOT = HERE


def _mcheck(name, condition):
    check(name, bool(condition))
    return bool(condition)


def group(title):
    if VERBOSE:
        print("\n== %s" % title)


# ---------------------------------------------------------------------------
# Proxy traffic meter and budget
# ---------------------------------------------------------------------------

class _ByteCountingProxy:
    """A forward HTTP proxy on 127.0.0.1 that serves every plain-http GET
    itself with a body of a known size, and counts every byte it received
    and sent. The meter is judged against these two numbers."""

    def __init__(self, sizes):
        import socket
        import threading
        self.sizes = dict(sizes)            # path -> body length
        self.received = self.sent = 0
        # CONNECTs are the browser's own background connections (a system
        # Chromium phones home through its proxy too); they are not page
        # requests, this stub refuses them, and they are kept apart.
        self.connects = 0
        self.paths = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(32)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        import threading
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn):
        from urllib.parse import urlparse
        with conn:
            buf = b""
            while True:
                while b"\r\n\r\n" not in buf:
                    try:
                        chunk = conn.recv(65536)
                    except OSError:
                        return
                    if not chunk:
                        return
                    buf += chunk
                head, _, buf = buf.partition(b"\r\n\r\n")
                lines = head.decode("latin-1").split("\r\n")
                if lines[0].startswith("CONNECT "):
                    self.connects += 1
                    try:
                        conn.sendall(b"HTTP/1.1 403 Forbidden\r\n"
                                     b"Content-Length: 0\r\n\r\n")
                    except OSError:
                        pass
                    return
                self.received += len(head) + 4
                length = 0
                for line in lines[1:]:
                    if line.lower().startswith("content-length:"):
                        length = int(line.split(":", 1)[1])
                while len(buf) < length:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                self.received += length
                buf = buf[length:]
                path = urlparse(lines[0].split(" ")[1]).path or "/"
                self.paths.append(path)
                size = self.sizes.get(path, 0)
                ctype = ("text/html" if path == "/" else "text/css"
                         if path.endswith(".css") else
                         "application/javascript" if path.endswith(".js")
                         else "application/octet-stream")
                body = (b"<html><head><link rel=stylesheet href=/s.css>"
                        b"<script src=/a.js></script></head><body><p id=t>"
                        if path == "/" else b"")
                body = body + b"x" * max(0, size - len(body))
                if path == "/":
                    body = body[:-11] + b"</p></body>"
                status = b"200 OK" if path in self.sizes else b"404 Not Found"
                out = (b"HTTP/1.1 " + status + b"\r\nContent-Type: "
                       + ctype.encode() + b"\r\nContent-Length: "
                       + str(len(body)).encode()
                       + b"\r\nCache-Control: no-store\r\n\r\n" + body)
                try:
                    conn.sendall(out)
                except OSError:
                    return
                self.sent += len(out)

    @property
    def total(self):
        return self.received + self.sent

    def url(self):
        return "http://127.0.0.1:" + str(self.port)

    def close(self):
        self.sock.close()


def _base_exception(fn):
    try:
        fn()
    except BaseException as e:  # noqa: BLE001 — the point is to see it
        return e
    return None


def test_proxy_meter_units(skips):
    group("Proxy traffic meter: counting, the budget, the sidecar")
    import argparse
    from unittest import mock
    import output_writer
    import proxy_meter
    ok = True
    ns = argparse.Namespace

    # -- the meter itself ----------------------------------------------------
    m = proxy_meter.ProxyMeter(1)
    fired = []
    m.on_exceed(lambda: fired.append(1))
    m.add(600_000)
    m.add(400_000)
    ok &= _mcheck("1 MB budget: exactly 1,000,000 bytes is not over it",
                m.bytes == 1_000_000 and not m.exceeded and not fired)
    m.add(1)
    ok &= _mcheck("...one byte more is, and the refusal hook fires",
                m.exceeded and fired == [1])
    m.add(50)
    ok &= _mcheck("...the hook fires once, not per request", fired == [1])
    err = _base_exception(m.check)
    ok &= _mcheck("_mcheck() raises ProxyBudgetExceeded once over budget",
                isinstance(err, proxy_meter.ProxyBudgetExceeded))
    ok &= _mcheck("ProxyBudgetExceeded passes an `except Exception` (a retry "
                "loop cannot swallow it)",
                not issubclass(proxy_meter.ProxyBudgetExceeded, Exception))
    ok &= _mcheck("sidecar fields: bytes, budget in bytes, exceeded",
                m.sidecar() == {"proxy_bytes": 1_000_051,
                                "proxy_budget_bytes": 1_000_000,
                                "proxy_budget_exceeded": True})
    ok &= _mcheck(f"summary line ({m.summary()})",
                m.summary() == "Proxy traffic: 1.0 MB of 1.0 MB budget")
    late = []
    m.on_exceed(lambda: late.append(1))
    ok &= _mcheck("a hook added after the cap was crossed runs at once",
                late == [1])
    free = proxy_meter.ProxyMeter(0)
    free.add(10 ** 12)
    ok &= _mcheck("--proxy-budget-mb 0 means no cap (null budget in the "
                "sidecar)", not free.exceeded and _base_exception(free.check)
                is None and free.sidecar()["proxy_budget_bytes"] is None)

    # -- the flag and the environment ----------------------------------------
    parser = argparse.ArgumentParser()
    proxy_meter.add_budget_flag(parser)
    for bad in ("-1", "abc", "nan", "inf"):
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                parser.parse_args(["--proxy-budget-mb", bad])
            refused = False
        except SystemExit as e:
            refused = e.code == 2
        ok &= _mcheck(f"--proxy-budget-mb {bad} is a usage error (exit 2)",
                    refused)
    ok &= _mcheck("--proxy-budget-mb 0 and 2.5 parse",
                parser.parse_args(["--proxy-budget-mb", "0"]).proxy_budget_mb
                == 0.0 and parser.parse_args(
                    ["--proxy-budget-mb", "2.5"]).proxy_budget_mb == 2.5)
    env = proxy_meter.BUDGET_ENV
    with mock.patch.dict(os.environ, {env: ""}):
        ok &= _mcheck("default budget is 10 MB",
                    proxy_meter.budget_mb(ns(proxy_budget_mb=None)) == 10.0
                    and proxy_meter.DEFAULT_BUDGET_MB == 10.0)
    with mock.patch.dict(os.environ, {env: "2.5"}):
        ok &= _mcheck(f"{env} sets the budget",
                    proxy_meter.budget_mb(ns(proxy_budget_mb=None)) == 2.5)
        ok &= _mcheck("...and the flag wins over it",
                    proxy_meter.budget_mb(ns(proxy_budget_mb=1.0)) == 1.0)
    with mock.patch.dict(os.environ, {env: "lots"}):
        ok &= _mcheck(f"an unparseable {env} stops the run instead of "
                    "silently using a default",
                    isinstance(_base_exception(lambda: proxy_meter.budget_mb(
                        ns(proxy_budget_mb=None))), SystemExit))
    if hasattr(env_config, "ENV_KEYS"):
        ok &= _mcheck(f"{env} maps onto --proxy-budget-mb",
                    env_config.ENV_KEYS.get(env) == "proxy_budget_mb")
    pool = ProxyPool(["http://127.0.0.1:1"])
    ok &= _mcheck("metered only through a local proxy: not without one, not "
                "over --cdp-endpoint",
                proxy_meter.start(ns(proxy_budget_mb=None), None) is None
                and proxy_meter.start(ns(proxy_budget_mb=None), pool,
                                      remote=True) is None
                and isinstance(proxy_meter.start(ns(proxy_budget_mb=None),
                                                 pool), proxy_meter.ProxyMeter))

    a = ns(proxy_budget_mb=0.001)
    proxy_meter.start(a, pool).add(5_000)
    ok &= _mcheck("proxy_meter.check(args) stops a run that is over budget",
                isinstance(_base_exception(lambda: proxy_meter.check(a)),
                           proxy_meter.ProxyBudgetExceeded)
                and _base_exception(lambda: proxy_meter.check(
                    ns(proxy_budget_mb=None))) is None)

    # -- CDP events with known sizes (pyppeteer, Selenium) ------------------
    m = proxy_meter.ProxyMeter(0)
    c = proxy_meter.CdpCounter(m)
    req = {"method": "GET", "url": "http://x.test/a", "headers": {"A": "b"}}
    est = len("GET http://x.test/a HTTP/1.1\r\n") + 2 + len("A: b\r\n")

    def _resp(rid, **extra):
        return {"requestId": rid,
                "response": dict({"url": "http://x.test/a", "status": 200},
                                 **extra)}
    c.feed("Network.requestWillBeSent", {"requestId": "1", "request": req})
    c.feed("Network.responseReceived", _resp("1"))
    c.feed("Network.dataReceived", {"requestId": "1", "encodedDataLength": 99})
    c.feed("Network.loadingFinished", {"requestId": "1",
                                       "encodedDataLength": 1234})
    ok &= _mcheck(f"CDP: a finished request = its request + encodedDataLength "
                f"({m.bytes} vs {est + 1234})", m.bytes == est + 1234)
    c.feed("Network.requestWillBeSent", {"requestId": "2", "request": req})
    c.feed("Network.loadingFailed", {"requestId": "2",
                                     "errorText": "net::ERR_BLOCKED_BY_CLIENT"})
    c.feed("Network.requestWillBeSent", {"requestId": "2b", "request": req})
    c.feed("Network.loadingFailed", {"requestId": "2b", "errorText": "x",
                                     "blockedReason": "inspector"})
    ok &= _mcheck("CDP: a request the browser blocked costs nothing",
                m.bytes == est + 1234)
    c.feed("Network.requestWillBeSent", {"requestId": "3", "request": req})
    c.feed("Network.responseReceived", _resp("3"))
    c.feed("Network.dataReceived", {"requestId": "3", "encodedDataLength": 50})
    c.feed("Network.loadingFailed", {"requestId": "3",
                                     "errorText": "net::ERR_CONNECTION_RESET"})
    ok &= _mcheck("CDP: a request that failed mid-body costs what arrived",
                m.bytes == 2 * est + 1234 + 50)
    c.feed("Network.requestWillBeSent", {"requestId": "4", "request": req})
    c.feed("Network.requestWillBeSent", {
        "requestId": "4", "request": req,
        "redirectResponse": {"encodedDataLength": 300}})
    c.feed("Network.responseReceived", _resp("4"))
    c.feed("Network.loadingFinished", {"requestId": "4",
                                       "encodedDataLength": 500})
    ok &= _mcheck("CDP: a redirect hop is counted as its own exchange",
                m.bytes == 4 * est + 1234 + 50 + 300 + 500)

    # Only what crossed the network. A sibling repo's Selenium run counted
    # Chrome's own new-tab page (chrome:// scripts, ~2.8 MB of
    # loadingFinished) while its proxy carried ~6 KB.
    before = m.bytes
    tab = {"method": "GET", "url": "chrome://new-tab-page/x.js",
           "headers": {"A": "b"}}
    c.feed("Network.requestWillBeSent", {"requestId": "t", "request": tab})
    c.feed("Network.responseReceived", {"requestId": "t", "response": {
        "url": "chrome://new-tab-page/x.js", "status": 200}})
    c.feed("Network.dataReceived", {"requestId": "t",
                                    "encodedDataLength": 2_779_683})
    c.feed("Network.loadingFinished", {"requestId": "t",
                                       "encodedDataLength": 2_779_683})
    for scheme_url in ("data:text/plain,x", "blob:http://x.test/1"):
        c.feed("Network.requestWillBeSent", {"requestId": scheme_url,
               "request": dict(req, url=scheme_url)})
        c.feed("Network.loadingFinished", {"requestId": scheme_url,
                                           "encodedDataLength": 900})
    ok &= _mcheck(f"CDP: a chrome:// new-tab load, data: and blob: cost nothing "
                f"({m.bytes - before} B)", m.bytes == before)
    c.feed("Network.requestWillBeSent", {"requestId": "k1", "request": req})
    c.feed("Network.requestServedFromCache", {"requestId": "k1"})
    c.feed("Network.responseReceived", _resp("k1"))
    c.feed("Network.loadingFinished", {"requestId": "k1",
                                       "encodedDataLength": 5000})
    for flag in ("fromDiskCache", "fromPrefetchCache", "fromServiceWorker"):
        c.feed("Network.requestWillBeSent", {"requestId": flag, "request": req})
        c.feed("Network.responseReceived", _resp(flag, **{flag: True}))
        c.feed("Network.loadingFinished", {"requestId": flag,
                                           "encodedDataLength": 5000})
    ok &= _mcheck(f"CDP: a cache hit (requestServedFromCache, fromDiskCache, "
                f"fromPrefetchCache, fromServiceWorker) costs nothing "
                f"({m.bytes - before} B)", m.bytes == before)
    c.feed("Network.requestWillBeSent", {"requestId": "e", "request": req})
    c.feed("Network.loadingFinished", {"requestId": "e",
                                       "encodedDataLength": 250_000})
    ok &= _mcheck(f"CDP: Chrome's own error page (no responseReceived) costs only "
                f"the request that was attempted ({m.bytes - before} B)",
                m.bytes - before == est)

    # -- Playwright: sizes() of every finished/failed request ---------------
    m = proxy_meter.ProxyMeter(0.001)
    ctx = mock.MagicMock()
    proxy_meter.install_playwright(ctx, m)
    on = {call[0][0]: call[0][1] for call in ctx.on.call_args_list}
    request = mock.MagicMock()
    request.sizes.return_value = {"requestHeadersSize": 100,
                                  "requestBodySize": 20,
                                  "responseHeadersSize": 200,
                                  "responseBodySize": 500}
    on.get("requestfinished", lambda r: None)(request)
    dead = mock.MagicMock()
    dead.sizes.side_effect = RuntimeError("no response")
    on.get("requestfailed", lambda r: None)(dead)
    ok &= _mcheck(f"Playwright: request.sizes() summed on requestfinished "
                f"({m.bytes} of 820), a failed one without sizes is 0",
                m.bytes == 820 and "requestfailed" in on)
    pattern, gate = ctx.route.call_args[0]
    route = mock.MagicMock()
    gate(route)
    ok &= _mcheck(f"Playwright: under budget the route falls through to the "
                f"next handler ({pattern})", pattern == "**/*"
                and route.fallback.called and not route.abort.called)
    on["requestfinished"](request)
    route = mock.MagicMock()
    gate(route)
    ok &= _mcheck("Playwright: over budget every new request is aborted",
                route.abort.called and not route.fallback.called)
    ok &= _mcheck("no meter, nothing installed",
                proxy_meter.install_playwright(ctx, None) is None
                and ctx.route.call_count == 1)

    # -- Selenium: the performance log --------------------------------------
    m = proxy_meter.ProxyMeter(0.001)
    driver = mock.MagicMock()
    log = [{"message": json.dumps({"message": {
        "method": "Network.requestWillBeSent",
        "params": {"requestId": "9", "request": req}}})},
        {"message": json.dumps({"message": {
            "method": "Network.responseReceived", "params": _resp("9")}})},
        {"message": json.dumps({"message": {
            "method": "Network.loadingFinished",
            "params": {"requestId": "9", "encodedDataLength": 2000}}})},
        {"message": "not json"}]
    driver.get_log.return_value = log
    proxy_meter.install_selenium(driver, m)
    entries = proxy_meter.selenium_log(driver)
    ok &= _mcheck(f"Selenium: the performance log is counted ({m.bytes} = "
                f"{est + 2000}) and handed back to the engine's own reader",
                m.bytes == est + 2000 and entries == log)
    blocked = [call[0] for call in driver.execute_cdp_cmd.call_args_list]
    ok &= _mcheck("Selenium: over budget, every URL is blocked over CDP",
                ("Network.setBlockedURLs", {"urls": ["*"]}) in blocked)
    options = mock.MagicMock()
    proxy_meter.selenium_options(options, m)
    ok &= _mcheck("Selenium: the performance log is requested",
                options.set_capability.call_args[0] ==
                ("goog:loggingPrefs", {"performance": "ALL"}))

    # -- pyppeteer: its own CDP session -------------------------------------
    import asyncio
    m = proxy_meter.ProxyMeter(0.001)
    cdp = mock.MagicMock()
    cdp.send = mock.AsyncMock()
    page = mock.MagicMock()
    page.target.createCDPSession = mock.AsyncMock(return_value=cdp)

    async def _drive():
        await proxy_meter.install_pyppeteer(page, m)
        on = {call[0][0]: call[0][1] for call in cdp.on.call_args_list}
        on["Network.requestWillBeSent"]({"requestId": "p", "request": req})
        on["Network.responseReceived"](_resp("p"))
        on["Network.loadingFinished"]({"requestId": "p",
                                       "encodedDataLength": 1500})
        await asyncio.sleep(0)

    asyncio.run(_drive())
    sent = [call[0] for call in cdp.send.await_args_list]
    ok &= _mcheck(f"pyppeteer: Network events counted ({m.bytes} = "
                f"{est + 1500})", m.bytes == est + 1500
                and ("Network.enable", {}) in sent)
    ok &= _mcheck("pyppeteer: over budget, every URL is blocked over CDP",
                ("Network.setBlockedURLs", {"urls": ["*"]}) in sent)

    # The cap can be crossed on another thread (a concurrent worker) that has
    # no event loop: the block must still reach this page's own loop.
    import threading
    m = proxy_meter.ProxyMeter(0.001)
    cdp = mock.MagicMock()
    cdp.send = mock.AsyncMock()
    page = mock.MagicMock()
    page.target.createCDPSession = mock.AsyncMock(return_value=cdp)
    crossed = []

    async def _drive_threaded():
        await proxy_meter.install_pyppeteer(page, m)
        worker = threading.Thread(target=lambda: crossed.append(
            _base_exception(lambda: m.add(5_000))))
        worker.start()
        worker.join()
        for _ in range(5):
            await asyncio.sleep(0)

    asyncio.run(_drive_threaded())
    sent = [call[0] for call in cdp.send.await_args_list]
    ok &= _mcheck(f"pyppeteer: a budget crossed on a worker thread still "
                f"blocks every URL on the page's loop (thread raised "
                f"{crossed})", crossed == [None]
                and ("Network.setBlockedURLs", {"urls": ["*"]}) in sent)

    # -- an HTTP client, through a real stub proxy with known sizes ---------
    import requests
    stub = _ByteCountingProxy({"/": 20_000})
    try:
        m = proxy_meter.ProxyMeter(0)
        r = requests.get("http://meter.test/", proxies={"http": stub.url()},
                         timeout=10)
        proxy_meter.count_response(m, r)
        import time as _time
        for _ in range(50):
            if stub.sent:
                break
            _time.sleep(0.02)
        drift = abs(m.bytes - stub.total) / stub.total
        ok &= _mcheck(f"HTTP client through a stub proxy: metered {m.bytes} B, "
                    f"the proxy carried {stub.total} B ({drift:.1%}, "
                    f"tolerance 5%)", drift <= 0.05)
    finally:
        stub.close()

    # -- the sidecars --------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out")
        metered = proxy_meter.ProxyMeter(10)
        metered.add(123_456)
        rows = _meter_rows()
        with contextlib.redirect_stdout(io.StringIO()):
            rc = _meter_finish(rows, out, "completed", metered.sidecar())
        meta = json.load(open(out + ".meta.json"))
        attempt = json.load(open(out + ".latest_attempt.meta.json"))
        ok &= _mcheck(f"a metered run records proxy_bytes / proxy_budget_bytes "
                    f"/ proxy_budget_exceeded in .meta.json (rc={rc})",
                    rc == 0 and meta.get("proxy_bytes") == 123_456
                    and meta.get("proxy_budget_bytes") == 10_000_000
                    and meta.get("proxy_budget_exceeded") is False)
        ok &= _mcheck("...and the same in .latest_attempt.meta.json",
                    attempt.get("proxy_bytes") == 123_456
                    and attempt.get("status") == meta.get("status"))

    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out")
        over = proxy_meter.ProxyMeter(0.0001)
        over.add(5_000)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = _meter_finish(_meter_rows(), out,
                               proxy_meter.STOP_BUDGET_EXCEEDED, over.sidecar())
        meta = json.load(open(out + ".meta.json"))
        ok &= _mcheck(f"over budget WITH rows: exit 6, partial, "
                    f"stop_reason proxy_budget_exceeded (rc={rc})",
                    rc == 6 and meta.get("status") == "partial"
                    and meta.get("stop_reason") == "proxy_budget_exceeded"
                    and meta.get("proxy_budget_exceeded") is True)

    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out")
        for name, body in (("out.json", "[1]"), ("out.meta.json", "{}")):
            with open(os.path.join(tmp, name), "w") as f:
                f.write(body)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = _meter_finish([], out, proxy_meter.STOP_BUDGET_EXCEEDED,
                               over.sidecar())
        attempt = json.load(open(out + ".latest_attempt.meta.json"))
        untouched = (open(out + ".json").read() == "[1]"
                     and open(out + ".meta.json").read() == "{}")
        ok &= _mcheck(f"over budget with NO rows: exit 5 (rc={rc}), the "
                    f"attempt sidecar says so, the previous output is kept",
                    rc == 5 and untouched
                    and attempt.get("stop_reason") == "proxy_budget_exceeded"
                    and attempt.get("proxy_budget_exceeded") is True
                    and attempt.get("status") == "failed")

    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "out")
        with contextlib.redirect_stdout(io.StringIO()):
            _meter_finish(_meter_rows(), out, "completed", None)
        ok &= _mcheck("an unmetered run writes no attempt sidecar and no "
                    "proxy fields (unchanged)",
                    not os.path.exists(out + ".latest_attempt.meta.json")
                    and "proxy_bytes" not in json.load(
                        open(out + ".meta.json")))

    a = ns(proxy_budget_mb=None)
    setattr(a, "_proxy_meter", metered)
    # Read off the logger calls, not a handler: the root logger's level
    # differs between entry points and would hide an INFO line.
    with mock.patch.object(proxy_meter.logger, "info") as info, \
            mock.patch.object(proxy_meter.logger, "warning") as warn:
        extra = proxy_meter.finish(a, {"site": 1})
    said = [c[0][0] % c[0][1:] for c in
            info.call_args_list + warn.call_args_list]
    ok &= _mcheck(f"the run ends with 'Proxy traffic: X.X MB of Y MB budget' "
                f"({said})",
                "Proxy traffic: 0.1 MB of 10.0 MB budget" in said
                and extra.get("site") == 1 and extra.get("proxy_bytes"))
    return ok


# A request the PAGE makes after the cap. Navigations are stopped by the
# engines' own _mcheck() before they are issued; this is what the in-browser
# refusal (a route abort, CDP Network.setBlockedURLs) is for.
_LATE_FETCH = ("fetch('/n').then(r => r.status).catch(e => 'refused')")


def test_proxy_meter_in_a_real_browser(skips):
    """Opt-in (PROXY_METER_BROWSER=1): each installed driver loads a page
    through _ByteCountingProxy, and the meter must agree with what the proxy
    carried. Needs a Chromium; not run in CI, which installs none."""
    group("Proxy traffic meter in a real browser (opt-in)")
    if os.environ.get("PROXY_METER_BROWSER") != "1":
        print("  not run — set PROXY_METER_BROWSER=1 with a Chromium "
              "installed to measure the meter against a stub proxy")
        return True
    import proxy_meter
    import time as _time
    ok = True
    sizes = {"/": 60_000, "/s.css": 30_000, "/a.js": 90_000, "/n": 40_000}
    chromium = os.environ.get("PROXY_METER_CHROMIUM", "/usr/bin/chromium")
    target = "http://meter.test/"
    ran = 0
    for driver in ("playwright", "pyppeteer", "selenium"):
        try:
            __import__(driver)
        except ImportError:
            continue
        ran += 1
        stub = _ByteCountingProxy(sizes)
        meter = proxy_meter.ProxyMeter(0.15)
        stopped = None
        try:
            if driver == "playwright":
                from playwright.sync_api import sync_playwright
                with sync_playwright() as pw:
                    b = pw.chromium.launch(headless=True,
                                           proxy={"server": stub.url()})
                    ctx = b.new_context()
                    proxy_meter.install_playwright(ctx, meter)
                    p = ctx.new_page()
                    p.goto(target)
                    p.wait_for_timeout(800)
                    first = (meter.bytes, stub.total)
                    p.evaluate(_LATE_FETCH)
                    p.wait_for_timeout(500)
                    stopped = _base_exception(meter.check)
                    b.close()
            elif driver == "pyppeteer":
                import asyncio
                from pyppeteer import launch

                async def go():
                    b = await launch(headless=True, executablePath=chromium,
                                     args=["--no-sandbox",
                                           f"--proxy-server={stub.url()}"],
                                     handleSIGINT=False, handleSIGTERM=False,
                                     handleSIGHUP=False)
                    p = await b.newPage()
                    await proxy_meter.install_pyppeteer(p, meter)
                    await p.goto(target)
                    await asyncio.sleep(0.8)
                    snap = (meter.bytes, stub.total)
                    await p.evaluate(_LATE_FETCH, force_expr=True)
                    await asyncio.sleep(0.5)
                    await b.close()
                    return snap
                first = asyncio.new_event_loop().run_until_complete(go())
                stopped = _base_exception(meter.check)
            else:
                from selenium import webdriver
                from selenium.webdriver.chrome.options import Options
                from selenium.webdriver.chrome.service import Service
                o = Options()
                o.add_argument("--headless=new")
                o.add_argument("--no-sandbox")
                o.add_argument(f"--proxy-server={stub.url()}")
                o.binary_location = chromium
                proxy_meter.selenium_options(o, meter)
                d = webdriver.Chrome(options=o, service=Service(
                    os.environ.get("PROXY_METER_CHROMEDRIVER",
                                   "/usr/bin/chromedriver")))
                proxy_meter.install_selenium(d, meter)
                d.get(target)
                _time.sleep(0.8)
                proxy_meter.drain_selenium(d)
                first = (meter.bytes, stub.total)
                d.execute_script("return " + _LATE_FETCH)
                _time.sleep(0.5)
                proxy_meter.drain_selenium(d)
                stopped = _base_exception(meter.check)
                d.quit()
        except Exception as e:  # noqa: BLE001 — report by name
            ok &= _mcheck(f"{driver}: real-browser meter run ({e!r:.160})", False)
            stub.close()
            continue
        stub.close()
        metered, carried = first
        drift = abs(metered - carried) / max(1, carried)
        ok &= _mcheck(f"{driver}: page + css + js through the stub proxy: "
                    f"metered {metered} B, the proxy carried {carried} B "
                    f"({drift:.1%}, tolerance 10%)", drift <= 0.10)
        ok &= _mcheck(f"{driver}: past the 0.15 MB budget a request the page "
                    f"makes never reaches the proxy, and _mcheck() stops the "
                    f"run before its next navigation (proxy saw {stub.paths})",
                    isinstance(stopped, proxy_meter.ProxyBudgetExceeded)
                    and "/a.js" in stub.paths and "/n" not in stub.paths)
    if not ran:
        print("  no browser driver installed")
    return ok


def _meter_rows():
    import product_parser as P
    return P.parse_page(json.loads(fx("search_gol_p1")), _q(), page=1)[:3]


def _meter_finish(rows, out, stop_reason, proxy_extra):
    from output_writer import finish_run
    extra = {"site_fact": 1}
    if proxy_extra:
        extra.update(proxy_extra)
    return finish_run(rows, out, "json", False, blocked=False,
                      stop_reason=stop_reason, pages_requested=2,
                      pages_completed=1 if rows else 0,
                      start_url=GOL, final_url=GOL, mode="search", extra=extra)


def _budget_run(pages, concurrency, per_fetch, per_goto, budget_mb):
    """The shared loop, on fake operations that each cost known bytes."""
    import page_flow
    import proxy_meter
    import queue as _queue
    with tempfile.TemporaryDirectory() as tmp:
        args = types.SimpleNamespace(
            pages=pages, retries=2, retry_delay=0, delay=0,
            proxy_block_retries=2, out=os.path.join(tmp, "out"), format="json",
            allow_empty=False, dump_html=None, url=None, cdp_endpoint=None,
            concurrency=concurrency, proxy_budget_mb=budget_mb)
        answers = {("search", n): [(200, _search_page(n))]
                   for n in range(1, pages + 1)}
        fetched, lock = [], threading.Lock()

        class _CostlyOps(_FakeOps):
            def goto(self, url):
                proxy_meter.of(args).add(per_goto)
                return super().goto(url)

            def fetch(self, req):
                with lock:
                    fetched.append(self._key(req))
                proxy_meter.of(args).add(per_fetch)
                return super().fetch(req)

        def run_concurrently(page_nums):
            work = _queue.Queue()
            for n in page_nums:
                work.put(n)
            results, rlock, done = [], threading.Lock(), threading.Event()
            threads = [threading.Thread(target=page_flow.worker_loop, args=(
                _CostlyOps(answers), args, _q(), work, results, rlock, done,
                f"w{i}")) for i in range(concurrency)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            left = []
            while not work.empty():
                left.append(work.get_nowait())
            return results, sorted(left), done.is_set()

        with _captured_output() as buf:
            try:
                rc = page_flow.run_pages(
                    lambda: _CostlyOps(answers), lambda o: None,
                    run_concurrently, args, ProxyPool(["http://127.0.0.1:1"]),
                    _q(), concurrency)
            except BaseException as e:  # noqa: BLE001 — fail BY NAME
                rc = "raised %s" % type(e).__name__
        load = lambda p: json.load(open(p)) if os.path.exists(p) else {}
        meta = load(args.out + ".meta.json")
        attempt = load(args.out + ".latest_attempt.meta.json")
    return rc, meta, attempt, fetched, buf.getvalue()


def test_proxy_meter_in_the_engines(skips):
    group("Proxy traffic meter: the shared loop and every engine")
    import page_flow
    import proxy_meter
    from unittest import mock
    ok = True

    # -- the shared loop, which all three engines run ----------------------
    rc, meta, attempt, fetched, log = _budget_run(3, 1, 4_000, 0, 0.01)
    searches = [k for k in fetched if k[0] == "search"]
    ok &= _mcheck(f"sequential: budget crossed on page 2 of 3 → exit 6, rows "
                f"kept, page 3 never requested (rc={rc}, {searches})",
                rc == 6 and searches == [("search", 1), ("search", 2)]
                and meta.get("products") == 10)
    ok &= _mcheck("...stop_reason proxy_budget_exceeded and the proxy fields in "
                "both sidecars",
                meta.get("stop_reason") == "proxy_budget_exceeded"
                and meta.get("proxy_budget_exceeded") is True
                and meta.get("proxy_bytes") == 12_000
                and meta.get("proxy_budget_bytes") == 10_000
                and attempt.get("proxy_bytes") == 12_000)
    ok &= _mcheck("...and the log says 'Proxy traffic: 0.0 MB of 0.0 MB budget'",
                "Proxy traffic: 0.0 MB of 0.0 MB budget" in log)

    rc, meta, attempt, fetched, log = _budget_run(6, 2, 4_000, 0, 0.01)
    searches = sorted(k[1] for k in fetched if k[0] == "search")
    ok &= _mcheck(f"concurrent: every worker stops at its next request, the "
                f"rest is unattempted, exit 6 (rc={rc}, pages {searches})",
                rc == 6 and meta.get("stop_reason") == "proxy_budget_exceeded"
                and len(searches) <= 1 + 2 and 1 in searches
                and log.count("] stopping: proxy budget") == 2)

    import proxy_meter as _pm
    a = types.SimpleNamespace(pages=3, retries=2, retry_delay=0, delay=0,
                              proxy_block_retries=0, dump_html=None,
                              proxy_budget_mb=0.001)
    _pm.start(a, ProxyPool(["http://127.0.0.1:1"])).add(5_000)
    ops = _FakeOps({})
    err = _base_exception(lambda: page_flow.fetch_one_page(ops, a, None, _q(), 1))
    ok &= _mcheck(f"over budget, the shared fetch makes no request at all, not "
                  f"even the landing ({type(err).__name__}, gotos={ops.gotos}, "
                  f"fetches={ops.fetches})",
                  isinstance(err, _pm.ProxyBudgetExceeded)
                  and ops.gotos == 0 and not ops.fetches)

    rc, meta, attempt, fetched, log = _budget_run(3, 1, 0, 4_000, 0.001)
    ok &= _mcheck(f"budget spent on the landing, before any row → exit 5, no "
                f"output, the attempt sidecar says why (rc={rc}, {fetched})",
                rc == 5 and not meta and not fetched
                and attempt.get("stop_reason") == "proxy_budget_exceeded"
                and attempt.get("proxy_bytes") == 4_000)

    rc, meta, attempt, fetched, log = _budget_run(2, 1, 4_000, 0, 0)
    ok &= _mcheck(f"--proxy-budget-mb 0: no cap, the run completes (rc={rc})",
                rc == 0 and meta.get("proxy_budget_bytes") is None
                and meta.get("proxy_budget_exceeded") is False)

    # -- each engine installs the meter only through a local proxy ---------
    for name in ENGINES:
        try:
            engine = __import__(name)
        except ImportError as e:
            skips.append(f"{name} proxy meter driving — {e}")
            print("  SKIP  %s proxy meter — %s" % (name, e))
            continue
        for label, proxy, want in (("through a proxy", "http://127.0.0.1:1", True),
                                   ("without a proxy", None, False)):
            with tempfile.TemporaryDirectory() as tmp:
                args = _engine_args(engine, tmp, proxy)
            pool = ProxyPool([proxy]) if proxy else None
            proxy_meter.start(args, pool, remote=False)
            got = _engine_meter_installed(engine, args, pool)
            ok &= _mcheck(f"{name}: meter installed {label} = {want} (got {got})",
                        got is want)
        with tempfile.TemporaryDirectory() as tmp:
            args = _engine_args(engine, tmp, "http://127.0.0.1:1",
                                "--proxy-budget-mb", "2.5")
        ok &= _mcheck(f"{name}: --proxy-budget-mb reaches the run",
                    proxy_meter.budget_mb(args) == 2.5)
        src = open(os.path.join(HERE, name + ".py"), encoding="utf-8").read()
        ok &= _mcheck(f"{name}: runs the shared loop that holds the budget "
                    f"check", "page_flow.run_pages(" in src)
    return ok


def _engine_meter_installed(engine, args, pool):
    """Open the engine's local session on fakes; True if the meter went in."""
    from unittest import mock
    name = engine.__name__
    with _captured_output():
        if name == "playwright_scraper":
            pw = mock.MagicMock()
            browser = pw.chromium.launch.return_value
            browser.version = "120.0.0.0"
            ctx = browser.new_context.return_value
            engine._launch_local(pw, args, pool)
            return any(c[0][0] == "requestfinished" for c in ctx.on.call_args_list)
        if name == "selenium_scraper":
            with mock.patch.object(engine.webdriver, "Chrome") as chrome:
                driver = chrome.return_value
                driver.capabilities = {"browserVersion": "120.0.0.0"}
                driver.get_log.return_value = []
                engine._Ops(args, pool).open()
            return not isinstance(getattr(driver, "_proxy_meter_counter", None),
                                  (mock.MagicMock, type(None)))
        page = mock.MagicMock()
        for attr in ("setUserAgent", "setViewport", "authenticate",
                     "setRequestInterception", "evaluateOnNewDocument",
                     "setExtraHTTPHeaders"):
            setattr(page, attr, mock.AsyncMock())
        cdp = mock.MagicMock()
        cdp.send = mock.AsyncMock()
        page.target.createCDPSession = mock.AsyncMock(return_value=cdp)
        page._client.send = mock.AsyncMock()
        browser = mock.MagicMock()
        browser.newPage = mock.AsyncMock(return_value=page)
        browser.version = mock.AsyncMock(return_value="HeadlessChrome/120.0")

        async def fake_launch(*a, **k):
            return browser
        bridge = engine._AsyncBridge()
        try:
            with mock.patch.object(engine, "launch", fake_launch):
                engine._Ops(bridge, args, pool).open()
        finally:
            closer = getattr(bridge, "close", None)
            if closer:
                closer()
        return any(c[0][0] == "Network.enable" for c in cdp.send.await_args_list)


def test_canary_reports_proxy_traffic():
    group("Canary: proxy traffic in the step summary, budget enforced")
    import subprocess
    ok = True
    text = open(os.path.join(REPO_ROOT, ".github", "workflows", "canary.yml"),
                encoding="utf-8").read()
    steps = re.split(r"\n\s*- (?:name|uses):", text)
    runs = [s for s in steps if re.search(r"\w+_scraper\.py", s)
            and "--out" in s]
    ok &= _mcheck(f"every canary scrape passes --proxy-budget-mb 10 "
                f"({len(runs)} run step(s))", bool(runs) and all(
                    "--proxy-budget-mb 10" in r for r in runs))
    outs = set()
    for r in runs:
        outs.update(re.findall(r"--out\s+(\S+)", r))
    reporters = [s for s in steps if ".github/proxy_traffic.py" in s
                 and "!cancelled()" in s]
    reported = set()
    for s in reporters:
        reported.update(re.findall(r"proxy_traffic\.py\s+\S+\s+(\S+)", s))
    ok &= _mcheck(f"each run's proxy traffic goes to the step summary, even "
                f"after a failed step (runs {sorted(outs)}, reported "
                f"{sorted(reported)})", bool(outs) and outs <= reported)

    script = os.path.join(REPO_ROOT, ".github", "proxy_traffic.py")
    with tempfile.TemporaryDirectory() as tmp:
        summary = os.path.join(tmp, "summary.md")
        results = {}
        for case, meta in (
                ("over", {"proxy_bytes": 12_300_000,
                          "proxy_budget_bytes": 10_000_000,
                          "proxy_budget_exceeded": True}),
                ("under", {"proxy_bytes": 2_340_000,
                           "proxy_budget_bytes": 10_000_000,
                           "proxy_budget_exceeded": False}),
                ("plain", {"status": "complete"}),
                ("missing", None)):
            out = os.path.join(tmp, case)
            if meta is not None:
                with open(out + ".latest_attempt.meta.json", "w") as f:
                    json.dump(meta, f)
            proc = subprocess.run(
                [sys.executable, script, "live/" + case, out],
                capture_output=True, text=True,
                env={**os.environ, "GITHUB_STEP_SUMMARY": summary})
            results[case] = (proc.returncode, proc.stdout)
        written = open(summary).read() if os.path.exists(summary) else ""
    rc, out = results["over"]
    ok &= _mcheck(f"over budget: '::error::stopped: proxy budget 10 MB "
                f"exceeded' and the step fails (rc={rc})", rc == 1
                and "::error::stopped: proxy budget 10 MB exceeded" in out)
    ok &= _mcheck("under budget: '**live/under**: proxy traffic 2.3 MB of "
                "10 MB' in the summary, step passes",
                results["under"][0] == 0
                and "**live/under**: proxy traffic 2.3 MB of 10 MB" in written
                and "**live/over**: proxy traffic 12.3 MB of 10 MB" in written)
    ok &= _mcheck("no proxy fields / no sidecar: reported, not a crash",
                results["plain"][0] == 0 and results["missing"][0] == 0
                and "not metered" in written and "unknown" in written)
    return ok


def check_proxy_meter_units():
    test_proxy_meter_units(SKIPS)


def check_proxy_meter_in_the_shared_loop_and_engines():
    test_proxy_meter_in_the_engines(SKIPS)


def check_proxy_meter_in_a_real_browser():
    test_proxy_meter_in_a_real_browser(SKIPS)


def check_canary_reports_proxy_traffic():
    test_canary_reports_proxy_traffic()


CHECKS = [v for k, v in sorted(globals().items()) if k.startswith("check_")]


def main():
    global VERBOSE, _TREE_BEFORE
    parser = argparse.ArgumentParser(description="webmotors-scraper offline suite")
    parser.add_argument("-v", "--verbose", action="store_true")
    VERBOSE = parser.parse_args().verbose
    _TREE_BEFORE = _tree_state()
    for fn in CHECKS:
        if VERBOSE:
            print("\n== %s" % fn.__name__)
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — a broken check is a failure
            import traceback
            FAILURES.append("%s raised %s: %s" % (fn.__name__, type(e).__name__, e))
            print("  ERROR %s raised %s: %s" % (fn.__name__, type(e).__name__, e))
            if VERBOSE:
                traceback.print_exc()
    print("\n%d checks passed, %d failed, %d group(s) skipped."
          % (PASSED, len(FAILURES), len(SKIPS)))
    for line in SKIPS:
        print("  skipped: %s" % line)
    if FAILURES:
        print("\nFailures:")
        for line in FAILURES:
            print("  - %s" % line)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
