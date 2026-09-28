# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/) as closely as a CLI
toolkit can: a patch release means **fixes**, not that every flag and
default is frozen. A default that changes behaviour for an existing user is
said so at the top of its release notes.

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
