#!/usr/bin/env python3
"""
webmotors-scraper — 2captcha Scraper API edition (fourth engine)
================================================================

A fourth way to run this scraper. Unlike the three browser engines, this one
manages **no browser and no CDP session of its own**: it POSTs a URL to
2captcha's separate **Scraper API** (https://scraper.2captcha.com, a
different product from the Scraping Browser API the other three reach
through --cdp-endpoint), gets the response back over plain HTTPS, and feeds
it to this project's product_parser.

Why you would want it: no Chromium to install, runs from a tiny container or
a lambda.

WHAT THIS SITE NEEDS — READ THIS FIRST
--------------------------------------
Every endpoint this repo reads is a GET with an address, so this client
reads both modes: the search and each advert are fetched as the URLs the
browser engines fetch() (product_parser.request_for). What it does NOT
fetch, to keep the bill to one task per page: an advert's market-price
figures (a second endpoint per advert; those four columns stay null here).
The currency is read once per run, which is one task more.

Whether the Scraper API's own exits are served is the whole question on
this site, since CloudFront refuses datacentre addresses. See the README for
what was measured; --cdp-url routes the fetch through a Scraping Browser
session instead.

Usage
-----
    python3 scraper_api_client.py --url https://www.webmotors.com.br/carros/estoque/volkswagen/gol --pages 2

    # the key comes from $TWOCAPTCHA_KEY and a CDP endpoint from
    # $WEBMOTORS_CDP_ENDPOINT, so neither needs to be typed — a secret in
    # argv is readable by anything that can run `ps`

Requires: pip install -r requirements.txt
          (no playwright/selenium/pyppeteer needed for this engine)
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from typing import Optional

import requests

import html as html_lib

from product_parser import (BASE, DEFAULT_PER_PAGE, DEFAULT_SORT, MAX_PAGES,
                            SORTS, ads_from_text, api_error, currency_from_html,
                            currency_page, detect_bot_challenge,
                            detect_page_state, filter_mismatch,
                            pages_available, parse_page, query_from_url,
                            request_for, total_results)
from output_writer import dedupe_by_key, finish_run, SOURCE_DEFAULT
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("scraper_api_client")

API_BASE = "https://scraper.2captcha.com"
SYNC_ENDPOINT = f"{API_BASE}/tasks/sync"

# The API caps `timeout` at 120s and rejects bodies over 10,000 bytes.
MAX_API_TIMEOUT = 120

# Exit codes. Kept distinct from 2 (bad usage) on purpose: a remote API
# failing is not the operator passing wrong arguments, and a harness that
# lumps them together sends you looking in the wrong place. An early run
# reported `exit=2` for an HTTP 422 from the API — which reads as "you called
# it wrong".
#
# Imported rather than redefined: the browser engines return the same code for
# a Scraping Browser that will not accept a connection, and two definitions
# of one exit code is how a family's contract drifts.
from output_writer import EXIT_API_ERROR  # noqa: E402

def _mask_credentials(url: str) -> str:
    """Never print a username:password embedded in a ws://... or http://... URL."""
    if "@" not in url:
        return url
    scheme_sep = url.find("://")
    if scheme_sep == -1:
        return url
    scheme, rest = url[:scheme_sep + 3], url[scheme_sep + 3:]
    _, _, host_part = rest.partition("@")
    return f"{scheme}***:***@{host_part}"


# Credentials embedded ANYWHERE in a blob of text, not just in a string that
# is entirely a URL — and every occurrence, not the first. A masker that
# handles one occurrence prints the password the other four times and looks
# like it is working.
_CREDS_IN_TEXT_RE = re.compile(r"([a-z][a-z0-9+.-]*://)[^/\s'\"@]+@", re.IGNORECASE)
# Same shape as fingerprint_client's. A second copy is
# one too many and they should be unified in a family pass; reaching into
# another module's private name to avoid it would be worse.
_KEY_IN_TEXT_RE = re.compile(
    r"((?:client)?key|token|api[_-]?key)=([^&\s'\"]{6,})", re.IGNORECASE)


def _redact_debug_header(value: str) -> str:
    """The x-debug header, safe to log.

    SECURITY.md names this header as one of three places credentials reach a
    log unmasked, and it was logged verbatim: the API echoes back the task it
    ran, so a run driven through a credentialed CDP endpoint put that
    endpoint's username and password into the log, and a key passed as a
    query parameter would go the same way.

    Redaction rather than an allowlist of fields, deliberately: the header is
    the API's own metadata and its shape is not ours to pin, so an allowlist
    would silently drop the cost and timing figures this is logged FOR the
    first time the API adds a field.
    """
    return _KEY_IN_TEXT_RE.sub(r"\1=***",
                               _CREDS_IN_TEXT_RE.sub(r"\1***:***@", value))


def _build_wait_for(args) -> Optional[dict]:
    """`waitFor` is an OBJECT. Measured 2026-09-23 against the live
    /tasks/sync endpoint: the JSON-encoded string form this client used to
    send was answered with HTTP 422 ("params.waitFor must be an object")
    and was still billed ($0.0005); the same request with an object
    answered HTTP 200. The earlier note here, that the API wanted a
    double-encoded string, no longer describes the API.

    Default (no flag): wait for the DOM. On a challenge-protected page
    that resolves instantly against the challenge page itself — which is
    exactly the trap documented in this module's docstring, so
    --wait-text/--wait-element exist to wait on something only the real
    page can contain."""
    if args.wait_text:
        return {"text": args.wait_text}
    if args.wait_element:
        return {"element": args.wait_element, "checkVisible": True}
    if args.wait_state:
        return {"state": args.wait_state}
    return None


def fetch_html(args) -> str:
    payload = {
        "task_type": "scrape",
        "url": args.fetch_url,
        "data_format": "raw",   # we want HTML; product_parser does the rest
        "format": "json",       # {"status": verdict, "http_code": target status, "headers", "body"}
        "timeout": min(args.timeout, MAX_API_TIMEOUT),
    }

    wait_for = _build_wait_for(args)
    if wait_for:
        payload["waitFor"] = wait_for
        logger.info("waitFor: %s", json.dumps(wait_for))

    if args.cdp_url:
        payload["cdpurl"] = args.cdp_url
        logger.info("Routing through an existing browser session: %s",
                    _mask_credentials(args.cdp_url))

    logger.info("POST %s (url=%s)", SYNC_ENDPOINT, args.fetch_url)
    resp = requests.post(
        SYNC_ENDPOINT,
        headers={"Authorization": f"Bearer {args.key}", "Content-Type": "application/json"},
        json=payload,
        # Give the HTTP call more headroom than the API-side task timeout,
        # otherwise a task that legitimately runs the full 120s looks like
        # a client-side network failure.
        timeout=min(args.timeout, MAX_API_TIMEOUT) + 30,
    )

    # The API returns its own per-task metadata (price, timings, status)
    # in an x-debug header — worth logging, it's the only place the real
    # cost of the call shows up.
    debug = resp.headers.get("x-debug")
    if debug:
        logger.info("x-debug: %s", _redact_debug_header(debug))

    if resp.status_code != 200:
        # 422 = task ran but errored (this is what a bad/unreachable
        # cdpurl produces: "CDP connect failed (user cdpurl) after N
        # attempts"); 402 = out of balance; 408 = sync wait exceeded.
        raise RuntimeError(
            f"Scraper API returned HTTP {resp.status_code}: {resp.text[:500]}"
        )

    body = resp.json()
    html = body.get("body") or ""
    # The TARGET's HTTP status is `http_code`. `status` is the API's own
    # verdict string ("success"), measured 2026-09-23 -- passing it on
    # handed the page classifier a string, so a target 403/503 was never
    # seen. Fall back to `status` only if it is itself an integer.
    upstream_status = body.get("http_code")
    if not isinstance(upstream_status, int):
        legacy = body.get("status")
        upstream_status = legacy if isinstance(legacy, int) and not isinstance(legacy, bool) else None
    logger.info("Upstream page HTTP status %s, %d bytes of HTML.", upstream_status, len(html))
    # The STATUS is returned alongside the HTML, not thrown away. It used to
    # be, and that cost this engine the family's central distinction. On this
    # site a refusal carries no markup at all — nothing a challenge check
    # on it, so the challenge check below finds nothing and the run fell
    # through to "0 products" and exit 4. A pipeline branching on the exit
    # code then reads a block as an empty category. See detect_page_state,
    # which the three browser engines already reach through page_flow.
    return html, upstream_status


_PRE_RE = re.compile(r"<pre[^>]*>(.*?)</pre>", re.S | re.I)


def json_text(body: str) -> str:
    """The endpoint's JSON out of what the Scraper API returned.

    The API loads the URL in a browser, and a browser shows a JSON response
    inside its own viewer markup, so the body may be HTML with the payload
    in a <pre>. Returned unchanged when it already is JSON.
    """
    s = (body or "").strip()
    if s.startswith("{"):
        return s
    m = _PRE_RE.search(s)
    inner = html_lib.unescape(m.group(1)).strip() if m else ""
    # Only a <pre> holding JSON is the viewer's: CloudFront's refusal page
    # has a <pre> of its own ("Generated by cloudfront"), and unwrapping
    # that would throw away the markup its markers live in.
    return inner if inner.startswith("{") else s


def _query(args):
    if args.ads_file:
        with open(args.ads_file, encoding="utf-8") as f:
            ads = ads_from_text(f.read())
        from product_parser import Query
        return Query(mode="ad", ads=tuple(ads)), None
    return query_from_url(args.url or "", sort=args.sort or DEFAULT_SORT,
                          per_page=args.per_page or DEFAULT_PER_PAGE)


def main() -> int:
    args = parse_args()
    if not args.key:
        logger.error("No 2captcha API key. Pass --key, or better, export TWOCAPTCHA_KEY.")
        return 2
    query, why = _query(args)
    why = why or (query.validate() if query else "no query")
    if why:
        logger.error("%s", why)
        return 2
    wanted = len(query.ads) if query.mode == "ad" else args.pages

    rows, seen, pages_done, failed, gone = [], set(), 0, [], []
    stop_reason, blocked, total, available = "completed", False, None, None
    plan = wanted
    page = 1
    while page <= plan:
        req = request_for(query, page)
        args.fetch_url = req.url
        rc, text, status = _fetch_once(args)
        if rc:
            failed.append(page)
            stop_reason = "page_load_timeout" if rc == EXIT_API_ERROR else "blocked"
            blocked = rc == 3
            if blocked:
                stop_reason = "blocked_%s" % (args.last_vendor or "http-403")
            break
        state = detect_page_state(text, status, req.url)
        if state == "gone":
            logger.warning("Advert %s is gone: the site answered 404.", query.ads[page - 1])
            gone.append(query.ads[page - 1])
            pages_done += 1
            page += 1
            continue
        if state == "rejected":
            logger.error("The API gateway refused this request (%s).",
                         api_error(text) or "HTTP 403")
            failed.append(page)
            stop_reason = "api_rejected"
            break
        if state not in ("content", "empty"):
            logger.error("Page %d came back as %s (upstream HTTP %s).", page,
                         state, status)
            failed.append(page)
            blocked = state in ("challenge", "blocked")
            stop_reason = ("blocked_%s" % (detect_bot_challenge(text) or "http-403")
                           if blocked else "page_load_timeout")
            break
        if page == 1 and query.mode == "search":
            problem = filter_mismatch(text, query)
            if problem:
                logger.error("%s", problem)
                return 2
            total = total_results(text)
            available = pages_available(text)
            plan = max(1, min(wanted, available if available is not None else MAX_PAGES, MAX_PAGES))
        if page == 1:
            _read_currency(args, query)
        got = parse_page(text, query, page)
        for r in got:
            r.currency = query.currency
        pages_done += 1
        logger.info("Parsed %d row(s) from page %d.", len(got), page)
        if query.mode == "search":
            fresh = dedupe_by_key(got, seen)
            if not got or (page > 1 and not fresh):
                # An empty page, or one holding only rows already fetched:
                # how this site answers a page past its end.
                stop_reason = "end_of_listing"
                break
            rows.extend(fresh)
        else:
            rows.extend(got)
        page += 1
        if page <= plan:
            time.sleep(args.delay)

    extra = {"query": {"listing_url": query.listing_url, "sort": query.sort,
                       "per_page": query.per_page} if query.mode == "search"
             else {"ads": len(query.ads)},
             "currency": query.currency, "engine": "scraper_api"}
    if query.mode == "search":
        extra.update({"total_results": total, "pages_available": available})
    else:
        extra["ads_gone"] = gone
    return finish_run(rows, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      pages_requested=wanted, pages_completed=pages_done,
                      pages_failed=failed, mode=query.mode,
                      source=SOURCE_DEFAULT,
                      start_url=args.url or currency_page(query),
                      final_url=request_for(query, max(1, min(page, plan))).url,
                      extra=extra)


def _read_currency(args, query) -> None:
    """One more task per run: the page whose JSON-LD states the currency
    (page_flow.read_currency, for the same reason)."""
    saved = args.fetch_url
    args.fetch_url = currency_page(query)
    try:
        body, status = fetch_html(args)
        query.currency = currency_from_html(body) if status == 200 else None
    except (requests.RequestException, RuntimeError) as e:
        logger.warning("Could not read the currency (%s).", _mask_credentials(str(e)))
    finally:
        args.fetch_url = saved
    if not query.currency:
        logger.warning("No currency read — the column will be null rather than a guess.")


def _fetch_once(args):
    """(rc, json_text, upstream_status). rc is 0 on a fetch that returned,
    EXIT_API_ERROR on the API failing, 3 on a refusal after the retries."""
    attempts = max(1, args.retries + 1)
    args.last_vendor = None
    for attempt in range(1, attempts + 1):
        try:
            body, upstream_status = fetch_html(args)
        except requests.RequestException as e:
            logger.error("Network error talking to the Scraper API: %s",
                         _mask_credentials(str(e)))
            return EXIT_API_ERROR, "", None
        except RuntimeError as e:
            logger.error("%s", _mask_credentials(str(e)))
            return EXIT_API_ERROR, "", None
        text = json_text(body)
        if args.dump_html:
            with open(args.dump_html, "w", encoding="utf-8") as f:
                f.write(text)
            logger.info("Response written to %s", args.dump_html)
        state = detect_page_state(text, upstream_status, args.fetch_url)
        if state not in ("challenge", "blocked"):
            return 0, text, upstream_status
        args.last_vendor = detect_bot_challenge(body) or "http-403"
        if attempt < attempts:
            logger.info("Refused (%s) on attempt %d/%d — retrying in %ds.",
                        args.last_vendor, attempt, attempts, args.retry_delay)
            time.sleep(args.retry_delay)
    logger.error("The Scraper API's fetch was refused (%s) on every attempt. "
                 "Route it through a Scraping Browser session (--cdp-url), or "
                 "use a browser engine with a residential --proxy.",
                 args.last_vendor)
    return 3, "", None


def parse_args():
    p = argparse.ArgumentParser(
        description="webmotors.com.br scraper — 2captcha Scraper API edition "
                    "(no local browser). Search pages and adverts.")
    # NOT required: prefer the TWOCAPTCHA_KEY env var (a key in argv is
    # visible to anyone who can run `ps`).
    p.add_argument("--key", default=os.environ.get("TWOCAPTCHA_KEY"),
                   help="2captcha.com API key (sent as a Bearer token). "
                        "Defaults to $TWOCAPTCHA_KEY, which is the safer way to pass it.")
    p.add_argument("--url", default=None,
                   help="A search page or an advert on webmotors.com.br. Also "
                        "read from WEBMOTORS_URL.")
    p.add_argument("--ads-file", default=None, metavar="PATH",
                   help="Adverts to fetch: a search run's JSON output or one "
                        "address per line.")
    p.add_argument("--sort", choices=list(SORTS), default=None,
                   help="The site's own orderings (default %s)." % DEFAULT_SORT)
    p.add_argument("--per-page", type=int, default=None, metavar="N",
                   help="Listings per page (default %d)." % DEFAULT_PER_PAGE)
    p.add_argument("--pages", type=int, default=1,
                   help="Search pages to fetch (default 1). Each is a billable task.")
    p.add_argument("--delay", type=float, default=1.0,
                   help="Seconds between pages (default 1.0)")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="webmotors_rows_scraperapi", help="Output file prefix")
    p.add_argument("--timeout", type=int, default=60,
                   help=f"API-side task timeout in seconds (1-{MAX_API_TIMEOUT}, default 60)")
    p.add_argument("--cdp-url", default=None,
                   help="Route the fetch through an existing browser session over CDP "
                        "(sent as the API's `cdpurl` param), e.g. ws://user:pass@host:port")
    wait = p.add_mutually_exclusive_group()
    wait.add_argument("--wait-text", default=None,
                      help="Wait until this string appears on the page.")
    wait.add_argument("--wait-element", default=None,
                      help="Wait until this CSS selector is visible.")
    wait.add_argument("--wait-state", choices=["load", "domcontentloaded"], default=None,
                      help="Wait for a page load state instead of specific content")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were parsed.")
    p.add_argument("--retries", type=int, default=1,
                   help="Extra attempts if a refusal comes back. Each attempt is "
                        "a separate billable task, so this defaults to 1.")
    p.add_argument("--retry-delay", type=int, default=10,
                   help="Seconds between retries (default 10)")
    p.add_argument("--dump-html", default=None,
                   help="Also write the response (JSON) to this path, even on success")
    args = p.parse_args()
    # This client uses --key and --cdp-url rather than --twocaptcha-key and
    # --cdp-endpoint, so the env mapping is spelled out instead of defaulted.
    env_config.apply(args, keys={
        "TWOCAPTCHA_KEY": "key",
        "WEBMOTORS_CDP_ENDPOINT": "cdp_url",
        "WEBMOTORS_URL": "url",
    })
    if not args.url and not args.ads_file:
        p.error("--url (a search page or an advert) or --ads-file is required")
    if args.url and args.ads_file:
        p.error("--url and --ads-file are two different inputs; pass one")
    if args.pages < 1:
        p.error("--pages must be at least 1")
    args.fetch_url = None
    args.last_vendor = None
    return args


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
