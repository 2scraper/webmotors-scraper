# webmotors-scraper

[![release](https://img.shields.io/github/v/release/2scraper/webmotors-scraper)](https://github.com/2scraper/webmotors-scraper/releases)
[![tests](https://github.com/2scraper/webmotors-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/webmotors-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/webmotors-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/webmotors-scraper/actions/workflows/canary.yml)
![python](https://img.shields.io/badge/python-3.9%20%7C%203.12-blue)
[![licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
![engines](https://img.shields.io/badge/engines-playwright%20%7C%20selenium%20%7C%20pyppeteer-lightgrey)
![needs a residential exit](https://img.shields.io/badge/needs-a%20residential%20exit-orange)

Scrapes [webmotors.com.br](https://www.webmotors.com.br), Brazil's largest
car and motorcycle marketplace, into JSON and CSV:

| Mode | One row per | What you get |
|---|---|---|
| `--mode search` (default) | listing on a search page | price, make / model / version, year built and model year, mileage, transmission, body, colour, doors, engine, horsepower, armoured, the seller's ticked facts ("IPVA pago", "Único dono"), the price against the FIPE table in percent, the "Bom negócio" badge, photo, seller type and kind, city and state |
| `--mode ad` | advert | all of the above plus fuel, plate's final digit, optionals, a dealer's description, when the advert was created, the FIPE code and value, and the site's own market-price range (lowest / average / highest) for that version and year |

Cars and motorcycles, any search the site builds: a state or a city, a make
and model, a price or year range — pass the site's own address with `--url`
and the site's own API reads the filters out of it.

## Start with the part most scrapers bury: you need a residential exit

Measured 2026-09-28, and this is the whole access story:

| Client | Address | Answer |
|---|---|---|
| anything — curl, headless or headful Chromium | datacentre (a VPS) | **CloudFront 403**, before the site sees the request |
| the 2Captcha Scraper API on its own exits | its own | refused (CloudFront, then PerimeterX on a retry) |
| curl | residential | **PerimeterX 403**, its Press & Hold page |
| headless Chromium, default user agent | residential | PerimeterX 403, **0 of 3** |
| headless Chromium, user agent without `HeadlessChrome` | residential | **served, 3 of 3** |
| headful Chromium | residential (BR and US) | served, 2 of 2 each |
| the Scraping Browser API | its own | served |

So two things are needed, and the engines take care of the second:

1. **A residential address**, or the Scraping Browser. Your own home
   connection is one; a 2Captcha residential proxy is another. On a rotating
   residential gateway CloudFront accepts only some exits: twelve fresh
   browsers each, a `-region-br` login was served **5 of 12** times and a
   `-region-us` login **9 of 12** (2026-09-28). The engines therefore retry a
   refused page three times, each from a fresh browser, which a rotating
   gateway answers with a fresh exit. The country does not matter to the
   site; a US exit simply did better that day.
2. **A browser that does not announce itself.** PerimeterX refuses headless
   Chromium's own user agent (it says `HeadlessChrome`), and it refuses a
   user-agent override that drops the matching client hints — pyppeteer's
   `setUserAgent` does exactly that, and was refused 2 of 2 on an exit that
   served Playwright. The engines send a plain Chrome user agent naming the
   browser's own version, with its client hints, and nothing else.

Even so, PerimeterX refused a correctly-configured browser now and then
during the live runs (a fresh browser was then served). That is what the
retry is for, and it is why a refusal on one attempt is not a finding.

**This repo does not implement solving PerimeterX's Press & Hold
challenge.** No client this repo drives was shown it once the two fixes
above were in, so there was nothing to solve.

## Install

```bash
git clone https://github.com/2scraper/webmotors-scraper
cd webmotors-scraper
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-playwright.txt
.venv/bin/playwright install chromium
cp .env.example .env      # then put your residential proxy in WEBMOTORS_PROXY
```

Python 3.9 or newer. **Install one engine per virtualenv** — the three
engine libraries pin mutually unsatisfiable versions of their dependencies.

## Run

```bash
# every Volkswagen Gol in Brazil, three pages of 47
.venv/bin/python playwright_scraper.py --make volkswagen --model gol --pages 3

# the cheapest Corollas from 2020 on in the state of São Paulo
.venv/bin/python playwright_scraper.py \
  --url "https://www.webmotors.com.br/carros/sp/toyota/corolla?anode=2020" \
  --sort price-asc --pages 5

# Honda motorcycles, newest model year first
.venv/bin/python playwright_scraper.py --category motos --make honda --sort year-desc

# then every advert that search found, with FIPE and market prices
.venv/bin/python playwright_scraper.py --mode ad --ads-file webmotors_rows.json --out adverts
```

Output: `webmotors_rows.json`, `webmotors_rows.csv` and
`webmotors_rows.meta.json`, the last describing the run (status, pages, the
site's own total, whether its cap bit, the ordering, the currency). See
[`sample_output.json`](sample_output.json) and
[`sample_output_ad.json`](sample_output_ad.json), cut from real runs.

A search row has 41 columns and an advert row 50; both start with the
family's `source, scraped_at, url, sku, title, price, currency`.

## Six things about Webmotors that will look like bugs

### 1. A misspelt model gives you MORE results, not none

`/carros/estoque/volkswagen/gool` is answered with HTTP 200 and every
Volkswagen on the site (48,983, and 49,140 an hour later, on 2026-09-28); `/carros/estoque/zzzz` with the
whole catalogue; `/carros/xx` with the whole country. So after page 1 the run
reads back what the site says it applied, and refuses (exit 2) when it is not
what you asked for:

```
The site did not recognise the model 'gool' (the site applied every
VOLKSWAGEN model), and answered with HTTP 200 and 49,140 listing(s) anyway.
```

Spell makes and models as the site's own addresses do (`onix-plus`,
`mercedes-benz`, `hb20`).

### 2. The site serves at most ~10,000 results per search

348,317 cars matched the unfiltered search, and the site stated 213 pages of
47 (10,011). A run that fetches every page is complete as a request and
still a sample; the sidecar says `capped_by_site: true`. Narrow the search
to reach the rest.

### 3. `--sort` decides WHICH listings are in the file

Under the site's own default, "Mais relevantes", the first 47 cars of the
whole catalogue were 31 franchise dealers, 16 used-car lots and **no private
sellers**; under "Menor preço" they were 44 private sellers. On a capped
search that is two different samples, so `sort` is a column, the sidecar
records it, and `diff_runs.py` refuses to compare runs sorted differently.
An `o=` in the address is ignored by the site; only `--sort` orders.

### 4. A page past the end is page 1 again

Page 500 of a 40-page search came back with HTTP 200, ten rows of page 1 and
`PageCurrent: 500` echoed back. The run plans its pages from the page count
the site states, and a page holding only rows already fetched ends it.

### 5. The listing moves while you read it

"Mais relevantes" reorders itself within minutes: two runs of the same
search 20 minutes apart shared 89 of their first 94 listings. A row that
moves across a page boundary during a run is fetched twice (dropped, and the
log says so) or not at all.

### 6. Private sellers are people

A private seller's search record carries their postal code and their own
description of the car, and the advert adds their first name. None of it is
written: `seller_name` is null for every private seller, and `description`
is kept for dealers only. City and state are kept. A dealer's trading name is
a business's public name and is kept.

## Engines

| Engine | Proxy with a password | Scraping Browser (`--cdp-endpoint`) | Live on 2026-09-28 |
|---|---|---|---|
| `playwright_scraper.py` (primary) | yes | yes | search 3 pages, ad mode, both through a residential proxy and the Scraping Browser |
| `puppeteer_scraper.py` | yes, over CDP's Fetch domain | yes | search 3 pages through a residential proxy; search through the Scraping Browser |
| `selenium_scraper.py` | **no** — Chrome's `--proxy-server` takes no credentials | **no** — chromedriver cannot authenticate one | search 3 pages through a local forwarding proxy that needed no password |
| `scraper_api_client.py` | — | with `--cdp-url` | search 2 pages with `--cdp-url`; refused without it |

All three browser engines share one fetch loop (`page_flow.run_pages`), so
they agree on exit codes, run status and retries by construction; the rows
Playwright and pyppeteer produced for one search were identical, column for
column. Selenium from a server needs a proxy that authorises by source
address, since it cannot send a password.

pyppeteer is effectively unmaintained. Its bundled Chromium may not start on
a current system; point `--chromium-path` at Playwright's.

## What the 2Captcha products buy here, and when

One key, four separately-billed products:

- **Residential proxies** (`--proxy`) — the thing this site actually needs,
  unless you run from a residential connection of your own. See the table at
  the top for the acceptance rates measured.
- **The Scraping Browser API** (`--cdp-endpoint`) — a remote browser on a
  residential exit, nothing to install; served on every attempt measured.
  One live connection per profile, so no `--concurrency` through it; a
  profile's credentials last about a day.
- **The Scraper API** (`scraper_api_client.py`) — no local browser at all,
  but its own exits were refused, so on this site it works only with
  `--cdp-url` (a Scraping Browser session), measured at $0.0005 a page.
- **Fingerprints** (`--fingerprint`) — not needed here: the engines' own
  identity was served. Kept for parity with the family.

Captcha solving buys nothing on this site: see the end of the first section.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | done |
| 1 | crash — a bug here |
| 2 | bad usage, including a make, model or state the site did not recognise |
| 3 | blocked: CloudFront (the address) or PerimeterX (the client) |
| 4 | the search matched nothing (nothing is written) |
| 5 | the data never arrived: a timeout, a dead proxy, the API gateway refusing a path, the Scraping Browser refusing the connection |
| 6 | partial: some pages came back, a later one did not |

An advert that is gone (sold, withdrawn) is not a failure: it is listed in
the sidecar's `ads_gone` and the run goes on. See
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Configuration

Credentials go in `.env`, never on a command line (`ps` and your shell
history both see argv). `python3 env_config.py` prints what was picked up
without printing a secret. The variables are `TWOCAPTCHA_KEY`,
`WEBMOTORS_PROXY`, `WEBMOTORS_CDP_ENDPOINT` and `WEBMOTORS_URL`; see
[`.env.example`](.env.example).

## Tests

```bash
python3 smoke_test.py
```

Offline, no network, no key, a few seconds. The fixtures are real responses
captured on 2026-09-28 (`tools/capture.py`), cut and scrubbed by
`make_fixtures.py`, which proves each one parses exactly as its original.
CI runs the suite on Python 3.9 and 3.12, once per engine in its own
virtualenv, builds the Docker image, and scans for committed credentials.
The canary runs a real search daily when a residential proxy secret is set,
and skips with a notice when it is not.

## Legal

This reads public pages the site shows every anonymous visitor, at a pace
you set (`--delay`). It sends no lead, message or proposal to anyone, logs
into nothing, and writes no private seller's name or contact details. Check
the site's terms and your local law before you use the data.
