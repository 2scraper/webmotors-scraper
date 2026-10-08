# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/) as closely as a CLI
toolkit can: a patch release means **fixes**, not that every flag and
default is frozen. A default that changes behaviour for an existing user is
said so at the top of its release notes.

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
