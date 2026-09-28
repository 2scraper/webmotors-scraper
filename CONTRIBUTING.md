# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key, and takes
about a second:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own check count, and lists any group it had to skip because an
engine library is absent.

**The suite must pass with no engine installed at all.** CI installs only
`beautifulsoup4` and `requests`, so any import of `playwright_scraper`,
`puppeteer_scraper` or `selenium_scraper` in a test has to sit inside
`try/except ImportError` with the skip recorded. This is easy to get wrong
locally, where you almost certainly have an engine installed and an unguarded
import passes.

If the suite fails on a clean clone, that is itself the bug — say so.

## Never commit a credential

`.env` is in `.gitignore`. Keep it there.

The scrapers mask `user:pass@` in their own log lines, but three things are **not**
masked: raw HTML dumps, the Scraper API's `x-debug` response header, and your
shell history. Before pasting any output into an issue or a PR, replace keys,
proxy passwords and full `ws://user:pass@host:9222` endpoints with `***`.

CI fails the build if something that looks like a credential is committed. That
check is a backstop, not a review — a leaked key has to be rotated whether or
not the check caught it.

## Reporting a site change

Webmotors changing its API is the normal way this stops working, and it has
its own issue template. The parser reads no HTML for its rows: every row
comes out of the JSON endpoints the site's own front end calls
(`product_parser.py` names them). So there are five things that can break,
and each is loud or guarded:

1. **The search envelope.** `SearchResults`, `Count`, `Pagination` and
   `FilterCustom`. Without `SearchResults` a response is not classified as
   content at all, and the run retries and then fails loudly.
2. **The filter echo** (`FilterCustom.Veiculos`, `Sigla`). This is how a
   misspelt make or model is caught, because the site answers it with a
   WIDER search rather than an error. If the echo moves, `filter_mismatch`
   stops seeing anything and says nothing — the one guard here that fails
   OPEN, by design, since refusing correct runs over a response it cannot
   read would be worse.
3. **A record's own field names** (`Specification.Version`, `Prices.Price`,
   `Seller.SellerType`, ...). This is the one that can be QUIET: the row
   still writes, with that column null. `page_flow.CORE_FIELDS` is the
   guard, a coverage floor of 99% on the columns every captured record
   carried.
4. **The advert slug.** Row URLs are built as the site builds them
   (`product_parser.slugify`, 660 of 660 identical to the site's own links),
   and the detail endpoint answers a wrong slug with 404. If `--mode ad` on
   a fresh search's output starts reporting adverts as gone, the rule moved.
5. **The gates.** CloudFront (the address) and PerimeterX (the client). The
   canary runs through a residential proxy and fails by name on either.

If you are reporting a break, say which of those five it is, and attach the
`--dump-html` output: the exact JSON the parser was given, on success as well
as failure.

## Before this repository goes public

One item cannot be undone later, so it belongs on a checklist rather than in
someone's head. **A commit on top cannot reach what a published tag and a
merged PR's refs already hold** — those stay attached to the PR and cannot be
deleted from it. Afterwards, only a fresh repository removes anything.

```bash
python3 .github/ci_checks.py --history-check
```

That applies the same credential rules CI enforces to **every blob that has
ever existed**, not just the working tree. It is deliberately not part of
`--all` and not run by CI: it shells out to git once per object, and a dirty
history needs a decision, not a red check on every push.

Then the rest of the presentation, in the order that matters:

1. `python3 smoke_test.py` green, and the canary dispatched at least once.
   Without a `WEBMOTORS_PROXY` secret it SKIPS with a notice, because
   CloudFront refuses the runner's datacentre address; dispatch it once to
   see the skip branch run, and again with the secret set to see the real
   one.
2. The repo description, homepage and topics set (see the family notes on
   what those should say).
3. Only then the row in the org profile README — and check it with an
   ANONYMOUS request rather than your own logged-in browser. A row pointing
   at a private repo is a 404 for every visitor, which costs more trust than
   the missing row.

## Pull requests

**Add a test for the behaviour you are changing.** `smoke_test.py` is a single
file of plain functions. Its fixtures are real API responses, trimmed, in
`fixtures_generated.json`, which `make_fixtures.py` regenerates from a
capture directory. Copy the nearest existing check and edit it.

Six properties in this repo exist because they were measured against
expectation and cost real time. Tests pin all six, so a PR that breaks one
fails rather than silently regressing:

- **The user agent override is load-bearing, and so are its client hints.**
  PerimeterX refused headless Chromium's default UA (`HeadlessChrome`) 0 of 3
  times served and the same browser with a plain Chrome UA 3 of 3. And a
  UA override that drops the client hints (pyppeteer's `setUserAgent`) was
  refused 2 of 2: `page_flow.ua_override` sends both.
- **The API widens a search it does not understand.** A misspelt model is
  answered with every model of the make, an unknown make with the whole
  catalogue, an unknown state with the whole country, an unknown `order`
  with the default ordering — all HTTP 200. The ordering is allowlisted, the
  state is checked before sending, and make/model are checked against the
  site's own echo after page 1.
- **A page past the end is page 1 again.** `actualPage=500` of 40 returned
  ten rows of page 1 with `PageCurrent: 500` echoed back. Pages are planned
  from page 1's `PageTotal`, and a page holding only rows already fetched
  ends the run.
- **Sponsored new-car tiles are not listings.** `UniqueId: 0`,
  `MediaZeroKm: true`, no seller. They are skipped and `position` counts only
  the rows written.
- **No endpoint states a currency.** It is read once per run from a listing
  page's JSON-LD and threaded into the rows; a run that cannot read it
  writes null, never a default.
- **A private seller is a person.** Their name, postal code and own
  description are never written, and the fixtures are scrubbed of them.

Plus the family's own invariants, which are not negotiable:

- **A run that finds nothing writes nothing.** It must not replace a good
  output file with `[]`. `--allow-empty` is the opt-out.
- **Exit codes are a contract**, not decoration: `0` ok, `1` crash, `2` bad
  usage, `3` blocked, `4` zero rows — including a query that genuinely
  matched nothing, which is a correct answer — `5` remote API error, `6`
  partial. A pipeline branches on these.
- **An EMPTY page is never retried and never counted as blocked.** A query
  that matched nothing was served exactly as asked.
- **Credentials never reach argv or a log, and an exception message is a
  log.** The masker is global rather than first-occurrence: a Playwright
  connection error repeats the endpoint five times.
- **Merge in page order, not arrival order**, so concurrency cannot change
  the output.

### If your change needs a live run

Most do not: the suite covers the parser, the writers, the classifier, the
shared fetch loop and the CLI contract against real, trimmed responses. If
yours genuinely needs webmotors.com.br, say in the PR what you ran (engine,
mode, query), through what kind of exit, and what you got, including the
sidecar's `total_results`.

Two things about running this live that are specific to Webmotors:

* **You need a residential exit** (or a residential connection of your
  own). CloudFront refused every datacentre address measured. A rotating
  residential gateway is accepted only some of the time per exit, so a
  refusal on one attempt is not a finding; three in a row is.
* **Selenium cannot authenticate a proxy.** From a server, test it through
  a proxy that authorises by source address, or not at all.

**Run more than the primary engine.** "Mirror them exactly" is a design
rule, not a verification. The fetch loop is shared (`page_flow.run_pages`),
but each engine's driver plumbing is its own — pyppeteer's user-agent
override was refused by the site while Playwright's was served — and only
running it proves it.

## Scope

This repo reads **public data** on webmotors.com.br: search results and
advert pages, exactly as the site's own front end fetches them for an
anonymous visitor.

Out of scope: anything behind a login, a seller's contact details, anything
that sends a lead, a message or a proposal to a seller, and anything that
defeats a protection rather than passing it the way an ordinary browser
does.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
