# Troubleshooting

Find your exit code first (`echo $?` straight after the run), then the
sidecar's `stop_reason` in `<out>.meta.json` if one was written.

## Exit 2 — bad usage

* **"The site did not recognise the model 'gool' (the site applied every
  VOLKSWAGEN model)"** — the site answers a misspelt make or model with
  HTTP 200 and a WIDER search (every model of the make, or the whole
  catalogue), and the run was stopped rather than written as a sample of
  something nobody asked for. Spell it as the site's own address does: open
  the page on webmotors.com.br and copy the path (`onix-plus`, `hb20`,
  `mercedes-benz`).
* **"'xx' is not a Brazilian state"** — same reason: an unknown state is
  answered with the whole country. Use the two letters (`sp`, `rj`) or
  `estoque` for all of Brazil.
* **"--url already carries the search"** — pass a URL or the filter flags,
  not both. Merging them could scrape something neither named. A
  `WEBMOTORS_URL` in `.env` counts as a `--url`.
* **"--condition is only offered for cars"** — the used/new listings were
  measured for cars only.

## Exit 3 — blocked

The log names what refused the run:

* **`cloudfront`** — CloudFront refused the ADDRESS, before the site saw the
  request. Every datacentre address measured was refused (a VPS with
  headful Chromium, and the Scraper API's own exits). Use a RESIDENTIAL
  `--proxy`, or `--cdp-endpoint`. A rotating residential gateway hands each
  fresh browser a new exit and CloudFront accepts only some of them —
  measured 2026-09-28: 5 of 12 fresh browsers on a `-region-br` login, 9 of
  12 on `-region-us` — so the engines retry three times from a fresh browser
  before giving up. If it still fails, try the other region, or
  `--proxy-file` with several exits.
* **`perimeterx`** — PerimeterX refused the CLIENT. The engines handle the
  two causes measured: a headless user agent that says `HeadlessChrome`,
  and a user-agent override that drops the client hints. If you see this
  anyway, try `--headful` (under `xvfb-run` on a server), a different exit,
  or `--cdp-endpoint`. This repo does not implement solving PerimeterX's
  Press & Hold challenge.

A `<out>_page<N>_debug.html` beside the output holds what came back.

**Selenium and a proxy with a password:** Selenium cannot send proxy
credentials at all, so the engine strips them and warns, and the refused
request usually comes back as exit 5 or 3. Use `playwright_scraper.py` or
`puppeteer_scraper.py`.

## Exit 4 — zero rows

The search genuinely matched nothing, for example a price range no listing
falls in. Nothing is written, so an earlier good file is left alone;
`--allow-empty` writes the empty file.

## Exit 5 — the data never arrived

* **"The API gateway refused this request (HTTP 403: Missing Authentication
  Token)"** — the path is not one the site's API has. The same request would
  be refused again, so it is not retried. If the address looks right, the API
  changed: open a "Site changed" issue with `--dump-html` output.
* **"Gave up on page N"** — a timeout or a dead proxy. The log names which.
* **`could not connect to --cdp-endpoint`** — a 401 is an expired Scraping
  Browser profile (they last about a day); a 500 is a profile another run
  still holds.

## Exit 6 — partial

Some pages came back and a later one did not. The output holds what was
gathered and the sidecar lists `pages_failed` by number.

## Ad mode: an advert is missing from the output

It is listed in the sidecar's `ads_gone`: the site answered 404 for it. The
advert was sold or withdrawn, or the address is not the site's own for it —
the detail endpoint answers a wrong slug the same way. Addresses taken from
a search run's output are built exactly as the site builds them.

## A run looks fine but a column is wrong

Re-run with `--dump-html response.json`: it writes the exact JSON the parser
was given, on success too, so a parsing bug can be told apart from a change
in what the site sends.

## Duplicates dropped, or a row missing between two runs

The listings are live, and the default "relevance" ordering reorders itself
within minutes. A row that moves across a page boundary while a run reads
the pages is fetched twice (the dedupe drops it and the log says so), or
not at all. That is the site moving, not the scraper.

## Asked for 300 pages, got 213

The site serves at most about 10,000 results per search, and the page count
it states already includes that cap. The sidecar says `capped_by_site: true`
when it bit. Narrow the search (a state, a make, a price range) to reach the
rest.
