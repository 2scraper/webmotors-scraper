"""
page_flow.py
------------
The retry / solve / blocked decision, as DATA rather than as three copies of
an if-chain (CLAUDE.md §1), and the one fetch loop all three engines share.

webmotors.com.br answers one of this repo's requests in seven ways, and they
want five different responses:

    a search payload with listings, or an advert        -> parse
    a search payload with none                          -> parse, it is an answer
    HTTP 404, body `null`, on an advert                 -> record it as gone:
                                                           sold, withdrawn, or
                                                           not the site's own
                                                           address for it
    HTTP 403 {"message": ...} from the API gateway      -> stop: the PATH was
                                                           refused, and a retry
                                                           sends it again
    PerimeterX's refusal page                           -> a fresh browser, or
                                                           another exit
    CloudFront's 403 (the ADDRESS)                      -> another exit
    anything else                                       -> retry

Three copies of that triage across three engines would drift, and the drift
would be silent: one engine reporting exit 3 where its twin reports exit 0
on the same response.

Nothing here imports a browser, and **no JavaScript crosses this boundary**
(§1). Each engine spells its fetch() in its own driver's dialect.
"""

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from output_writer import dedupe_by_key, finish_run, SOURCE_DEFAULT
from product_parser import (MAX_PAGES, ORIGIN_URL, ApiRequest, Query, BASE,
                            DEFAULT_PER_PAGE, DEFAULT_SORT,
                            ads_from_text, api_error, apply_market_prices,
                            average_price_request, currency_from_html,
                            currency_page, detect_bot_challenge,
                            detect_page_state, filter_mismatch,
                            listing_url_from_parts, pages_available,
                            parse_ad_url, parse_page, query_from_url,
                            request_for, sponsored_count, total_results,
                            count_rows)

log = logging.getLogger("page_flow")


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

# How long one fetch() may take before the engine gives up on it. The
# largest response measured was a 100-row search page, ~410 KB, which
# arrived in about a second through a residential exit. The bound exists
# because a browser fetch() has no timeout of its own, and CLAUDE.md §8
# requires every remote call to have one.
FETCH_TIMEOUT_MS = 30_000

# How long to wait at the SAME exit after a 429 before trying again. NOT
# OBSERVED on this site: 60+ requests in one session at one per second or
# faster never drew one (2026-09-28). The wait is kept because a throttle,
# wherever it appears, wants the opposite of a block: slow down, same exit.
THROTTLE_WAIT_S = 10.0
THROTTLE_RETRIES = 2


# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------

def classify(html: Optional[str], status: Optional[int] = None,
             url: str = "", waf_action: Optional[str] = None) -> str:
    """Name what the site answered with. See product_parser.detect_page_state.

    The argument ORDER is the contract: every engine calls
    `classify(html, status, url, waf_action)`. A sibling repo shipped
    `classify(html, url=...)` in two of three engines against a callee that
    took `status` second, and both crashed on their first fetch (§17).
    `smoke_test.py` binds every engine's call against this signature for
    that reason.
    """
    return detect_page_state(html or "", status, url, waf_action)


STATE_POLICY = {
    "content":   {"retry": False, "solve": False, "blocked": False, "parse": True},
    # A search that matched nothing. The site served exactly what was asked
    # for, so this is EXIT_NO_PRODUCTS rather than EXIT_BLOCKED.
    "empty":     {"retry": False, "solve": False, "blocked": False, "parse": True},
    # An advert the detail endpoint answers 404 `null` for. Not a failure of
    # the run: the site has said the advert is not there. It is recorded in
    # the sidecar (`ads_gone`) and the run goes on to the next advert.
    "gone":      {"retry": False, "solve": False, "blocked": False, "parse": False},
    # The API gateway refused the PATH ("Missing Authentication Token"). The
    # same request sent again gets the same answer.
    "rejected":  {"retry": False, "solve": False, "blocked": False, "parse": False},
    # PerimeterX refused this CLIENT. Nothing is solved: this repo does not
    # implement an answer to its Press & Hold challenge, and no client this
    # repo drives was shown it once the fixes in the engines were in (the
    # table in product_parser's docstring). A fresh browser, or another
    # exit, is what changes the answer, hence retry.
    "challenge": {"retry": True,  "solve": False, "blocked": True,  "parse": False},
    # CloudFront refusing the ADDRESS, or any other 403.
    "blocked":   {"retry": True,  "solve": False, "blocked": True,  "parse": False},
    # Rate limited: the retry happens at the same exit after a wait
    # (THROTTLE_*), and it is NOT counted as blocked. Calling a throttle a
    # block reports exit 3 for a page that was about to come back (§24).
    "throttled": {"retry": True,  "solve": False, "blocked": False, "parse": False},
    # Not JSON and not a refusal. Worth one more try.
    "unknown":   {"retry": True,  "solve": False, "blocked": False, "parse": False},
}


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["retry"]


def should_solve(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["solve"]


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["blocked"]


def should_parse(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["parse"]


# Whether a blocked page is worth re-fetching at all. CONSULTED by the loop
# below, so setting it False really does stop the retry (§17).
RETRY_ON_BLOCKED = True

# How many times to re-fetch a refused page, each from a FRESH browser, when
# there is no proxy pool to rotate into. Three, because the usual single
# --proxy here is a ROTATING residential gateway, which hands each new
# browser a new exit, and CloudFront accepts only some of them. Measured
# 2026-09-28, twelve fresh browsers each: a `-region-br` login was served 5
# of 12 times (6 CloudFront refusals), a `-region-us` login 9 of 12. One
# retry would leave a Brazilian run at roughly two chances in three; three
# retries cost a second or two each. WITH a pool the loop retries once per
# remaining exit instead (--proxy-block-retries).
BLOCK_RETRIES_WITHOUT_POOL = 3


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def pages_to_plan(pages_requested: int, pages_available: Optional[int]) -> int:
    """How many pages a run may ask for, given the total page 1 reported.

    Every search states its page count on page 1, and walking past it is
    not harmless here: a page beyond the end is answered with rows of PAGE
    ONE again (actualPage=500 of 77 returned ten of them, 2026-09-28), so
    the plan never asks for one.
    """
    ceiling = MAX_PAGES if pages_available is None else min(pages_available, MAX_PAGES)
    return max(1, min(int(pages_requested), ceiling))


def concurrency_limit(cdp_endpoint: Optional[str]) -> Optional[int]:
    """1 when workers would collide, else None for "no limit imposed here".

    The Scraping Browser API allows ONE live connection per profile, so N
    workers sharing a `pid` collide with `profile_locked`. Several `pid`s,
    one run each, is the way to parallelise that path (§7).
    """
    return 1 if cdp_endpoint else None


# ---------------------------------------------------------------------------
# Refusals: how they are named and what the reader is told
# ---------------------------------------------------------------------------

def refusal_name(state: str, text: Optional[str] = None) -> str:
    """The name a refusal is reported by, in logs and in `stop_reason`."""
    if state == "challenge":
        return "perimeterx"
    if state == "blocked":
        return detect_bot_challenge(text) or "http-403"
    return state


def refusal_advice(state: str, text: Optional[str] = None) -> str:
    """One sentence on what changes the answer, per refusal. Kept here so
    the three engines cannot give three different pieces of advice."""
    if state == "challenge":
        return ("PerimeterX refused this browser. Measured: it refuses a "
                "headless browser whose user agent says `HeadlessChrome` "
                "(0 of 3) and serves one that does not (3 of 3), which is why "
                "the engines override the user agent; a headful browser was "
                "served too. If it still refuses, try --headful, a different "
                "residential exit (--proxy), or --cdp-endpoint. This repo does "
                "not implement solving PerimeterX's Press & Hold challenge.")
    if (text and detect_bot_challenge(text) == "cloudfront") or state == "blocked":
        return ("CloudFront refused this ADDRESS before the site saw the "
                "request. Every datacentre address measured was refused, "
                "headful Chromium included; residential exits in Brazil and "
                "the US were served. Use a residential --proxy / --proxy-file, "
                "or --cdp-endpoint.")
    return ""


def stop_reason_for(outcome) -> str:
    """The run's stop_reason when `outcome` is the page that ended it."""
    if getattr(outcome, "unread", False):
        return "parser_found_nothing"
    if getattr(outcome, "rejected", None):
        return "api_rejected"
    if getattr(outcome, "blocked_by", None):
        return "blocked_%s" % outcome.blocked_by
    if getattr(outcome, "state", None) == "throttled":
        return "throttled"
    return "page_load_timeout"


# ---------------------------------------------------------------------------
# The query, and the end of a run
# ---------------------------------------------------------------------------

# The filter flags, by the argparse dest they land in. A flag left at None
# was not typed, which is how build_query tells a user's value from a
# default.
FILTER_FLAGS = (("category", "--category"), ("condition", "--condition"),
                ("state", "--state"), ("make", "--make"), ("model", "--model"))


def build_query(args, error: Callable[[str], None]) -> Query:
    """The Query a run sends, from --url, --ads-file or the flags, validated.

    --url and the filter flags are two ways to say the same thing. A flag
    the user typed that disagrees with the URL would silently scrape
    something neither of them named, so the combination is refused rather
    than merged (the family's --country rule, §10). `error` is argparse's
    `p.error`, so a refusal is exit 2 with the usage line, as in every
    engine.
    """
    sort = args.sort or DEFAULT_SORT
    per_page = args.per_page or DEFAULT_PER_PAGE
    typed = [flag for dest, flag in FILTER_FLAGS
             if getattr(args, dest, None) is not None]
    ads_file = getattr(args, "ads_file", None)
    if ads_file:
        if args.url or typed:
            error("--ads-file is a list of adverts; it cannot be combined "
                  "with --url or the search filters.")
        try:
            with open(ads_file, encoding="utf-8") as f:
                ads = ads_from_text(f.read())
        except OSError as e:
            error("cannot read --ads-file: %s" % e)
        if not ads:
            error("--ads-file %s holds no advert address "
                  "(https://www.webmotors.com.br/comprar/...)" % ads_file)
        query = Query(mode="ad", ads=tuple(ads))
    elif args.url:
        query, why = query_from_url(args.url, sort=sort, per_page=per_page)
        if query is None:
            error(why)
        if typed:
            error("--url already carries the search; %s would have to agree "
                  "with it and nothing checks that they do. Pass a URL or the "
                  "flags, not both." % ", ".join(typed))
    else:
        if args.mode == "ad":
            error("--mode ad needs --url (an advert address) or --ads-file.")
        url, why = listing_url_from_parts(args.category or "carros",
                                          args.condition or "all",
                                          args.state, args.make, args.model)
        if url is None:
            error(why)
        query, why = query_from_url(url, sort=sort, per_page=per_page)
        if query is None:
            error(why)
    if args.mode and args.mode != query.mode:
        error("--mode %s disagrees with the input, which is %s."
              % (args.mode, "an advert list" if query.mode == "ad"
                 else "a search page"))
    if query.mode == "ad":
        if args.sort or args.per_page:
            error("--sort and --per-page apply to a search, not to adverts.")
        # One advert is one "page" of this run (Query's docstring).
        if args.pages not in (1, len(query.ads)):
            log.info("--pages is ignored in ad mode: every advert of the list "
                     "is fetched (%d).", len(query.ads))
        args.pages = len(query.ads)
    why = query.validate()
    if why:
        error(why)
    if args.pages < 1:
        error("--pages must be at least 1")
    return query


def query_summary(query: Query) -> dict:
    """The query as the sidecar records it: only the fields its mode uses."""
    if query.mode == "search":
        return {"listing_url": query.listing_url, "category": query.vehicle,
                "sort": query.sort, "per_page": query.per_page,
                "make": query.make, "model": query.model, "state": query.state}
    # The SET of adverts asked for, not how many. v0.1.0 recorded only the
    # count, so two runs over two different lists of the same length read as
    # one question and diff_runs compared them (third-party audit,
    # 2026-10-08). The hash is over the sorted ids, so the order a file
    # lists them in does not matter, and adverts that turn out to be gone
    # stay in the scope that was ASKED, which is what the hash describes.
    skus = sorted(a.rstrip("/").rsplit("/", 1)[-1] for a in query.ads)
    return {"ads": len(skus),
            "ads_sha256": hashlib.sha256("\n".join(skus).encode()).hexdigest()}


def finish(args, query: Query, outcomes: List, stop_reason: str,
           blocked: bool) -> int:
    """Merge the pages in PAGE order, write the output, return the exit code.

    One implementation for the three engines, so the merge order, the
    dedupe and the sidecar cannot differ between them (§6).
    """
    rows, seen = [], set()
    for oc in sorted(outcomes, key=lambda o: o.page_num):
        fresh = dedupe_by_key(oc.products, seen, key="sku")
        if len(fresh) < len(oc.products):
            log.info("Page %d: dropped %d duplicate row(s) — the live listing "
                     "moved between page fetches.", oc.page_num,
                     len(oc.products) - len(fresh))
        rows.extend(fresh)

    first = next((o for o in outcomes if o.page_num == 1), None)
    total = getattr(first, "total_available", None)
    available = getattr(first, "pages_available", None)
    ok_pages = [o for o in outcomes if o.ok]
    failed_pages = sorted(o.page_num for o in outcomes if not o.ok)
    extra = {"query": query_summary(query), "currency": query.currency}
    if query.mode == "search":
        reachable = available * query.per_page if available is not None else None
        extra.update({
            "total_results": total,
            "pages_available": available,
            # The site serves at most ~10,000 results per search
            # (product_parser.SITE_RESULT_CAP), and its page count already
            # includes that. A run that fetched every page of a capped
            # search is complete as a REQUEST and still a sample.
            "reachable_max": reachable,
            "capped_by_site": (bool(total > reachable)
                               if total is not None and reachable is not None
                               else None),
            "sponsored_skipped": sum(o.sponsored for o in outcomes),
            # Records the site sent as listings that could not be read. 0 on
            # every page captured (3,386 listings); anything else is the
            # payload's shape moving, and the canary fails on it.
            "malformed_records": sum(o.malformed for o in outcomes),
        })
        if rows and total:
            log.info("The site reports %d match(es); this run holds %d (%.1f%%).",
                     total, len(rows), 100.0 * len(rows) / total)
    else:
        extra["ads_gone"] = sorted(o.url for o in outcomes if o.state == "gone")
        extra["ads_requested"] = sorted(
            a.rstrip("/").rsplit("/", 1)[-1] for a in query.ads)
    # Columns that fell below the measured floor on some page. A warning in
    # the log is read by nobody once the run exits 0, so it goes where a
    # pipeline (and the canary) reads it.
    extra["columns_below_floor"] = sorted(
        {c for o in outcomes for c in o.field_shortfall})
    last_ok = max([o.page_num for o in ok_pages] or [1])
    return finish_run(
        rows, args.out, args.format, args.allow_empty,
        blocked=blocked, stop_reason=stop_reason,
        pages_requested=args.pages, pages_completed=len(ok_pages),
        pages_failed=failed_pages, mode=query.mode, source=SOURCE_DEFAULT,
        start_url=args.url or currency_page(query),
        final_url=request_for(query, last_ok).url,
        extra=extra)


# ---------------------------------------------------------------------------
# The fetch loop, driven through named operations
# ---------------------------------------------------------------------------
#
# Everything about fetching one page lives here, once: landing on the
# origin, retrying a transport failure, waiting out a throttle, rotating on a
# refusal, and parsing what came back. The three engines differ only in HOW
# they ask their driver, so each passes in an object with these operations
# and no JavaScript crosses this boundary (§1):
#
#     ops.goto(url)        -> (status, waf_header). Raises TransportError.
#     ops.document_text()  -> the landing document, JSON text if it is JSON
#     ops.wait_ms(ms)
#     ops.fetch(req)       -> (status, text, waf_header, error_or_None)
#     ops.relaunch()       -> a fresh browser (on the pool's current exit)
#     ops.landed           -> bool attribute, owned by the loop
#     ops.proxy_failure(text) -> the driver's proxy-error name in text, or ""
#
# One copy of the loop is how the family rule that the three engines agree
# on exit codes and on whether a run spends money (§6) holds by
# construction rather than by discipline.


class TransportError(Exception):
    """A navigation that did not complete: a timeout, a dead proxy."""


@dataclass
class PageOutcome:
    """What one page produced.

    Collected per page and merged afterwards, in page order, rather than
    folded into shared state as the loop goes, so the output cannot depend
    on which page happened to finish first (§8).
    """
    page_num: int
    url: str
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    # The state the page came back as. Carried so the caller can tell an
    # EMPTY page (the end of a live listing) and a GONE advert from a failed
    # page: all three hold zero rows and they mean different things.
    state: Optional[str] = None
    # The gateway's own complaint when it refused the path.
    rejected: Optional[str] = None
    # The site's own count of what matched, from this page's response.
    total_available: Optional[int] = None
    pages_available: Optional[int] = None
    # Search only: what the site applied, when it is not what was asked.
    filter_problem: Optional[str] = None
    sponsored: int = 0
    # Records sent as listings that the parser could not read.
    malformed: int = 0
    # The page held listing records and NONE could be read: the payload's
    # shape has moved. Not an empty listing (see fetch_one_page).
    unread: bool = False
    field_shortfall: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (not self.load_failed and self.blocked_by is None
                and self.rejected is None and not self.unread)


# The columns the site filled on EVERY record of the 3,000 captured
# (2026-09-28), per mode. Below this share, the payload shape has moved
# rather than the data being unusual. Deliberately NOT here: `odometer_km`
# (87.7% of cars), `fipe_pct` (93.8%), `image_url` (98.7%) — genuinely
# absent on some records.
CORE_FIELD_FLOOR = 99
CORE_FIELDS = {
    "search": ("sku", "url", "title", "price", "make", "model", "year_model",
               "seller_type", "city", "state"),
    "ad": ("sku", "url", "title", "price", "make", "model", "year_model",
           "seller_type", "city", "state"),
}


def land(ops, args) -> tuple:
    """Put the page on a www.webmotors.com.br document that fetch() can use.

    Returns (state, error): state is a policy state for the landing, and
    error is a transport failure's text or None.
    """
    try:
        status, waf = ops.goto(ORIGIN_URL)
    except TransportError as e:
        return "load_failed", str(e)
    text = ops.document_text()
    state = classify(text, status, ORIGIN_URL, waf)
    if state == "unknown" and (text or "").lstrip().startswith("{"):
        # /api/location's JSON is none of the payloads the classifier names,
        # and the one thing that matters about the landing is that it is not
        # a refusal.
        state = "content"
    ops.landed = state == "content"
    return state, None


def _core_field_warnings(rows: List, mode: str, page_num: int) -> List[str]:
    """The core columns below the floor on this page, each also logged."""
    short = []
    for name in CORE_FIELDS.get(mode, ()):
        if not rows:
            return short
        filled = sum(1 for r in rows if getattr(r, name, None) not in (None, "", []))
        share = 100.0 * filled / len(rows)
        if share < CORE_FIELD_FLOOR:
            log.warning("Only %.0f%% of page %d carries `%s`, against a "
                        "measured floor of %d%%. Every record of every capture "
                        "had one, so the payload shape has moved — re-run with "
                        "--dump-html.", share, page_num, name, CORE_FIELD_FLOOR)
            short.append(name)
    return short


def _dump(args, page_num: int, text: str) -> None:
    """Write the exact response the parser was given, on success too (§9)."""
    if not args.dump_html:
        return
    path = args.dump_html if args.pages == 1 else f"{args.dump_html}.page{page_num}"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    log.info("Saved the response the parser sees to %s (%d bytes).",
             path, len(text))


def _save_debug(args, page_num: int, text: str) -> str:
    path = f"{args.out}_page{page_num}_debug.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text or "")
    return path


def fetch_one_page(ops, args, pool, query: Query, page_num: int,
                   mask: Callable[[str], str] = lambda s: s) -> PageOutcome:
    """Fetch and parse one page. Retries, rotations and debug dumps live here.

    Never raises for an EXPECTED failure. A timeout, a refusal and a dead
    exit are all recorded on the outcome, because what the run should do
    about them differs between the sequential and the concurrent paths.
    """
    req = request_for(query, page_num)
    outcome = PageOutcome(page_num=page_num, url=req.url if query.mode == "ad"
                          else req.label)
    if query.mode == "ad":
        outcome.url = query.ads[page_num - 1]

    has_pool = bool(pool and len(pool) > 1)
    block_retries = 0 if not RETRY_ON_BLOCKED else (
        args.proxy_block_retries if has_pool else BLOCK_RETRIES_WITHOUT_POOL)
    throttles = 0
    state, text, last_error, exit_failed = "unknown", "", None, None

    for block_attempt in range(block_retries + 1):
        log.info("Fetching %s %d/%d: %s", "advert" if query.mode == "ad"
                 else "page", page_num, args.pages, req.label)
        exit_failed = None
        attempt = 0
        while attempt < args.retries:
            attempt += 1
            text, last_error = "", None
            if not ops.landed:
                state, last_error = land(ops, args)
                if last_error:
                    exit_failed = ops.proxy_failure(last_error) or None
                    if exit_failed:
                        break  # a different exit is the only thing that helps
                elif not ops.landed:
                    text = ops.document_text()
            if ops.landed:
                status, text, waf, last_error = ops.fetch(req)
                state = ("load_failed" if last_error
                         else classify(text, status, req.url, waf))
                if counts_as_blocked(state):
                    # Refused after the landing: the session is spent, and
                    # the next attempt lands again from a fresh browser.
                    ops.landed = False
            if state == "throttled" and throttles < THROTTLE_RETRIES:
                throttles += 1
                attempt -= 1  # a throttle wait spends its own budget (§24)
                pause = THROTTLE_WAIT_S * throttles
                log.warning("Rate-limited on page %d (HTTP 429) — waiting "
                            "%.0fs at the same exit (%d/%d).", page_num, pause,
                            throttles, THROTTLE_RETRIES)
                ops.wait_ms(int(pause * 1000))
                continue
            if state in ("load_failed", "unknown") and attempt < args.retries:
                pause = args.retry_delay * (2 ** (attempt - 1))
                log.warning("Page %d came back %s (attempt %d/%d)%s — retrying "
                            "in %.1fs.", page_num, state, attempt, args.retries,
                            f": {mask(last_error)}" if last_error else "", pause)
                ops.wait_ms(int(pause * 1000))
                continue
            break

        if exit_failed and has_pool and block_attempt < block_retries:
            log.warning("Exit %s is unusable (%s) — rotating to another one "
                        "(%d/%d).", mask(pool.current), exit_failed,
                        block_attempt + 1, block_retries)
            pool.advance(f"unusable exit: {exit_failed}")
            ops.relaunch()
            continue
        if (counts_as_blocked(state) and should_retry(state)
                and block_attempt < block_retries):
            name = refusal_name(state, text)
            if has_pool:
                log.warning("Page %d refused (%s) at %s — rotating to another "
                            "exit (%d/%d).", page_num, name,
                            mask(pool.current), block_attempt + 1, block_retries)
                pool.advance(f"refused: {name}")
            else:
                log.warning("Page %d refused (%s) — re-fetching once from a "
                            "fresh browser.", page_num, name)
            ops.relaunch()
            continue
        break

    outcome.state = state
    if state == "load_failed" or exit_failed:
        outcome.load_failed = True
        log.error("Gave up on page %d: %s", page_num,
                  mask(last_error or "the request never completed"))
        return outcome
    if state == "rejected":
        outcome.rejected = api_error(text) or "HTTP 403"
        log.error("The API gateway refused this request (%s). That is a "
                  "statement about the PATH, and the same request sent again "
                  "gets the same answer, so it is not retried. If the address "
                  "looks right, the site's API has changed — open an issue "
                  "with --dump-html.", outcome.rejected)
        _dump(args, page_num, text)
        return outcome
    if state == "gone":
        log.warning("Advert %s is gone: the site answered 404. It was sold or "
                    "withdrawn, or the address is not the site's own for it "
                    "(a wrong slug gets the same answer).", outcome.url)
        return outcome
    if counts_as_blocked(state):
        outcome.blocked_by = refusal_name(state, text)
        debug = _save_debug(args, page_num, text)
        log.error("Blocked by %s on page %d — saved to %s. This is exit 3, "
                  "distinct from an empty listing (exit 4). %s",
                  outcome.blocked_by, page_num, debug,
                  refusal_advice(state, text))
        return outcome
    if not should_parse(state):
        # throttled past its budget, or never the endpoint's JSON at all
        outcome.load_failed = True
        debug = _save_debug(args, page_num, text)
        log.error("Page %d never came back as the endpoint's JSON (%s) — saved "
                  "to %s. %s", page_num, state, debug,
                  "Raise --delay, or spread the run over --proxy-file."
                  if state == "throttled" else "")
        return outcome

    _dump(args, page_num, text)
    rows = parse_page(text, query, page_num)
    outcome.products = rows
    if query.mode == "search":
        outcome.sponsored = sponsored_count(text)
        listed = count_rows(text)
        outcome.malformed = listed - len(rows)
        if outcome.malformed:
            log.error("Page %d: %d of %d listing record(s) could not be read "
                      "(no usable UniqueId). Every record of every capture had "
                      "one, so the payload's shape has moved — re-run with "
                      "--dump-html.", page_num, outcome.malformed, listed)
        if listed and not rows:
            # The site sent listings and the parser read none. Reported as
            # an empty page, this ended a 3-page run `complete` with exit 0
            # on a page holding 47 listings (audit, 2026-10-08).
            outcome.unread = True
            debug = _save_debug(args, page_num, text)
            log.error("Page %d was served with %d listing record(s) and none "
                      "were read — the payload's shape has moved. Saved to %s. "
                      "This is NOT an empty listing; open an issue with that "
                      "file.", page_num, listed, debug)
        outcome.total_available = total_results(text)
        outcome.pages_available = pages_available(text)
        if page_num == 1:
            outcome.filter_problem = filter_mismatch(text, query)
            if outcome.total_available is not None:
                log.info("The site reports %d match(es) — %s page(s) at %d per "
                         "page.", outcome.total_available,
                         outcome.pages_available, query.per_page)
        log.info("Parsed %d row(s) from page %d%s.", len(rows), page_num,
                 " (%d sponsored tile(s) skipped)" % outcome.sponsored
                 if outcome.sponsored else "")
    elif rows:
        _market_prices(ops, rows[0], page_num, mask)
    outcome.field_shortfall = _core_field_warnings(rows, query.mode, page_num)
    return outcome


def _market_prices(ops, row, page_num: int, mask) -> None:
    """The averageprice endpoint for one advert. A failure costs the four
    market columns, never the advert: the row is kept and the run says so."""
    address = parse_ad_url(row.url)
    kind = address.kind if address else ("bike" if row.vehicle == "motorcycle" else "car")
    req = average_price_request(kind, row.sku, page_num)
    status, text, _waf, err = ops.fetch(req)
    state = "load_failed" if err else classify(text, status, req.url)
    if counts_as_blocked(state):
        # PerimeterX refused this one request mid-session once in a live
        # run (2026-09-28) and served the same request moments later. One
        # more try from a fresh browser; the refusal leaves the session
        # spent either way, so the next advert lands again.
        ops.relaunch()
        if land(ops, None)[0] == "content":
            status, text, _waf, err = ops.fetch(req)
            state = "load_failed" if err else classify(text, status, req.url)
    if err or status != 200 or not apply_market_prices(row, text):
        log.warning("No market prices for advert %s (%s) — the row is kept "
                    "with them null.", row.sku,
                    mask(err) if err else "HTTP %s, %s" % (status, state))


def read_currency(ops, args, query: Query,
                  mask: Callable[[str], str] = lambda s: s) -> None:
    """Read the currency the site states, once per run, into the query.

    No endpoint states one, and a rendered page's JSON-LD does
    (product_parser.currency_from_html). The page is FETCHED, not
    navigated to: its server-rendered HTML carries the JSON-LD, and
    rendering it would run the site's own scripts, which redirect and fire
    a dozen requests of their own. A failure leaves the currency null and
    says so; it never stops the run.
    """
    if query.currency or not ops.landed:
        return
    page = currency_page(query)
    req = ApiRequest("GET", page[len(BASE):] if page.startswith(BASE) else page, 0)
    status, text, _waf, err = ops.fetch(req)
    query.currency = None if err or status != 200 else currency_from_html(text)
    if query.currency:
        log.info("The site states prices in %s (from the JSON-LD of %s).",
                 query.currency, page)
    else:
        log.warning("Could not read the currency from %s (%s) — the `currency` "
                    "column will be null rather than a guess.", page,
                    mask(err) if err else "HTTP %s" % status)


def run_pages(open_ops, close_ops, run_concurrently, args, pool,
              query: Query, concurrency: int,
              mask: Callable[[str], str] = lambda s: s) -> int:
    """The whole run after argument handling, shared by the three engines.

    `open_ops()` returns a ready ops object on `pool`, `close_ops(ops)`
    tears it down, and `run_concurrently(page_nums)` returns
    (outcomes, unattempted, exhausted) for pages 2..N fetched by workers. The
    engines supply those three because a browser's lifecycle (and on
    Playwright, its thread) is the one thing that cannot be shared.
    """
    outcomes: List[PageOutcome] = []
    blocked, stop_reason = False, "completed"
    ops = open_ops()
    try:
        # Page 1 is always fetched alone: its total decides how many pages
        # there are to address (§7), and its echo decides whether the site
        # searched for what was asked at all.
        first = fetch_one_page(ops, args, pool, query, 1, mask)
        if first.filter_problem:
            log.error("%s", first.filter_problem)
            return 2
        if first.ok:
            read_currency(ops, args, query, mask)
            for row in first.products:
                row.currency = query.currency
        outcomes.append(first)
        if not first.ok:
            stop_reason = stop_reason_for(first)
            blocked = first.blocked_by is not None
        else:
            plan = pages_to_plan(args.pages, first.pages_available)
            if plan < args.pages:
                log.info("Asked for %d page(s); the listing has %s. Fetching "
                         "all of them.", args.pages, first.pages_available)
            more_to_fetch = bool(first.products) or query.mode == "ad"
            rest = list(range(2, plan + 1)) if more_to_fetch else []
            if rest and concurrency > 1:
                close_ops(ops)
                ops = None
                log.info("Fetching pages 2-%d across %d workers%s.", plan,
                         concurrency, f" over {len(pool)} exit(s)" if pool else "")
                more, unattempted, exhausted = run_concurrently(rest)
                outcomes.extend(more)
                failed = [o for o in more if not o.ok]
                if failed:
                    stop_reason = stop_reason_for(min(failed, key=lambda o: o.page_num))
                    blocked = any(o.blocked_by for o in more)
                elif exhausted:
                    stop_reason = "end_of_listing"
                elif unattempted:
                    stop_reason = "pages_unattempted"
            else:
                seen = {r.sku for r in first.products}
                for page_num in rest:
                    ops.wait_ms(int(args.delay * 1000))
                    if pool and pool.rotates_per_page():
                        pool.advance(f"per-page rotation, page {page_num}")
                        ops.relaunch()
                    outcome = fetch_one_page(ops, args, pool, query, page_num, mask)
                    outcomes.append(outcome)
                    if not outcome.ok:
                        stop_reason = stop_reason_for(outcome)
                        blocked = outcome.blocked_by is not None
                        break
                    if query.mode == "ad":
                        continue
                    if listing_ended(outcome, seen):
                        stop_reason = "end_of_listing"
                        break
                    seen.update(r.sku for r in outcome.products)
    finally:
        if ops is not None:
            close_ops(ops)
    return finish(args, query, outcomes, stop_reason, blocked)


def listing_ended(outcome: PageOutcome, seen) -> bool:
    """Whether a search page says the live listing ended before the plan.

    Two shapes, both properties of the DATA rather than of markup:
    an empty page, and a page holding only rows already fetched — which is
    how this site answers a page past its end (it serves page 1 again).
    """
    if not outcome.products:
        log.info("Page %d came back empty — the listing ended before the plan "
                 "did.", outcome.page_num)
        return True
    if all(r.sku in seen for r in outcome.products):
        log.warning("Page %d holds only listings already fetched — the site "
                    "answers a page past its end with page 1 again, so the "
                    "listing ended before the plan did.", outcome.page_num)
        return True
    return False


def worker_loop(ops, args, query: Query, work, results, results_lock,
                exhausted, name: str, mask: Callable[[str], str] = lambda s: s):
    """One concurrent worker's page loop, after its engine opened `ops`.

    Takes pages until the queue is empty or a search page comes back empty
    (the live listing ended before the plan did), which sets `exhausted` so
    the other workers stop taking work too. A page that repeats earlier
    rows is not detectable here, since a worker sees only its own pages; the
    merge's dedupe drops those rows.
    """
    first = True
    while not exhausted.is_set():
        try:
            page_num = work.get_nowait()
        except Exception:  # queue.Empty
            break
        if not first:
            ops.wait_ms(int(args.delay * 1000))
        first = False
        outcome = fetch_one_page(ops, args, ops.pool, query, page_num, mask)
        for row in outcome.products:
            row.currency = query.currency
        with results_lock:
            results.append(outcome)
        if query.mode == "search" and outcome.ok and not outcome.products:
            log.info("[%s] page %d returned no rows — the listing ended; "
                     "stopping dispatch.", name, page_num)
            exhausted.set()


def concurrency_for(args, pool) -> int:
    """How many workers this run may use, with the warnings said once for
    all three engines."""
    concurrency = max(1, args.concurrency)
    if concurrency <= 1:
        return 1
    if concurrency_limit(args.cdp_endpoint) == 1:
        log.warning("--concurrency is ignored with --cdp-endpoint: the Scraping "
                    "Browser API allows one live connection per profile, and "
                    "several workers would collide on it (profile_locked). Use "
                    "several pids instead.")
        return 1
    if not pool:
        log.warning("--concurrency %d with no proxy pool: every worker leaves "
                    "from the SAME address, which is N times the request rate "
                    "from it. Pass --proxy-file to spread the load.",
                    concurrency)
    if concurrency > 8:
        log.warning("--concurrency %d means %d browsers at once (~150-300MB "
                    "each).", concurrency, concurrency)
    return concurrency


def worker_pool(pool, worker_index: int):
    """A private ProxyPool for one worker, starting at a different exit.

    Workers start on distinct exits and share no mutable state, so rotation
    needs no lock (§7).
    """
    if not pool:
        return None
    from proxy_pool import ProxyPool
    proxies = pool.proxies
    offset = worker_index % len(proxies)
    return ProxyPool(proxies[offset:] + proxies[:offset], rotate="per-run")


def ua_override(chromium_version: str, locale: str) -> dict:
    """The CDP `Network.setUserAgentOverride` parameters for a COMPLETE
    identity: the user agent string AND the client hints that go with it.

    pyppeteer's `page.setUserAgent` and a bare Selenium override send the UA
    string alone, and Chromium then drops the client hints entirely: no
    `Sec-CH-UA` header, and `navigator.userAgentData.brands` an empty list,
    which no real Chrome 153 reports. Measured 2026-09-28 against an echo
    service, then on the site: PerimeterX refused pyppeteer that way 2 of 2,
    while Playwright (whose override keeps the hints) was served on the same
    exit. Half an identity is refused; a whole one is not (§24).

    The version is the browser's OWN (§8), the platform agrees with the
    Windows UA string, and the brand list is the one Chromium itself sends,
    minus the `HeadlessChrome` token.
    """
    major = (chromium_version or "0").split(".")[0].split("/")[-1]
    full = (chromium_version or "0").split("/")[-1]
    brands = [{"brand": "Chromium", "version": major},
              {"brand": "Not_A Brand", "version": "8"}]
    return {
        "userAgent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      f"Chrome/{full} Safari/537.36"),
        "acceptLanguage": locale,
        "platform": "Win32",
        "userAgentMetadata": {
            "brands": brands,
            "fullVersionList": [{"brand": "Chromium", "version": full},
                                {"brand": "Not_A Brand", "version": "8.0.0.0"}],
            "fullVersion": full,
            "platform": "Windows",
            "platformVersion": "10.0.0",
            "architecture": "x86",
            "model": "",
            "mobile": False,
            "bitness": "64",
            "wow64": False,
        },
    }


def cdp_connect_hint(error_text: str) -> str:
    """What a failed --cdp-endpoint connection means, from its status.

    Two answers that want opposite fixes. A Scraping Browser profile's
    credentials last about a day, so a 401 is usually an endpoint copied from
    an older .env; a 500 is usually a pid another run still holds.
    """
    if "401" in (error_text or ""):
        return ("HTTP 401: the endpoint's credentials were refused. A Scraping "
                "Browser profile's credentials last about a day, so an "
                "endpoint copied from an older .env has usually expired. "
                "Get a fresh one from your 2Captcha dashboard.")
    return ("A Scraping Browser profile allows ONE live connection at a time, "
            "so an HTTP 500 here usually means another run still holds this "
            "`pid`. Wait for it to finish, or use a different pid.")


# Connecting to a Scraping Browser profile right after the previous run let
# go of it answers HTTP 500 `profile_locked`: the service released a profile
# 1.6-1.9 s after a clean disconnect in a sibling repo (3 of 3, 2026-09-24).
# Three attempts 3 s apart ride that out, and a profile genuinely held by
# another run still fails, after ~9 s, with the pid explanation.
CDP_CONNECT_ATTEMPTS = 3
CDP_LOCKED_WAIT_S = 3.0
# pyppeteer does not surface the 500 at all: its connect() waits on a future
# the rejected handshake never resolves, so only a timeout ends it. A
# successful connect measured under 2 s in the sibling repos.
CDP_CONNECT_TIMEOUT_S = 10


def cdp_should_retry(error_text: str) -> bool:
    """Whether a failed --cdp-endpoint connection is worth another attempt:
    a locked profile (500) or a connect that never answered. A 401 is not:
    expired credentials stay expired."""
    text = error_text or ""
    if "401" in text:
        return False
    return ("profile_locked" in text or " 500" in text or "HTTP 500" in text
            or "did not return within" in text)

