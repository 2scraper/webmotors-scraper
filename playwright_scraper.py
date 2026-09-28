#!/usr/bin/env python3
"""
webmotors-scraper — Playwright edition (primary engine)
=======================================================

Scrapes Brazil's largest vehicle marketplace, webmotors.com.br:

    --mode search   (default)  a search page of cars or motorcycles: price,
                               year, mileage, version, FIPE percentage,
                               seller kind, city and state
    --mode ad                  adverts, one row each: everything above plus
                               fuel, optionals, the FIPE code and value, and
                               the site's own market-price range

Three engines ship in this repo and they must agree on exit codes, run
status, and whether a run crashes or spends money. The shared decisions live
in output_writer.finish_run() and page_flow.py, including the fetch loop
itself, so they cannot drift apart.

What is different about Webmotors
---------------------------------
* **Two gates, and they refuse different things.** CloudFront refuses every
  datacentre ADDRESS (a 986-byte 403, headful Chromium included), and
  PerimeterX refuses the CLIENT: a headless browser whose user agent says
  `HeadlessChrome` got its Press & Hold page 0 of 3 times served, and the
  same browser with that one token removed was served 3 of 3 (2026-09-28).
  So this engine needs a residential exit (--proxy) or the Scraping Browser
  (--cdp-endpoint), and it overrides the user agent (`_chrome_ua`).
* **No page is rendered.** The browser lands on a 3 KB endpoint and asks the
  site's own search and detail endpoints for every page with a same-origin
  `fetch()`. See product_parser's docstring.
* **The API does not validate what it is given.** A misspelt model is
  answered with every model of the make, an unknown state with the whole
  country, a page past the end with page 1 again. The ordering is
  allowlisted, the site's echo of the filters is checked after page 1, and
  pages are planned from the site's own page count.
* **The site serves at most ~10,000 results per search.** The sidecar
  records the site's own total beside what a run holds (`capped_by_site`).

Usage
-----
    python playwright_scraper.py --make volkswagen --model gol --pages 3 \\
        --proxy http://USER:PASS@HOST:PORT

    python playwright_scraper.py --url "https://www.webmotors.com.br/carros/sp/toyota/corolla?anode=2020" \\
        --sort price-asc

    python playwright_scraper.py --category motos --make honda

    python playwright_scraper.py --mode ad --ads-file webmotors_rows.json

Requires: pip install -r requirements.txt -r requirements-playwright.txt
          then: playwright install chromium   (only if NOT using --cdp-endpoint)
"""

import argparse
import logging
import queue
import re
import sys
import threading
import time

from playwright.sync_api import (sync_playwright, Error as PWError,
                                 TimeoutError as PWTimeout)

from product_parser import (CONDITION_SUFFIX, DEFAULT_PER_PAGE, DEFAULT_SORT,
                            MAX_PER_PAGE, MODES, SORTS, UFS, VEHICLES, Query)
from output_writer import EXIT_API_ERROR
import page_flow
from proxy_pool import (from_args as proxy_pool_from_args, to_playwright, mask,
                        ROTATE_MODES, ProxyError)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("playwright_scraper")


# Chromium's own names for a proxy that could not be used. Distinguished
# from a timeout because the two want opposite responses (CLAUDE.md §8).
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED",
    "ERR_PROXY_AUTH_REQUESTED",
    "ERR_PROXY_CERTIFICATE_INVALID",
    "ERR_NO_SUPPORTED_PROXIES",
    "ERR_SOCKS_CONNECTION_FAILED",
    "ERR_MANDATORY_PROXY_CONFIGURATION_FAILED",
)

# The fetch() every page goes through. It is handed to `page.evaluate` as a
# FUNCTION, which Playwright sends through `Runtime.callFunctionOn` rather
# than evaluating a string, so it works under any Content-Security-Policy
# (§18). No headers are set: the site's own front end sends none that the
# endpoints need, and the one HTML page fetched per run (for its currency,
# page_flow.read_currency) must be asked for as a browser asks for a page.
#
# The AbortController is the timeout: a browser fetch() has none of its own,
# and §8 requires every remote call to be bounded.
FETCH_JS = """
async ([url, method, body, timeoutMs]) => {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const init = {method, credentials: "include", signal: ctl.signal};
    if (body !== null) {
      init.headers = {"content-type": "application/json"};
      init.body = body;
    }
    const r = await fetch(url, init);
    return {status: r.status, text: await r.text(), waf: null};
  } catch (e) {
    return {status: 0, text: "", waf: null, error: String(e)};
  } finally {
    clearTimeout(timer);
  }
}
"""

# What the landing document holds: the JSON when there is JSON, since
# Chromium wraps a JSON response in its own viewer markup and the payload is
# only reachable as body.innerText. A function, for the same CSP reason.
BODY_TEXT_JS = "() => document.body ? document.body.innerText : ''"


def _chrome_ua(chromium_version: str) -> str:
    """A desktop-Chrome UA naming the browser's OWN real version (§8).

    LOAD-BEARING on this site, not cosmetic. Headless Chromium's default UA
    carries the token `HeadlessChrome`, and PerimeterX refused that 0 of 3
    times served, while the same headless browser with this UA was served 3
    of 3, same exit, same session length (2026-09-28). In a headful browser
    it changed nothing (served 2 of 2 with and without it).
    """
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{chromium_version} Safari/537.36")


def _proxy_failure(exc) -> str:
    """The Chromium proxy-error name in `exc`, or "" if it is not one."""
    text = str(exc)
    for marker in _PROXY_ERROR_MARKERS:
        if marker in text:
            return marker
    return ""


def _launch_local(pw, args, pool):
    """Launch our own Chromium on `pool`'s current exit; return (browser, context, page).

    A proxy rotation tears the whole browser down and calls this again.
    Cookies a bot manager issued against one exit, replayed from another,
    are a stronger signal than either address alone (§8).
    """
    launch_kwargs = {"headless": args.headless}
    proxy = to_playwright(pool.current) if pool else None
    if proxy:
        launch_kwargs["proxy"] = proxy
        logger.info("Using proxy exit %s", mask(pool.current))

    browser = pw.chromium.launch(**launch_kwargs)
    ctx_kwargs = {"user_agent": _chrome_ua(browser.version), "locale": args.locale}
    init_script = None
    if args.fingerprint:
        from fingerprint_client import (get_fingerprint,
                                        playwright_context_kwargs,
                                        playwright_init_script)
        fp = get_fingerprint(args.twocaptcha_key,
                             tags=args.fp_tags, country=args.fp_country)
        ctx_kwargs.update(playwright_context_kwargs(fp))
        init_script = playwright_init_script(fp)
        logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"), fp.get("country"))

    context = browser.new_context(**ctx_kwargs)
    if init_script:
        context.add_init_script(init_script)
    return browser, context, context.new_page()


class _Ops:
    """One browser + context + page, exposed as page_flow's named operations.

    page_flow owns the fetch loop for all three engines. This class answers
    only HOW Playwright does each step. `landed` records whether the page
    sits on a www.webmotors.com.br document that fetch() can be issued
    from; the loop owns it, and a relaunch clears it.
    """

    def __init__(self, pw, args, pool, remote: bool = False):
        self.pw, self.args, self.pool, self.remote = pw, args, pool, remote
        self.browser = self.context = self.page = None
        self.landed = False

    def open(self):
        if self.remote:
            self.browser, self.context, self.page = _connect_remote(self.pw, self.args)
        else:
            self.browser, self.context, self.page = _launch_local(
                self.pw, self.args, self.pool)
        self.landed = False
        return self

    # ---- page_flow's operations -----------------------------------------

    def goto(self, url: str):
        try:
            resp = self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except (PWTimeout, PWError) as e:
            raise page_flow.TransportError(_mask_credentials(str(e))) from None
        if resp is None:
            return None, None
        return resp.status, None

    def document_text(self) -> str:
        """The landing document: JSON text when it is the endpoint's JSON.

        Chromium wraps a JSON response in its own viewer markup, so the
        payload is only reachable as body.innerText. A refusal page is read
        as the whole markup, because its markers live in the markup.
        """
        try:
            text = self.page.evaluate(BODY_TEXT_JS) or ""
        except (PWError, PWTimeout):
            text = ""
        if text.lstrip().startswith("{"):
            return text
        try:
            return self.page.content()
        except PWError:
            return text

    def wait_ms(self, ms: int) -> None:
        time.sleep(ms / 1000.0)

    def fetch(self, req, timeout_ms: int = page_flow.FETCH_TIMEOUT_MS):
        try:
            got = self.page.evaluate(
                FETCH_JS, [req.url, req.method, req.body_json, timeout_ms])
        except (PWError, PWTimeout) as e:
            return None, "", None, _mask_credentials(str(e))
        if not isinstance(got, dict):
            return None, "", None, "fetch() returned nothing"
        if got.get("error"):
            return None, "", None, str(got["error"])
        return got.get("status"), got.get("text") or "", None, None

    def proxy_failure(self, text: str) -> str:
        return _proxy_failure(text)

    def relaunch(self):
        """A fresh browser on the pool's current exit. On a remote browser
        only the landing is reset, since its exit is not ours to change."""
        if self.remote:
            self.landed = False
            return
        try:
            self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error while closing browser for rotation: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.page.close()  # leave the remote browser app running
            else:
                self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


def _connect_remote(pw, args):
    """Attach to an already-running browser over CDP; return (browser, context, page)."""
    logger.info("Connecting to existing browser over CDP: %s",
                _mask_credentials(args.cdp_endpoint))
    browser, e = None, None
    for attempt in range(1, page_flow.CDP_CONNECT_ATTEMPTS + 1):
        try:
            browser = pw.chromium.connect_over_cdp(args.cdp_endpoint, timeout=30000)
            break
        except (PWError, PWTimeout) as err:
            e = err
            if attempt < page_flow.CDP_CONNECT_ATTEMPTS and page_flow.cdp_should_retry(str(err)):
                logger.warning("The Scraping Browser profile is still locked "
                               "(attempt %d/%d) — a previous run may be "
                               "releasing it; retrying in %.0fs.", attempt,
                               page_flow.CDP_CONNECT_ATTEMPTS,
                               page_flow.CDP_LOCKED_WAIT_S)
                time.sleep(page_flow.CDP_LOCKED_WAIT_S)
                continue
            break
    if browser is None:
        # The endpoint carries a password, and Playwright repeats it five
        # times in its error text (§8). Rewritten with it masked, keeping
        # host and port, which are the useful half.
        raise PWError(
            f"could not connect to --cdp-endpoint "
            f"{_mask_credentials(args.cdp_endpoint)}: "
            f"{_mask_credentials(str(e))}\n"
            f"{page_flow.cdp_connect_hint(str(e))}"
        ) from None
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()
    # The Scraping Browser API's own CAPTCHA domain
    # (https://2captcha.com/scraper/browser-api/api). Enabled for parity with
    # the family: if a challenge the extension handles ever appears in front
    # of the landing, it can clear it. PerimeterX's refusal did not appear
    # over this path in any run measured.
    try:
        cdp_session = context.new_cdp_session(page)
        cdp_session.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
        cdp_session.on("Captcha.detected", lambda *_: logger.info("[Scraping Browser] CAPTCHA detected on page."))
        cdp_session.on("Captcha.solveFinished", lambda *_: logger.info("[Scraping Browser] CAPTCHA solved automatically."))
        cdp_session.on("Captcha.solveFailed", lambda *_: logger.warning("[Scraping Browser] CAPTCHA auto-solve failed."))
        logger.info("Scraping Browser API Captcha.setAutoSolve enabled.")
    except Exception as e:  # noqa: BLE001
        logger.info("Captcha.setAutoSolve not available on this --cdp-endpoint (%s).", e)
    return browser, context, page


# Every `scheme://user:pass@` in a string, however many times it occurs.
# Matching GLOBALLY is the point: a Playwright connection error repeats the
# endpoint five times (§8).
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced."""
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _fetch_pages_concurrently(args, pool, query: Query, page_nums, concurrency: int):
    """Fetch `page_nums` across `concurrency` workers.

    Each worker owns its own Playwright instance, browser and exit: with the
    sync API a browser belongs to the thread that made it, so sharing one is
    not an option even in principle (§7). The page loop itself is
    page_flow.worker_loop, shared by all three engines.
    """
    work = queue.Queue()
    for n in page_nums:
        work.put(n)
    results, results_lock = [], threading.Lock()
    exhausted = threading.Event()

    def worker(index: int):
        name = f"worker-{index + 1}"
        try:
            with sync_playwright() as pw:
                ops = _Ops(pw, args, page_flow.worker_pool(pool, index)).open()
                try:
                    page_flow.worker_loop(ops, args, query, work, results,
                                          results_lock, exhausted, name,
                                          _mask_credentials)
                finally:
                    ops.close()
        except Exception:  # noqa: BLE001 — a dead worker must not hang the run
            logger.exception("[%s] died; its pages will be reported as failed.", name)

    threads = [threading.Thread(target=worker, args=(i,), name=f"page-worker-{i + 1}")
               for i in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    unattempted = []
    while True:
        try:
            unattempted.append(work.get_nowait())
        except queue.Empty:
            break
    return results, sorted(unattempted), exhausted.is_set()


def scrape(args) -> int:
    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None
    if not pool and not args.cdp_endpoint:
        logger.warning("No --proxy and no --cdp-endpoint. CloudFront refused "
                       "every datacentre address measured; from a residential "
                       "connection of your own this can still work.")
    concurrency = page_flow.concurrency_for(args, pool)
    with sync_playwright() as pw:
        return page_flow.run_pages(
            lambda: _Ops(pw, args, pool, remote=bool(args.cdp_endpoint)).open(),
            lambda ops: ops.close(),
            lambda pages: _fetch_pages_concurrently(args, pool, args.query,
                                                    pages, concurrency),
            args, pool, args.query, concurrency, _mask_credentials)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="webmotors.com.br scraper — car and motorcycle listings "
                    "and adverts (Playwright edition)")
    p.add_argument("--mode", choices=list(MODES), default=None,
                   help="search (default): a search page of listings. ad: "
                        "adverts, one row each, with fuel, optionals, the FIPE "
                        "value and the site's market-price range. Inferred "
                        "from --url when that is given.")
    p.add_argument("--url", default=None,
                   help="A webmotors.com.br search page "
                        "(/carros/estoque/volkswagen/gol, "
                        "/carros/sp/toyota?anode=2020, /motos/estoque/honda) "
                        "or an advert (/comprar/...). The site's own API "
                        "reads the filters out of the address, so any search "
                        "the site builds works. Also read from WEBMOTORS_URL.")
    g = p.add_argument_group("search, without --url")
    g.add_argument("--category", choices=list(VEHICLES), default=None,
                   help="carros (default) or motos.")
    g.add_argument("--condition", choices=list(CONDITION_SUFFIX), default=None,
                   help="all (default), used or new. Cars only.")
    g.add_argument("--state", default=None, metavar="UF",
                   help="A Brazilian state by its two letters (%s). Default: "
                        "all of Brazil." % ", ".join(u.upper() for u in UFS))
    g.add_argument("--make", default=None,
                   help="The make as the site's addresses spell it "
                        "(volkswagen, mercedes-benz, land-rover).")
    g.add_argument("--model", default=None,
                   help="The model as the site's addresses spell it (gol, "
                        "onix-plus). Needs --make. A misspelt model is caught: "
                        "the site answers it with every model of the make, and "
                        "the run is refused with what the site applied.")
    g = p.add_argument_group("search")
    g.add_argument("--sort", choices=list(SORTS), default=None,
                   help="The site's own orderings (default %s). Not cosmetic: "
                        "the site serves at most ~10,000 results per search, "
                        "so the ordering decides WHICH listings a large search "
                        "yields — 'relevance' put no private seller in the "
                        "first 47 where 'price-asc' put 44." % DEFAULT_SORT)
    g.add_argument("--per-page", type=int, default=None, metavar="N",
                   help="Listings per page, 1-%d (default %d, the site's own)."
                        % (MAX_PER_PAGE, DEFAULT_PER_PAGE))
    g = p.add_argument_group("ad")
    g.add_argument("--ads-file", default=None, metavar="PATH",
                   help="Adverts to fetch: a search run's JSON output (its "
                        "`url` column) or one advert address per line.")
    p.add_argument("--pages", type=int, default=1,
                   help="Search pages to fetch. Planned against the page count "
                        "the site states on page 1, so asking for more than "
                        "exist fetches all of them. Ignored in ad mode.")
    p.add_argument("--delay", type=float, default=1.0,
                   help="Delay between pages, seconds (default %(default)s)")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Fetch pages through N parallel workers (default 1). "
                        "Each worker runs its own browser and holds its own "
                        "proxy exit. Ignored with --cdp-endpoint.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page on a transport failure (default 3). "
                        "The pause doubles each time.")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first retry, doubling thereafter.")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="webmotors_rows", help="Output file prefix")
    p.add_argument("--locale", default="pt-BR",
                   help="Browser locale (default pt-BR). The endpoints answer "
                        "in Portuguese whatever the browser claims.")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL, e.g. http://ACCOUNT:PASSWORD@HOST:9999 "
                        "(2captcha.com/proxy). A RESIDENTIAL exit: CloudFront "
                        "refuses datacentre addresses.")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line to rotate across. "
                        "Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run",
                   help="per-run (default): one exit for the whole run. "
                        "per-page: a new exit, and a fresh browser, per page.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool at startup.")
    p.add_argument("--proxy-block-retries", type=int, default=2,
                   help="When a page is refused (CloudFront, PerimeterX), "
                        "retry it from this many OTHER exits (default 2). "
                        "Needs a pool of more than one.")
    p.add_argument("--twocaptcha-key", default=None,
                   help="2captcha.com API key. Used by --fingerprint here: "
                        "no captcha on this site is solved (see --solve-captcha).")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Apply a browser fingerprint from 2captcha's "
                        "Fingerprint API. Needs --twocaptcha-key. Ignored with "
                        "--cdp-endpoint.")
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag for the fingerprint filter: "
                        "Windows, Microsoft Windows or Android. NOT a list — "
                        "Chrome, Desktop and Mobile are each rejected by the "
                        "API with 400. (default: Windows)")
    p.add_argument("--fp-country", default=None,
                   help="Fingerprint country, ISO 3166-1 alpha-2. Match it to "
                        "your proxy's exit country.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2",
                   help="Kept for parity with the family. Inert here: see "
                        "--solve-captcha.")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="Kept for parity with the family. Inert here: the "
                        "only challenge this site showed is PerimeterX's Press "
                        "& Hold, which this repo does not implement, and no "
                        "client this repo drives was shown it once served.")
    p.add_argument("--min-score", type=float, default=0.7,
                   help="Kept for parity with the family. Inert here.")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to an already-running browser over CDP "
                        "instead of launching Chromium, e.g. the Scraping "
                        "Browser API endpoint ws://user:pass@host:port. "
                        "--proxy and --headless/--headful are ignored.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact response the parser is given, on "
                        "success as well as failure. It is JSON; the flag "
                        "keeps the family's name.")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    args = p.parse_args(argv)
    env_config.apply(args)
    args.query = page_flow.build_query(args, p.error)
    args.mode = args.query.mode
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key (the Fingerprint API "
                     "uses the same key, though it's a separate subscription "
                     "from solving).")
        sys.exit(2)
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the "
                       "Scraping Browser supplies its own fingerprint.")
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        logger.error("%s", e)
        sys.exit(2)
    except PWError as e:
        # A remote browser refusing the connection is a REMOTE API failure
        # (exit 5), not a crash in this code (exit 1).
        text = _mask_credentials(str(e))
        if "profile_locked" in text or "connect to --cdp-endpoint" in text:
            logger.error("%s", text)
            sys.exit(EXIT_API_ERROR)
        raise
