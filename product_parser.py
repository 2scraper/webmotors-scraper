"""
product_parser.py
-----------------
Everything this repo knows about webmotors.com.br lives here (CLAUDE.md §1).

Two modes, four of the site's own JSON endpoints
------------------------------------------------
    --mode search   GET /api/search/car   (cars)
                    GET /api/search/bike  (motorcycles)
                    one row per listing on a search page: price, year,
                    mileage, version, FIPE percentage, seller kind and city
    --mode ad       GET /api/detail/{car|bike}/{slug path}/{id}
                    GET /api/detail/averageprice/{car|bike}/{id}
                    one row per advert: everything above plus fuel,
                    optionals, the FIPE code and value, and the site's own
                    market-price range for that version

The site's front end calls exactly these, with exactly these parameters. The
search endpoint takes the listing page's OWN address as its `url` parameter
and parses the filters out of it itself, which is why `--url` accepts any
listing address the site produces: the path (/carros/sp/volkswagen/gol) and
the query (?precoate=30000&anode=2015) both reach the API as the site wrote
them.

Why JSON endpoints and not pages
--------------------------------
Measured 2026-09-28. The whole site sits behind CloudFront and PerimeterX:

    any client, datacentre address           CloudFront 403, 986 bytes,
                                             "The request could not be
                                             satisfied" — before PerimeterX
                                             is even consulted
    curl, residential address (BR or US)     PerimeterX 403, "Access to this
                                             page has been denied", its Press
                                             & Hold challenge
    headless Chromium, default UA            PerimeterX 403, 0 of 3
    headless Chromium, UA without the
      `HeadlessChrome` token                 served, 3 of 3, same address
    headful Chromium, residential            served, 2 of 2 BR and 2 of 2 US
    the Scraping Browser API (country-us)    served

The same answers hold for the JSON endpoints: the refusal is about the
CLIENT and the ADDRESS, never about the route, so the endpoints are not an
ungated side door (§21). They are simply the cheapest thing a served browser
can ask for. A browser lands on a small endpoint (ORIGIN_URL), which gives it
a www.webmotors.com.br origin, and issues every page as a same-origin
`fetch()`. Nothing is rendered, so a 3 MB listing page and its third-party
scripts are never loaded.

The API does not validate what it is given
------------------------------------------
Measured 2026-09-28, each on the live endpoint. The worst thing this site
does is accept a wrong value and answer with something plausible:

    order=0, 7, 8 or abc                  HTTP 200, the default ordering
    /carros/estoque/volkswagen/gool       HTTP 200, EVERY Volkswagen (48,983)
    /carros/estoque/zzzz                  HTTP 200, the WHOLE catalogue
    /carros/xx                            HTTP 200, the whole country
    actualPage=500 of 77                  HTTP 200, ten rows of PAGE 1, with
                                          `PageCurrent: 500` echoed back
    actualPage=78 of 77                   HTTP 200, zero rows

The ordering is allowlisted (SORTS). A make, model or state the site did not
recognise is caught after page 1 by reading what the site says it applied
(`FilterCustom`), and the run is refused with the reason rather than written
as a healthy-looking sample of something nobody asked for. Pages are planned
from the site's own `PageTotal`, never walked off the end, because walking
off it is served as page 1 again.
"""

import html as html_lib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse

from output_writer import Ad, Listing

BASE = "https://www.webmotors.com.br"
HOSTS = ("www.webmotors.com.br", "webmotors.com.br")

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

# The API's word for each vehicle type, by the word the site's own listing
# paths use. Measured: /api/search/car and /api/search/bike, and the detail
# page of a motorcycle calls /api/detail/bike/... (2026-09-28).
API_KIND = {"carros": "car", "motos": "bike"}

SEARCH_PATH = "/api/search/%s"
DETAIL_PATH = "/api/detail/%s"
AVERAGE_PRICE_PATH = "/api/detail/averageprice/%s/%s"

# Where a browser engine lands before its first request. It needs to be:
#   * on www.webmotors.com.br, so every later fetch() is same-origin;
#   * small, since it happens once per browser;
#   * behind the same gate as the data, so a refused client is refused HERE,
#     on a document the classifier can read, rather than inside a fetch().
# /api/location answers 3 KB of JSON (the edge's own geolocation of the
# exit) and was served or refused exactly as the endpoints were, in every
# row of the table in the module docstring.
ORIGIN_URL = BASE + "/api/location"

# The parameters the site's own front end sends beside `url`, copied from
# its requests (2026-09-28). `mediaZeroKm=false` leaves out the sponsored
# new-car tiles the front end would otherwise splice into the results; they
# are skipped by the parser anyway (`is_sponsored`), and asking for none
# keeps the payload the size of the page.
SEARCH_FIXED_PARAMS = (("showMenu", "true"), ("showCount", "true"),
                       ("showBreadCrumb", "true"), ("testAB", "false"),
                       ("returnUrl", "false"), ("mediaZeroKm", "false"))

# ---------------------------------------------------------------------------
# Page size and the site's cap
# ---------------------------------------------------------------------------

# The front end asks for 47 per page, so page N here is page N on the site.
# `displayPerPage` is honoured exactly: 24, 47, 100 and even 1,000 came back
# with that many rows (2026-09-28). --per-page stops at 100 anyway: every
# response is held in memory and dumped whole by --dump-html, and a 1,000-row
# page is 4 MB of JSON for no gain, since the cap below is on RESULTS, not on
# pages.
DEFAULT_PER_PAGE = 47
MAX_PER_PAGE = 100

# The site serves at most about 10,000 results per search, whatever matched.
# Measured on the unfiltered catalogue, `Count: 348317`: `PageTotal` was 417
# at 24 per page and 213 at 47 per page, i.e. 10,008 and 10,011. The page
# count the site states already includes the cap, so the engines plan against
# `PageTotal` and the sidecar records whether the cap bit (`capped_by_site`).
SITE_RESULT_CAP = 10_000

# A ceiling on --pages, so a typo cannot start a run of thousands of
# requests beyond anything the site serves: at one row per page, the cap is
# 10,000 pages.
MAX_PAGES = 10_000

# ---------------------------------------------------------------------------
# Allowlists
# ---------------------------------------------------------------------------

# --sort -> the API's `order`. These are the five the site's own "Ordenar
# Por" menu offers, by the value its menu items carry (2026-09-28). Values
# 2, 9, 10 and 11 also change the ordering, but the site names none of them,
# so what they sort BY is unknown and they are not offered. 0, 7, 8 and any
# non-number give the default ordering silently. An `o=` in a listing
# address is IGNORED by the API (measured: `?o=5` with order=1 came back in
# the default ordering), so the ordering is only ever this flag.
SORTS = {
    "relevance": 1,      # "Mais relevantes", the site's default
    "price-desc": 6,     # "Maior preço"
    "price-asc": 5,      # "Menor preço"
    "year-desc": 3,      # "Ano mais novo"
    "km-asc": 4,         # "Menor Km"
}
DEFAULT_SORT = "relevance"

VEHICLES = ("carros", "motos")
MODES = ("search", "ad")

# --condition -> the path suffix the site uses for it. Measured on cars
# only: /carros-usados/estoque (307,946) and /carros-novos/estoque (39,755)
# against /carros/estoque (348,317). The motorcycle equivalents were not
# measured, so --condition is refused with --vehicle motos rather than
# guessed.
CONDITION_SUFFIX = {"all": "", "used": "-usados", "new": "-novos"}

# Brazil's 27 federative units, as the site's own paths spell them
# (/carros/sp, /carros/rj/volkswagen). The site answers any other two
# letters with the whole country; the list lets a typo be refused before
# anything is sent.
UFS = ("ac", "al", "am", "ap", "ba", "ce", "df", "es", "go", "ma", "mg", "ms",
       "mt", "pa", "pb", "pe", "pi", "pr", "rj", "rn", "ro", "rr", "rs", "sc",
       "se", "sp", "to")

# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------


@dataclass
class ApiRequest:
    """One call to one of the site's endpoints: what an engine sends.

    Every endpoint here is a GET, so `body_json` is always None. The field
    is kept because the three engines' fetch() takes it, and an endpoint
    added later should not need three engine edits.
    """
    method: str
    path: str
    page: int
    params: Tuple[Tuple[str, Any], ...] = ()

    @property
    def url(self) -> str:
        q = ("?" + urlencode(self.params)) if self.params else ""
        return BASE + self.path + q

    @property
    def body_json(self) -> Optional[str]:
        return None

    @property
    def label(self) -> str:
        if self.path.startswith("/api/search/"):
            return "%s page=%d" % (self.path, self.page)
        return self.path


@dataclass
class Query:
    """What a run asks for, independent of the page number.

    Search mode is one listing address plus an ordering and a page size.
    The address is kept whole because the API parses the filters out of it
    itself (module docstring); `make`, `model` and `state` are what this
    repo read out of the same address, and are there only to check the
    site's own echo against after page 1 (`filter_mismatch`).

    Ad mode is a list of advert addresses. A "page" is one advert, so the
    family's page machinery (retries, rotation, concurrency, the sidecar's
    `pages_failed`) applies to adverts unchanged.
    """
    mode: str
    listing_url: str = ""
    vehicle: str = "carros"
    sort: str = DEFAULT_SORT
    per_page: int = DEFAULT_PER_PAGE
    make: Optional[str] = None
    model: Optional[str] = None
    state: Optional[str] = None
    ads: Tuple[str, ...] = ()
    # The currency the site states, read ONCE per run (page_flow) from the
    # JSON-LD of a page the site renders. No endpoint states one, and a
    # constant compiled in here would be a guess wearing a fact's clothes
    # (§4). None until read, and None for good if it could not be.
    currency: Optional[str] = None

    def validate(self) -> Optional[str]:
        """None when the query can be sent, else the reason it cannot."""
        if self.mode == "search":
            if self.vehicle not in VEHICLES:
                return "--vehicle must be one of %s" % ", ".join(VEHICLES)
            if self.sort not in SORTS:
                return "--sort must be one of %s" % ", ".join(SORTS)
            if not 1 <= int(self.per_page) <= MAX_PER_PAGE:
                return "--per-page must be between 1 and %d" % MAX_PER_PAGE
            if not self.listing_url:
                return "no listing address to search"
        elif self.mode == "ad":
            if not self.ads:
                return ("--mode ad needs at least one advert address: --url "
                        "https://www.webmotors.com.br/comprar/..., or "
                        "--ads-file")
            bad = [u for u in self.ads if parse_ad_url(u) is None]
            if bad:
                return "not an advert address: %s" % bad[0]
        else:
            return "unknown mode %r" % self.mode
        return None

    @property
    def api_kind(self) -> str:
        return API_KIND[self.vehicle]


def request_for(query: Query, page: int) -> ApiRequest:
    """The API call that fetches page `page` (1-based) of `query`.

    In ad mode, page N is the N-th advert of the list.
    """
    if query.mode == "search":
        params = (("url", query.listing_url), ("actualPage", page),
                  ("displayPerPage", int(query.per_page)),
                  ("order", SORTS[query.sort])) + SEARCH_FIXED_PARAMS
        return ApiRequest("GET", SEARCH_PATH % query.api_kind, page,
                          params=params)
    if query.mode == "ad":
        ad = parse_ad_url(query.ads[page - 1])
        if ad is None:
            raise ValueError("not an advert address: %r" % query.ads[page - 1])
        return ApiRequest("GET", DETAIL_PATH % ad.kind + ad.path, page,
                          params=(("pandora", "false"),))
    raise ValueError("unknown mode %r" % query.mode)


def average_price_request(kind: str, sku: str, page: int) -> ApiRequest:
    """The site's own market-price figures for one advert's version."""
    return ApiRequest("GET", AVERAGE_PRICE_PATH % (kind, sku), page,
                      params=(("pandora", "false"),))


def currency_page(query: Query) -> str:
    """The page whose JSON-LD the run reads its currency from.

    In search mode, the listing itself. In ad mode, the all-of-Brazil
    listing of the first advert's vehicle type: an advert page's
    server-rendered HTML is an 18 KB shell with NO JSON-LD at all (its
    structured data is drawn by the browser afterwards, 2026-09-28), while a
    listing page's carries `priceCurrency` on every item. It is the same
    site stating the currency of the same kind of price.
    """
    if query.mode == "search":
        return query.listing_url
    first = parse_ad_url(query.ads[0]) if query.ads else None
    return BASE + ("/motos/estoque" if first and first.kind == "bike"
                   else "/carros/estoque")


# ---------------------------------------------------------------------------
# Addresses: listings
# ---------------------------------------------------------------------------

_LISTING_PATH_RE = re.compile(r"^/(carros|motos)(-[a-z]+)?(?:/(.*))?$")


def _norm(text: Optional[str]) -> str:
    """Case, accents and punctuation removed: how two spellings of one make
    or model are compared (`onix-plus` in a path, `ONIX PLUS` in the echo)."""
    t = unicodedata.normalize("NFKD", (text or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", t)


def query_from_url(url: str, sort: str = DEFAULT_SORT,
                   per_page: int = DEFAULT_PER_PAGE
                   ) -> Tuple[Optional[Query], Optional[str]]:
    """Map a webmotors.com.br address onto a Query. Returns (query, None), or
    (None, reason) when the address is not one of the shapes this repo reads.

    Accepted: any listing address the site produces, whose path starts
    /carros, /carros-usados, /carros-novos or /motos, followed by `estoque`
    (all of Brazil), a state (`sp`), or a state and city (`sp-sao-paulo`),
    then optionally a make, a model and anything further. And an advert
    address (/comprar/...), which becomes an ad-mode query of one.

    A `page=` in the query string is dropped: the API pages with its own
    `actualPage` parameter, and a run starts at page 1 (use --pages).
    """
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if host not in HOSTS:
        return None, ("%r is not a webmotors.com.br address."
                      % (parsed.hostname or url))
    path = re.sub(r"/+$", "", parsed.path or "") or "/"

    if path.startswith("/comprar/"):
        if parse_ad_url(url) is None:
            return None, ("%s looks like an advert address but does not end "
                          "in its numeric id." % url)
        return Query(mode="ad", ads=(canonical_ad_url(url),)), None

    m = _LISTING_PATH_RE.match(path)
    if not m:
        return None, ("%s is not a page this repo reads. Supported: a search "
                      "page (/carros/estoque/..., /motos/sp/...) or an advert "
                      "(/comprar/...)." % url)
    vehicle = m.group(1)
    rest = [s for s in (m.group(3) or "").split("/") if s]
    location = rest[0] if rest else "estoque"
    state = None
    if location != "estoque":
        uf = location.split("-", 1)[0]
        if uf not in UFS:
            return None, ("%r in %s is not a Brazilian state. The site answers "
                          "an unknown one with the WHOLE country, so the run "
                          "is refused before it starts. Use `estoque` for all "
                          "of Brazil, or one of: %s."
                          % (location, url, ", ".join(UFS)))
        state = uf.upper()
    make = rest[1] if len(rest) > 1 else None
    model = rest[2] if len(rest) > 2 else None
    qs = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
          if k.lower() != "page"]
    listing = BASE + path + (("?" + urlencode(qs)) if qs else "")
    return Query(mode="search", listing_url=listing, vehicle=vehicle,
                 sort=sort, per_page=per_page, make=make, model=model,
                 state=state), None


def listing_url_from_parts(vehicle: str = "carros", condition: str = "all",
                           state: Optional[str] = None,
                           make: Optional[str] = None,
                           model: Optional[str] = None
                           ) -> Tuple[Optional[str], Optional[str]]:
    """The listing address the site itself would use for these filters, or
    (None, reason). Built so that --make/--model and --url go through ONE
    path: the site's own address, parsed by the site's own API."""
    if vehicle not in VEHICLES:
        return None, "--vehicle must be one of %s" % ", ".join(VEHICLES)
    if condition not in CONDITION_SUFFIX:
        return None, "--condition must be one of %s" % ", ".join(CONDITION_SUFFIX)
    if condition != "all" and vehicle != "carros":
        return None, ("--condition is only offered for cars: the site's "
                      "/carros-usados and /carros-novos listings were "
                      "measured, the motorcycle ones were not.")
    if model and not make:
        return None, "--model needs --make"
    loc = "estoque"
    if state:
        st = state.strip().lower()
        if st not in UFS:
            return None, ("--state %r is not a Brazilian state (%s). The site "
                          "answers an unknown one with the whole country."
                          % (state, ", ".join(UFS)))
        loc = st
    parts = [vehicle + CONDITION_SUFFIX[condition], loc]
    for p in (make, model):
        if p:
            parts.append(slugify(p))
    return BASE + "/" + "/".join(parts), None


# ---------------------------------------------------------------------------
# Addresses: adverts
# ---------------------------------------------------------------------------

def slugify(text: Optional[str]) -> str:
    """The site's own slug rule, as its advert links spell it.

    Derived from and verified against 660 advert links the site itself put
    in its listing pages (cars and motorcycles, 15 searches under two
    orderings, 2026-09-28), 660 of 660 identical:

        accents removed            AUTOMÁTICO   -> automatico, CITROËN -> citroen
        anything but [a-z0-9 -]    1.6 -> 16, G.III -> giii, 4MATIC+ -> 4matic
          dropped
        whitespace -> "-"          RANGE ROVER  -> range-rover

    It matters more than a slug usually does: the detail endpoint answers
    a wrong slug with HTTP 404 and the body `null`, the same answer as for
    an advert that is gone.
    """
    t = unicodedata.normalize("NFKD", (text or "").strip().lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-z0-9\s-]", "", t)
    return re.sub(r"\s+", "-", t.strip())


def _years(fabrication: Any, model: Any) -> str:
    """`2013-2014`, or one year when the two agree: the site writes a 2024
    car built in 2024 as `/2024/`, never `/2024-2024/`."""
    yf, ym = _int(fabrication), _int(model)
    if yf is None:
        return str(ym) if ym is not None else ""
    if ym is None or ym == yf:
        return str(yf)
    return "%d-%d" % (yf, ym)


def ad_url(rec: Dict[str, Any], vehicle: str) -> str:
    """An advert's address, built from its search record as the site builds it.

        car         /comprar/{make}/{model}/{version}/{doors}-portas/{years}/{id}
        motorcycle  /comprar/{make}/{model}/{cc}cc/{years}/{id}

    A motorcycle record has no version at all; the site's third segment is
    its displacement, and a displacement of 0 (not stated) is written `cc`.
    """
    sid = _int(rec.get("UniqueId"))
    spec = rec.get("Specification") if isinstance(rec.get("Specification"), dict) else {}
    if not sid:
        return ""
    make, model = slugify(_value(spec.get("Make"))), slugify(_value(spec.get("Model")))
    years = _years(spec.get("YearFabrication"), spec.get("YearModel"))
    if vehicle == "motos":
        cc = _float(spec.get("CubicCentimeter"))
        third = ("%dcc" % int(cc)) if cc else "cc"
        parts = [make, model, third, years, str(sid)]
    else:
        parts = [make, model, slugify(_value(spec.get("Version"))),
                 "%s-portas" % (_str(spec.get("NumberPorts")) or ""), years,
                 str(sid)]
    return BASE + "/comprar/" + "/".join(parts)


@dataclass
class AdAddress:
    path: str       # /{make}/{model}/.../{id}, the part after /comprar
    sku: str
    kind: str       # car | bike


_AD_PATH_RE = re.compile(r"^/comprar(/(?:[^/?#]+/)+(\d+))/?$")
# A car's advert path carries its doors as `{n}-portas`; a motorcycle's does
# not. Measured on the 660 links above, and the ONLY difference between the
# two shapes: 600 of 600 car links carried the segment, 0 of 60 motorcycle
# links did.
_DOORS_SEGMENT_RE = re.compile(r"/\d+-portas/")


def parse_ad_url(url: str) -> Optional[AdAddress]:
    parsed = urlparse((url or "").strip())
    if (parsed.hostname or "").lower() not in HOSTS:
        return None
    m = _AD_PATH_RE.match(parsed.path or "")
    if not m:
        return None
    path = m.group(1)
    kind = "car" if _DOORS_SEGMENT_RE.search(path) else "bike"
    return AdAddress(path=path, sku=m.group(2), kind=kind)


def canonical_ad_url(url: str) -> str:
    ad = parse_ad_url(url)
    return BASE + "/comprar" + ad.path if ad else url


def ads_from_text(text: str) -> List[str]:
    """Advert addresses from an --ads-file: a search run's JSON output (its
    `url` column), or plain text with one address per line. Order kept,
    duplicates dropped, anything that is not an advert address ignored."""
    urls: List[str] = []
    stripped = (text or "").lstrip()
    if stripped.startswith("["):
        try:
            rows = json.loads(stripped)
        except ValueError:
            rows = []
        for r in rows if isinstance(rows, list) else []:
            if isinstance(r, dict) and isinstance(r.get("url"), str):
                urls.append(r["url"])
    else:
        urls = [ln.strip() for ln in (text or "").splitlines()]
    out, seen = [], set()
    for u in urls:
        ad = parse_ad_url(u)
        if ad and ad.sku not in seen:
            seen.add(ad.sku)
            out.append(canonical_ad_url(u))
    return out


# ---------------------------------------------------------------------------
# Page state
# ---------------------------------------------------------------------------

# PerimeterX's refusal page, counted on 2026-09-28:
#                                          refusal   served listing/ad/JSON
#   _pxAppId                                   1           0
#   captcha.px-cloud.net                       1           0
#   /captcha/captcha.js                        2           0
#   "Access to this page has been denied"      1           0
# `px-captcha` (42 on the refusal) is deliberately NOT a marker: it is the
# id of the element the challenge renders into, and a bare element id is the
# shape §24 warns about, a thing another script could carry. The bare words
# "captcha" and "perimeterx" are not markers either: the Scraping Browser's
# auto-solve extension injects hunter scripts that say "captcha" on every
# page it loads (§24); smoke_test scores this set against a page fetched
# that way.
PERIMETERX_MARKERS = ("_pxAppId", "captcha.px-cloud.net", "/captcha/captcha.js",
                      "Access to this page has been denied")

# CloudFront refusing the ADDRESS, before PerimeterX sees the request: the
# 986-byte page every datacentre client got, headful Chromium included.
CLOUDFRONT_MARKERS = ("The request could not be satisfied",)


def detect_bot_challenge(html: Optional[str], url: str = "") -> Optional[str]:
    """The vendor whose refusal this is, or None.

    Entities are unescaped over a bounded prefix first, so a marker matches
    the raw bytes an HTTP client gets and the DOM a browser serialises
    alike (§20). Both refusal pages are under 12 KB.
    """
    head = html_lib.unescape((html or "")[:60_000])
    if any(m in head for m in PERIMETERX_MARKERS):
        return "perimeterx"
    if "CloudFront" in head and any(m in head for m in CLOUDFRONT_MARKERS):
        return "cloudfront"
    return None


def _json_or_none(text: Optional[str]) -> Any:
    if not text:
        return None
    s = text.strip()
    if not s or s[0] not in "{[":
        return None
    try:
        return json.loads(s)
    except ValueError:
        return None


def _as_dict(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, (str, bytes)):
        got = _json_or_none(payload if isinstance(payload, str)
                            else payload.decode("utf-8", "replace"))
        return got if isinstance(got, dict) else {}
    return {}


def detect_page_state(text: Optional[str], status: Optional[int] = None,
                      url: str = "", waf_action: Optional[str] = None) -> str:
    """Name what the site answered with. See page_flow.STATE_POLICY.

        content     a search payload with listings in it, or an advert
        empty       a search payload with none: an answer, not a failure
        gone        the detail endpoint's HTTP 404 with the body `null`: the
                    advert is sold or withdrawn, or its address is not the
                    site's own (a wrong slug gets the same answer)
        rejected    the API gateway refused the PATH: HTTP 403 with
                    {"message": "Missing Authentication Token"}, which is
                    what a detail request without its slug gets
        challenge   PerimeterX's refusal page (Press & Hold)
        blocked     CloudFront refusing the address, or any other 403
        throttled   429. NOT OBSERVED on this site by this repo; here
                    because a 429 means the same thing wherever it appears
        unknown     anything else

    Signals are ordered by what they PROVE, not by what they cost (§17): the
    site's own JSON envelope first, because no refusal page carries it.
    `waf_action` is accepted for the family's signature and ignored: this
    site has no AWS WAF.
    """
    payload = _json_or_none(text)
    if isinstance(payload, dict):
        if "SearchResults" in payload:
            return "content" if count_rows(payload) > 0 else "empty"
        if _int(payload.get("UniqueId")):
            return "content"
        if status == 403 and isinstance(payload.get("message"), str):
            return "rejected"
    if status == 404 and (text or "").strip() == "null":
        return "gone"
    vendor = detect_bot_challenge(text)
    if vendor == "perimeterx":
        return "challenge"
    if vendor == "cloudfront" or status == 403:
        return "blocked"
    if status == 429:
        return "throttled"
    return "unknown"


def api_error(text: Optional[str]) -> Optional[str]:
    """The gateway's own complaint, for a `rejected` response."""
    msg = _as_dict(text).get("message")
    return ("HTTP 403: %s" % msg) if isinstance(msg, str) and msg else None


# ---------------------------------------------------------------------------
# Payload access
# ---------------------------------------------------------------------------

def is_sponsored(rec: Dict[str, Any]) -> bool:
    """A sponsored new-car tile the site splices into the results.

    Measured 2026-09-28: 3 of 50 records on a page asked with
    `mediaZeroKm=true`, each with `UniqueId: 0`, `MediaZeroKm: true`, no
    `Seller` and an `AdvertisementLink` to a dealer's lead form. They are not
    listings of this search, they have no id to key on, and counting them
    would shift every position after them (§24). Each condition alone
    identifies them; all three are checked so a tile that loses one of the
    three still does not become a row.
    """
    return (not _int(rec.get("UniqueId")) or bool(rec.get("MediaZeroKm"))
            or "Seller" not in rec)


def _records(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = payload.get("SearchResults")
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def count_rows(payload: Any) -> int:
    return sum(1 for r in _records(_as_dict(payload)) if not is_sponsored(r))


def sponsored_count(payload: Any) -> int:
    return sum(1 for r in _records(_as_dict(payload)) if is_sponsored(r))


def total_results(payload: Any, mode: str = "search") -> Optional[int]:
    """The site's own count of everything that matched, from page 1."""
    if mode != "search":
        return None
    t = _as_dict(payload).get("Count")
    return t if isinstance(t, int) and t >= 0 else None


def pages_available(payload: Any, mode: str = "search") -> Optional[int]:
    """The site's own page count, which already includes its ~10,000-result
    cap (SITE_RESULT_CAP). Read from page 1 only: `PageCurrent` on a later
    page just echoes what was asked, even past the end."""
    if mode != "search":
        return None
    pag = _as_dict(payload).get("Pagination")
    t = pag.get("PageTotal") if isinstance(pag, dict) else None
    return t if isinstance(t, int) and t >= 0 else None


def filter_mismatch(payload: Any, query: Query) -> Optional[str]:
    """None when the site applied the make, model and state the address
    asked for, else a refusal saying what it applied instead.

    The search answers an unrecognised make with the whole catalogue and an
    unrecognised model with the whole make (module docstring), HTTP 200
    both times. What it DID apply is in `FilterCustom`: `Veiculos` lists the
    make and model, `Sigla` the state. Reading that echo is the only way to
    tell "no such model" from a result, short of knowing every model name.
    """
    if query.mode != "search":
        return None
    fc = _as_dict(payload).get("FilterCustom")
    if not isinstance(fc, dict):
        return None  # no echo to check against: nothing is concluded
    vehicles = fc.get("Veiculos") if isinstance(fc.get("Veiculos"), list) else []
    first = vehicles[0] if vehicles and isinstance(vehicles[0], dict) else {}
    applied_make, applied_model = first.get("Marca"), first.get("Modelo")
    problems = []
    if query.make and _norm(applied_make) != _norm(query.make):
        problems.append("make %r (the site applied %s)"
                        % (query.make, "no make at all" if not applied_make
                           else repr(applied_make)))
    elif query.model and _norm(applied_model) != _norm(query.model):
        problems.append("model %r (the site applied %s)"
                        % (query.model, "every %s model" % applied_make
                           if not applied_model else repr(applied_model)))
    applied_state = (_str(fc.get("Sigla")) or "").upper()
    if query.state and applied_state != query.state:
        problems.append("state %r (the site applied %s)"
                        % (query.state, applied_state or "the whole country"))
    if not problems:
        return None
    total = total_results(payload)
    return ("The site did not recognise the %s, and answered with HTTP 200 "
            "and %s listing(s) anyway. Writing those would be a sample of "
            "something nobody asked for, so the run is refused. Check the "
            "spelling against the site's own address for that page."
            % ("; ".join(problems),
               "{:,}".format(total) if total is not None else "some"))


# ---------------------------------------------------------------------------
# Currency, from a page the site renders
# ---------------------------------------------------------------------------

_JSONLD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I)
# ISO 4217 codes a Brazilian marketplace could plausibly state. An
# allowlist, never a bare [A-Z]{3}, so nothing that merely looks like a code
# is read as one (§4).
_ISO_CODES = ("BRL", "USD", "EUR", "ARS", "UYU", "PYG", "CLP")


def currency_from_html(html: Optional[str]) -> Optional[str]:
    """The `priceCurrency` a listing or advert page's JSON-LD states.

    Measured 2026-09-28: a listing page carries it on every item of its
    OfferCatalog (48 of 48 on the page read), an advert page once. No
    endpoint states a currency at all, so this is read once per run and
    threaded into the rows (page_flow), and a run that could not read it
    writes null rather than a default (§4).
    """
    found = []
    for m in _JSONLD_RE.finditer(html or ""):
        for cur in re.findall(r'"priceCurrency"\s*:\s*"([A-Z]{3})"', m.group(1)):
            if cur in _ISO_CODES:
                found.append(cur)
    if not found:
        return None
    # One currency or none: a page stating two is not a page this reads.
    return found[0] if len(set(found)) == 1 else None


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------

def _float(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _int(v: Any) -> Optional[int]:
    f = _float(v)
    return int(f) if f is not None and f == int(f) else None


def _str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = re.sub(r"\s+", " ", str(v)).strip()
    return s or None


def _value(v: Any) -> Optional[str]:
    """The site wraps many values as {"id": .., "Value": ".."}."""
    if isinstance(v, dict):
        return _str(v.get("Value"))
    return _str(v)


def _names(items: Any, key: str = "Name") -> Optional[List[str]]:
    if not isinstance(items, list):
        return None
    out = [_str(i.get(key)) for i in items if isinstance(i, dict)]
    out = [x for x in out if x]
    return out or None


def _uf(state: Optional[str]) -> Optional[str]:
    """`São Paulo (SP)` -> `SP`."""
    m = re.search(r"\(([A-Z]{2})\)\s*$", state or "")
    return m.group(1) if m else None


def _leading_int(text: Any) -> Optional[int]:
    """`104 cv` -> 104."""
    m = re.match(r"\s*(\d+)", str(text or ""))
    return int(m.group(1)) if m else None


def _iso_date(v: Any) -> Optional[str]:
    """`2026-07-08T10:42:57.333` -> the same, validated. The site's own
    placeholder for "no date", `0001-01-01T00:00:00`, is None."""
    s = _str(v)
    if not s:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if not m or int(m.group(1)) < 1990:
        return None
    try:
        datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    return s


# The base the site's own JSON-LD serves every photo from: 627 of 627
# listing images checked matched this prefix + the record's `PhotoPath`,
# used and new, cars and motorcycles (2026-09-28). A record with no
# `PhotoPath` is shown the site's "no photo" placeholder, which is not a
# photo of the vehicle, so it is null here.
PHOTO_BASE = "https://image.webmotors.com.br/_fotos/anunciousados/gigante/"


def _photo_url(path: Any) -> Optional[str]:
    p = _str(path)
    if not p:
        return None
    if p.startswith("http"):
        return p
    return PHOTO_BASE + p.replace("\\", "/").lstrip("/")


# The site's seller types, by `SellerType`. `AdType.Value` says the same in
# Portuguese and splits the dealers in two ("Loja" / "Concessionária"); it
# is kept as `seller_kind` because a franchise dealer and a used-car lot are
# different things to a buyer.
SELLER_TYPES = {"PF": "private", "PJ": "dealer"}


def _seller_fields(seller: Dict[str, Any]) -> Dict[str, Any]:
    """The seller columns every row carries.

    A PRIVATE seller (`SellerType: PF`) is a person. The search record holds
    their city and postal code, and the detail record adds their first name
    and part of their phone number. None of that is written beyond the city:
    the name and phone are never read, the postal code of a person's home is
    not a column, and a private seller's `seller_name` is always null. A
    dealer's trading name (`FantasyName`) is a business's public name and is
    kept.
    """
    kind = SELLER_TYPES.get(_str(seller.get("SellerType")) or "")
    return {
        "seller_id": _str(seller.get("Id")),
        "seller_type": kind,
        "seller_kind": _value(seller.get("AdType")),
        "seller_name": _str(seller.get("FantasyName")) if kind == "dealer" else None,
        "city": _str(seller.get("City")),
        "state": _uf(seller.get("State")),
    }


def _vehicle_fields(rec: Dict[str, Any], vehicle: str) -> Dict[str, Any]:
    spec = rec.get("Specification") if isinstance(rec.get("Specification"), dict) else {}
    media = rec.get("Media") if isinstance(rec.get("Media"), dict) else {}
    photos = media.get("Photos") if isinstance(media.get("Photos"), list) else []
    first_photo = rec.get("PhotoPath")
    if not first_photo and photos and isinstance(photos[0], dict):
        first_photo = photos[0].get("PhotoPath")
    color = spec.get("Color") if isinstance(spec.get("Color"), dict) else {}
    cc = _int(spec.get("CubicCentimeter"))
    return {
        "vehicle": "car" if vehicle == "carros" else "motorcycle",
        # `ListingType`: U = used, N = new (0 km).
        "condition": {"U": "used", "N": "new"}.get(_str(rec.get("ListingType")) or ""),
        "make": _value(spec.get("Make")),
        "model": _value(spec.get("Model")),
        "version": _value(spec.get("Version")),
        "year_fabrication": _int(spec.get("YearFabrication")),
        "year_model": _int(spec.get("YearModel")),
        "odometer_km": _int(spec.get("Odometer")),
        # A car states `Transmission`; a motorcycle states `Shift`.
        "transmission": _str(spec.get("Transmission")) or _value(spec.get("Shift")),
        "body_type": _str(spec.get("BodyType")),
        "color": _str(color.get("Primary")),
        "doors": _int(spec.get("NumberPorts")),
        "engine_litres": _float(spec.get("EngineSize")),
        # 0 is the site's "not stated" (a BMW G 310 R listed at 0 cc), so
        # it is null rather than a displacement of nothing.
        "engine_cc": cc if cc else None,
        "horsepower_cv": _leading_int(spec.get("HorsePower")),
        "traction": _str(spec.get("Traction")),
        # `S`/`N` (sim/não) on cars; absent on motorcycles.
        "armored": {"S": True, "N": False}.get(_str(spec.get("Armored")) or ""),
        "attributes": _names(spec.get("VehicleAttributes")),
        # How the asking price compares with the FIPE table value, in
        # percent (105 = 5% above).
        "fipe_pct": _int(rec.get("FipePercent")),
        # The site's "Bom negócio" badge. It is either `true` or absent,
        # never `false` (0 of 3,000 records), so absent is written False:
        # no badge shown.
        "good_deal": bool(rec.get("GoodDeal")),
        "photo_count": len(photos),
        "image_url": _photo_url(first_photo),
    }


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

def parse_listing(rec: Dict[str, Any], query: Query, *, page: int,
                  position: int) -> Optional[Listing]:
    if is_sponsored(rec):
        return None
    spec = rec.get("Specification") if isinstance(rec.get("Specification"), dict) else {}
    prices = rec.get("Prices") if isinstance(rec.get("Prices"), dict) else {}
    seller = rec.get("Seller") if isinstance(rec.get("Seller"), dict) else {}
    url = ad_url(rec, query.vehicle)
    if not url:
        return None
    return Listing(
        url=url,
        sku=str(_int(rec.get("UniqueId"))),
        title=_str(spec.get("Title")),
        # `Price` and `SearchPrice` agreed on 3,000 of 3,000 records.
        price=_float(prices.get("Price")),
        currency=query.currency,
        **_vehicle_fields(rec, query.vehicle),
        auction=bool(spec.get("Auction")),
        **_seller_fields(seller),
        page=page,
        position=position,
        mode="search",
        sort=query.sort,
        data_source="api-search",
    )


def parse_ad(payload: Any, query: Query, page: int = 1) -> Optional[Ad]:
    rec = _as_dict(payload)
    sid = _int(rec.get("UniqueId"))
    if not sid:
        return None
    vehicle = "motos" if _str(rec.get("Type")) == "bike" else "carros"
    spec = rec.get("Specification") if isinstance(rec.get("Specification"), dict) else {}
    prices = rec.get("Prices") if isinstance(rec.get("Prices"), dict) else {}
    seller = rec.get("Seller") if isinstance(rec.get("Seller"), dict) else {}
    evaluation = spec.get("Evaluation") if isinstance(spec.get("Evaluation"), dict) else {}
    sfields = _seller_fields(seller)
    address = parse_ad_url(query.ads[page - 1]) if 0 < page <= len(query.ads) else None
    fipe = _float(evaluation.get("FIPE"))
    return Ad(
        url=(BASE + "/comprar" + address.path) if address else "",
        sku=str(sid),
        title=_str(spec.get("Title")),
        price=_float(prices.get("Price")),
        currency=query.currency,
        **_vehicle_fields(rec, vehicle),
        fuel=_str(spec.get("Fuel")),
        final_plate=_int(spec.get("FinalPlate")),
        optionals=_names(spec.get("Optionals")),
        # A dealer's description is its sales copy. A private seller's is a
        # person writing about their own car, often with their name or
        # number in it (the one private fixture captured signs off with a
        # name), so it is not written (see _seller_fields).
        description=(_str(rec.get("LongComment"))
                     if sfields["seller_type"] == "dealer" else None),
        created_at=_iso_date(rec.get("CreatedDate")),
        fipe_code=_str(evaluation.get("FIPEId")),
        fipe_price=fipe if fipe else None,
        **sfields,
        page=page,
        position=1,
        mode="ad",
        data_source="api-detail",
    )


def apply_market_prices(row: Ad, payload: Any) -> bool:
    """Fold the averageprice endpoint's figures into an advert row.

    What it returns (2026-09-28): the version's FIPE code and value, the
    state its figures cover, and the smallest, medium and biggest price the
    site holds for that version and year. True if a price was applied.
    """
    p = _as_dict(payload)
    if not p:
        return False
    row.market_price_min = _float(p.get("SmallestPrice")) or None
    row.market_price_avg = _float(p.get("MediumPrice")) or None
    row.market_price_max = _float(p.get("BiggestPrice")) or None
    row.market_state = _str(p.get("State"))
    if not row.fipe_code:
        row.fipe_code = _str(p.get("FipeCode"))
    if not row.fipe_price:
        row.fipe_price = _float(p.get("FipePrice")) or None
    return any(v is not None for v in (row.market_price_min, row.market_price_avg,
                                       row.market_price_max))


def parse_page(payload: Any, query: Query, page: int = 1) -> List[Any]:
    """Every row on one page of one mode's response, in the site's order.

    `position` counts the rows EMITTED, not the slots in the payload, so a
    sponsored tile the parser drops cannot shift every later position (§24).
    """
    if query.mode == "ad":
        row = parse_ad(payload, query, page)
        return [row] if row else []
    rows: List[Any] = []
    for rec in _records(_as_dict(payload)):
        row = parse_listing(rec, query, page=page, position=len(rows) + 1)
        if row:
            rows.append(row)
    return rows
