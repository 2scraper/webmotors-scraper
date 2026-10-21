# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/) as closely as a CLI
toolkit can: a patch release means **fixes**, not that every flag and
default is frozen. A default that changes behaviour for an existing user is
said so at the top of its release notes.

## [Unreleased]

> **Behaviour change:** a run through a proxy now makes one pre-check
> request before starting a browser. A proxy that answers **HTTP 407** or
> **402** ends the run with **exit 5** and
> `<out>.latest_attempt.meta.json` (`status: failed`, `stop_reason:
> proxy_auth_failed` / `proxy_balance_exhausted`), instead of timing out on
> every retry. Runs without a proxy or over `--cdp-endpoint` are unchanged.
> Media still loads by default.
>
> **Behaviour change:** a run through a local proxy now **stops at 10 MB of
> proxy traffic** by default (`--proxy-budget-mb`, `0` = no cap). It keeps
> its rows and exits **6** (**5** with none) with `stop_reason:
> proxy_budget_exceeded`, and it always writes
> `<out>.latest_attempt.meta.json`.

### Added

- `proxy_meter.py`: a per-run proxy traffic meter and budget for all three
  engines, held in the shared page loop (`page_flow`). It is active only
  through a local proxy.
  - Counting: Playwright sums `request.sizes()`. pyppeteer and Selenium count
    CDP `Network.loadingFinished.encodedDataLength`, plus each request's own
    size.
  - Over budget: Playwright aborts new requests in a route, and pyppeteer
    and Selenium block every URL over CDP. The loop, and with `--concurrency`
    every worker, stops before its next request.
  - New sidecar fields `proxy_bytes`, `proxy_budget_bytes` and
    `proxy_budget_exceeded`, and a closing log line
    `Proxy traffic: X.X MB of Y MB budget`.
  - `--proxy-budget-mb`, and `WEBMOTORS_PROXY_BUDGET_MB` (commented out in
    `.env.example`).
- CI only: the canary's search and ad-mode runs pass `--proxy-budget-mb 10`.
  Each one writes its proxy traffic to the step summary, and fails with
  `::error::stopped: proxy budget 10 MB exceeded` when the cap was hit
  (`.github/proxy_traffic.py`).
- Offline checks:
  - The meter against known CDP and Playwright sizes.
  - An HTTP client through a stub proxy that counts every byte.
  - The sidecars and exit codes over budget.
  - The shared loop driven past the budget, sequentially and with two
    workers.
  - Each engine installing the meter only through a proxy.
  - The canary steps and their summary script.
  - An opt-in real-browser check (`PROXY_METER_BROWSER=1`).

- `proxy_check.py` adds the proxy pre-check to all three engines, through
  `page_flow.stop_on_refused_proxy`. It makes one CONNECT plus
  `HEAD /robots.txt` on `www.webmotors.com.br` and uses no third-party
  service.
  - On a 407, the message names `WEBMOTORS_PROXY` and says the gateway
    refused the credential (or the traffic balance is exhausted). On a 402,
    it says the proxy traffic/balance is exhausted. Neither message prints
    the credential.
  - A proxy that does not answer keeps the existing behaviour.
- `--block-media`: an opt-in on all three engines that aborts image,
  video/audio and font requests. It is off by default on this site, because
  the effect on PerimeterX could not be measured from an address CloudFront
  serves.
- `--load-media` and `WEBMOTORS_LOAD_MEDIA` (commented out in
  `.env.example`) keep media loading.
- Offline checks:
  - A stub proxy that answers 407, 402 or 200, or does not answer.
  - Each engine driven to exit 5 without a browser on a 407.
  - The attempt sidecar.
  - The Playwright route handler, the pyppeteer Fetch patterns and the
    Selenium blocked-URL list.
  - When each engine installs blocking.

### Changed

- CI only: the canary bills a paid route (`WEBMOTORS_PROXY` or
  `WEBMOTORS_CDP_ENDPOINT`), so it now runs weekly (Mondays 06:20 UTC) instead of
  daily; a manual dispatch still runs it.

### Fixed

- The pyppeteer and Selenium proxy meter counts only `http://` / `https://` requests and responses that came over the network. Chrome's own loads (the `chrome://` new-tab page, `data:`, `blob:`), cache hits (`requestServedFromCache`, `fromDiskCache`, `fromPrefetchCache`, `fromServiceWorker`) and Chrome's error page count nothing. A sibling repo's Selenium run had counted about 2.8 MB of new-tab scripts while its proxy carried about 6 KB. The pyppeteer block sent once the budget is spent now reaches the page's own event loop even when the budget runs out on another thread.

## [0.2.0] — 2026-10-15

> **Behaviour change:** `--retries 0` is now a usage error (exit 2). It used
> to be accepted and made the page-load loop unreachable, so nothing was
> fetched and the engine reported the browser's own blank page as a result:
> exit 3 or 4 with a 39-byte "capture", in tens of milliseconds. If a
> script of yours passes 0, pass **1**: the flag counts ATTEMPTS, so 1 means
> "try once and do not retry".

> **Behaviour change:** negative values of `--delay`, `--retry-delay` and
> `--proxy-block-retries`, `--concurrency` below 1 and `--pages` below 1 are
> now usage errors (exit 2) naming the flag, in every browser engine. In
> `scraper_api_client.py`, `--retries`, `--delay` and `--retry-delay` below 0
> and `--pages` below 1 are refused the same way (`--retries 0` stays legal
> there: it counts EXTRA attempts).

> **Behaviour change:** CSV cells that begin with `=`, `+`, `-`, `@`, a tab
> or a line break now get a leading `'`, so a spreadsheet reads them as text.
> Numbers and the JSON output are unchanged; the sidecar's
> `csv_cells_escaped` says how many cells were changed.

> **Behaviour change:** the canary now FAILS, naming the missing secret, when
> neither `WEBMOTORS_PROXY` nor `WEBMOTORS_CDP_ENDPOINT` is set. It used to
> skip and end green without contacting the site.

### Fixed

- **A failed write destroyed the previous good output.** JSON, CSV, the
  `.meta.json` sidecar and `diff_runs.py --out` were opened with truncation,
  so a full disk or an interrupt mid-write left a short, invalid file where a
  good one had been. Each file is now written to a temporary file in the same
  directory, flushed, synced and renamed over the target; the temporary file
  is removed on failure. A new file gets the mode `open()` would have given
  it (`0o666` minus the umask); an existing file keeps its own mode. The
  three files are replaced one by one, not as a single transaction.
- **CSV cells were not protected against formula injection.** Titles,
  versions, attributes, optionals and dealer names and descriptions are
  third-party text, and a cell starting with `=` ran as a formula when the
  CSV was opened in a spreadsheet. Escaping runs after list columns are
  joined.
- **A copied `.env.example` broke the README's flag examples.** It shipped an
  active `WEBMOTORS_URL`, and a URL from `.env` was treated as a typed
  `--url`, so `--make volkswagen --model gol` exited 2. The example line now
  ships commented out, and a `WEBMOTORS_URL` from the environment is only a
  default: typed search flags, `--ads-file` or a different `--mode` replace
  it. A typed `--url` with filter flags is still refused.
- **The Scraping Browser 401 hint claimed credentials "last about a day".**
  That figure is not a constant (an endpoint was seen working after ten
  days). The hint, README, TROUBLESHOOTING and `.env.example` now say the
  credentials were refused (expired, revoked or mistyped) without guessing a
  lifetime.
- **The bug-report template's example command used flags this scraper does
  not have.** It now shows a Volkswagen Gol search.
- **The banned-wording check read virtualenvs.** A `.venv` inside the repo,
  as the README suggests, had its site-packages scanned. Directories holding
  a `pyvenv.cfg` are now skipped.
- **Comments cited private notes by section number.** They now state the
  reason in place, and a suite check fails if such a citation returns.

- **`--retries 0` fetched nothing and called it a result.** The load loop is
  `range(1, retries + 1)`, so 0 never runs it and the engine judged
  `about:blank`. Three third-party audits of this family ran exactly that and
  reported a working scraper as broken. `--retries` now goes through
  `page_flow.attempts` in every entry point; a check reads the source of each
  one and calls the validator, so a new engine declaring the flag with a
  plain int fails the suite.

### Changed

- **The canary covers ad mode** (the first three adverts of the car search)
  and accepts a 2Captcha Scraping Browser API endpoint
  (`WEBMOTORS_CDP_ENDPOINT`) as well as a residential proxy. Without either
  secret it fails rather than skipping.

## [0.1.1] — 2026-10-08

> **Exit codes change for a page the parser cannot read.** v0.1.0 treated a
> listing record with no usable `UniqueId` or no `Seller` as advertising, so
> a moved payload shape read as an EMPTY page: exit 4 on page 1 ("nothing
> matched"), and a `complete` run with exit 0 when it happened on a later
> page. It now ends as `parser_found_nothing`: exit 5 when nothing was read,
> exit 6 (`partial`, rows kept) when earlier pages were. A pipeline that
> treated exit 4 as "empty search" was being told something false.

Fixes from a third-party audit of v0.1.0, each reproduced before changing
anything and each pinned by a check that a planted fault turns red.

### Fixed

- A record is sponsored only on a POSITIVE marker (`MediaZeroKm` or
  `AdvertisementLink`). Measured across every capture: 3 of 3 sponsored
  tiles carry both, 0 of 3,386 listings carry either. A listing that cannot
  be read is counted in the sidecar's new `malformed_records` and logged.
- Core columns that fall below their coverage floor are recorded in the
  sidecar (`columns_below_floor`), not only logged, and the canary fails on
  them and on any malformed record.
- Ad mode: the sidecar's `query` identifies the SET of adverts asked for
  (`ads_sha256`, order-independent) and lists them in `ads_requested`.
  v0.1.0 recorded only their number, so `diff_runs.py` compared two
  different lists of one length.
- `diff_runs.py` refuses two search runs of different depth
  (`pages_requested`): the extra pages read as listings the market added.

### Changed

- A skipped canary run says so at the top of its summary page, and the
  README says the badge is green because the canary skips: this repository
  has no proxy secret set.

## [0.1.0] — 2026-09-28

First release. Two modes over webmotors.com.br's own JSON endpoints, three
browser engines over one shared fetch loop, and the 2Captcha Scraper API
client.

### Added

- `--mode search`: a search page of cars (`/api/search/car`) or motorcycles
  (`/api/search/bike`), one row per listing. Any listing address the site
  builds is accepted with `--url`; `--category`, `--condition`, `--state`,
  `--make` and `--model` build one. The five orderings the site's own menu
  offers (`--sort`), and `--per-page` up to 100.
- `--mode ad`: one row per advert (`/api/detail/{car,bike}` plus
  `/api/detail/averageprice`), from `--url` or `--ads-file` (a search run's
  JSON output, or one address per line). An advert the site answers 404 for
  is recorded in the sidecar's `ads_gone` and does not fail the run.
- Row URLs built exactly as the site builds its advert links (660 of 660
  identical on the day), so a search's output feeds `--mode ad` directly.
- The currency, read once per run from a listing page's JSON-LD, since no
  endpoint states one.
- Refusals up front and after page 1 for what the API would otherwise answer
  with a WIDER search: an unknown state before anything is sent; a make or
  model the site did not apply, from its own `FilterCustom` echo.
- Sidecar fields `total_results`, `pages_available`, `reachable_max`,
  `capped_by_site` (the site's ~10,000-result cap), `sponsored_skipped`,
  `currency` and, in ad mode, `ads_gone`.
- `tools/capture.py` and `make_fixtures.py`: raw captures, then scrubbed,
  trimmed fixtures proven to parse like their originals.

### Measured and built in

- CloudFront refuses datacentre addresses; the engines warn when run with no
  proxy and no `--cdp-endpoint`, and retry a refused page three times from a
  fresh browser, since a rotating residential gateway gives each one a new
  exit (5 of 12 accepted on a `-region-br` login, 9 of 12 on `-region-us`).
- PerimeterX refuses headless Chromium's `HeadlessChrome` user agent (0 of 3)
  and an override that drops the client hints (pyppeteer, 0 of 2). All three
  engines send a complete identity (`page_flow.ua_override`).
- pyppeteer answers a proxy's authentication over CDP's Fetch domain, since
  its own `page.authenticate` is dead on current Chromium, and connects to a
  Scraping Browser without `ignoreHTTPSErrors`, which hung it.
- Sponsored new-car tiles are skipped and do not shift `position`.

### Not included, on purpose

- No captcha solver. The one challenge the site showed is PerimeterX's Press
  & Hold, which this repo does not implement, and no client it drives was
  shown it once the identity fixes were in. `captcha_solver.py` from the
  family core is not shipped rather than kept as dead code.
- No private seller's name, postal code, phone or description is written.
