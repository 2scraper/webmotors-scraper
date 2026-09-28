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
import copy
import csv
import inspect
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
# Parsing: VALUES on real fixtures, not coverage (§10)
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
    equal("...at position 1, not 4 (§24)", rows[0].position, 1)
    # The FILTER is what is tested, not id recovery: a tile with a real-looking
    # id but MediaZeroKm set must still be skipped (§24's weak-check lesson).
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
    """§24: the Scraping Browser's auto-solve extension injects captcha
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
    check("throttled: retried, NOT blocked (§24)",
          F.should_retry("throttled") and not F.counts_as_blocked("throttled"))
    check("the advice for PerimeterX names what was measured",
          "HeadlessChrome" in F.refusal_advice("challenge"))
    check("...and says 'does not implement', never 'cannot'",
          "does not implement" in F.refusal_advice("challenge")
          and "cannot" not in F.refusal_advice("challenge").lower())
    check("the advice for CloudFront names the ADDRESS",
          "ADDRESS" in F.refusal_advice("blocked", fx("cloudfront_403")))
    check("a CDP 401 is explained as expired credentials, not a held pid",
          "expired" in F.cdp_connect_hint("WebSocket error: 401 Unauthorized"))


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
    """§17: a policy constant nothing reads is the same defect as dead code."""
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
    equal("a throttle wait spends its OWN budget, not --retries (§24): "
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


def check_every_engine_implements_the_operations_page_flow_uses():
    """The fetch loop is shared, so an engine missing ONE operation fails
    only when a live run reaches it. The set is DERIVED from page_flow's own
    source (every `ops.<name>`), not listed by hand (§26)."""
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
        equal("%s: the family prefix is byte-identical and in order (§9)" % cls.__name__,
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
    check("end_of_listing is a COMPLETE stop reason (§24)",
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
# The engines — the checks CLAUDE.md §17 says to steal
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
    """§17's check #1. Every call from an engine (and the Scraper API client,
    diff_runs, page_flow and the fixture tools) into a shared module is bound
    against the callee's real signature. A name that does not exist FAILS
    (§22). A name bound in the calling file shadows a same-named module."""
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


# The family's flag contract (CLAUDE.md §9, including the five it omitted
# for months), plus this repo's own query flags.
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
    """§17's check #2: against the contract AND against each other, both
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
    """§10: compileall proves a file PARSES, not that its names RESOLVE.
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
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", text, re.M))
    read = set(env_config.ENV_KEYS)
    equal("the example and the loader name the same variables",
          sorted(documented), sorted(read))
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
        # by the WHOLE directory being absent, never by one file in it (§22).
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
    copy of a check that imports the code (§26)."""
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
# than exempted (§22: the file most likely to acquire a stray phrase is the
# one a wholesale exemption never reads).
BANNED_WORDING = (
    "cloud" + " browser", "anti" + "detect browser", "2scraper " + "Anti" + "detect Browser",
    "gate." + "2prx.com", "ANTI" + "DETECT_LOCAL_API",
)


def check_banned_wording():
    """§12, enforced by this test rather than by review."""
    scanned = 0
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".pytest_cache",
                                                "live", "captures", ".claude")]
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


def check_concurrency_with_the_browser_stubbed():
    """§10: a live run cannot always reach this machinery. Driven through the
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
    """§18: wait_for_function evaluates a string, which a CSP without
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
    """§19: the most expensive bug this family can ship is a SENTENCE."""
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
    """§17's check #4: a column count claimed in the README is a class's."""
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
