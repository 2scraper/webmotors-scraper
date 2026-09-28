"""
output_writer.py
-----------------
Row models + JSON/CSV writers shared by all three engines.

Two modes, two row classes
--------------------------
    --mode search   Listing   one listing on a search page
    --mode ad       Ad        one advert, read from its own detail endpoint

An advert holds everything a listing does and more (fuel, optionals, the FIPE
value, the site's market-price range), so `Ad` carries `Listing`'s vehicle
columns in the same order, followed by its own. A consumer can therefore
read the first columns of either file the same way.

What both share with the whole family, byte-identical and in order, is the
prefix: `source`, `scraped_at`, `url`, `sku`, `title`, and then `price` and
`currency`, the shop triple minus `brand` (a vehicle's make is `make`, a
different thing from a brand of goods). The run-describing tail — `page`,
`position`, `mode`, `data_source` — is shared too.

Everything below the dataclasses is row-class-agnostic: pass `row_cls` so
an empty CSV still gets the right header for the mode that produced it.
"""

import csv
import json
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type


SOURCE_DEFAULT = "webmotors.com.br"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Listing:
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=_now)
    # The advert's own page, built from the record as the site builds it
    # (product_parser.ad_url; 660 of 660 identical to the site's links).
    url: str = ""
    # `UniqueId`, the advert's numeric id. The last segment of `url`.
    sku: Optional[str] = None
    # `Specification.Title`: make, model and version in the site's capitals.
    title: Optional[str] = None
    # The asking price. `Price` and `SearchPrice` agreed on every record.
    price: Optional[float] = None
    # Read once per run from the JSON-LD of a page the site renders, since
    # no endpoint states it. null if it could not be read, never a default.
    currency: Optional[str] = None

    vehicle: Optional[str] = None           # car | motorcycle
    condition: Optional[str] = None         # used | new (`ListingType` U/N)
    make: Optional[str] = None
    model: Optional[str] = None
    # Cars only: a motorcycle record has no version.
    version: Optional[str] = None
    year_fabrication: Optional[int] = None
    year_model: Optional[int] = None
    odometer_km: Optional[int] = None
    transmission: Optional[str] = None
    body_type: Optional[str] = None
    color: Optional[str] = None
    # Cars only.
    doors: Optional[int] = None
    # Cars only, and stated on 44% of them: `EngineSize` in litres.
    engine_litres: Optional[float] = None
    # Motorcycles only: `CubicCentimeter`, null where the site says 0.
    engine_cc: Optional[int] = None
    # Cars only, stated on 80% of them.
    horsepower_cv: Optional[int] = None
    traction: Optional[str] = None
    # Cars only (`Armored` S/N).
    armored: Optional[bool] = None
    # The seller's ticked facts: "Aceita troca", "IPVA pago", "Único dono"...
    attributes: Optional[List[str]] = None
    # The asking price against the FIPE table, in percent (105 = 5% above).
    fipe_pct: Optional[int] = None
    # The site's "Bom negócio" badge.
    good_deal: Optional[bool] = None
    photo_count: Optional[int] = None
    image_url: Optional[str] = None
    # A listing sold at auction (`Auction`). 1 of 3,000 records captured.
    auction: Optional[bool] = None

    seller_id: Optional[str] = None
    seller_type: Optional[str] = None       # private | dealer (`SellerType`)
    # The site's own word: "Pessoa Física", "Loja", "Concessionária".
    seller_kind: Optional[str] = None
    # A dealer's trading name. ALWAYS null for a private seller: that is a
    # person, and this repo writes no personal name (product_parser).
    seller_name: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None             # the UF, "SP"

    page: Optional[int] = None
    position: Optional[int] = None
    mode: Optional[str] = None
    # The ordering the search ran under. A column, not only a sidecar
    # field, because it decides WHICH listings are in the file: the site
    # serves at most ~10,000 results per search, and "relevance" put 0
    # private sellers in the first 47 where "price-asc" put 44.
    sort: Optional[str] = None
    data_source: Optional[str] = None


@dataclass
class Ad:
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=_now)
    url: str = ""
    sku: Optional[str] = None
    title: Optional[str] = None
    price: Optional[float] = None
    currency: Optional[str] = None

    vehicle: Optional[str] = None
    condition: Optional[str] = None
    make: Optional[str] = None
    model: Optional[str] = None
    version: Optional[str] = None
    year_fabrication: Optional[int] = None
    year_model: Optional[int] = None
    odometer_km: Optional[int] = None
    transmission: Optional[str] = None
    body_type: Optional[str] = None
    color: Optional[str] = None
    doors: Optional[int] = None
    engine_litres: Optional[float] = None
    engine_cc: Optional[int] = None
    horsepower_cv: Optional[int] = None
    traction: Optional[str] = None
    armored: Optional[bool] = None
    attributes: Optional[List[str]] = None
    fipe_pct: Optional[int] = None
    good_deal: Optional[bool] = None
    photo_count: Optional[int] = None
    image_url: Optional[str] = None

    # What only the detail endpoint states.
    fuel: Optional[str] = None
    # The last digit of the plate, which decides the month Brazil's annual
    # licensing falls due. The plate itself is never published.
    final_plate: Optional[int] = None
    optionals: Optional[List[str]] = None
    # A DEALER's description only: a private seller's text is a person's
    # own writing and is not written (product_parser.parse_ad).
    description: Optional[str] = None
    # When the advert was created, as the site states it (no offset).
    created_at: Optional[str] = None
    fipe_code: Optional[str] = None
    fipe_price: Optional[float] = None
    # The site's own price range for this version and year, and the state
    # it covers, from its averageprice endpoint.
    market_price_min: Optional[float] = None
    market_price_avg: Optional[float] = None
    market_price_max: Optional[float] = None
    market_state: Optional[str] = None

    seller_id: Optional[str] = None
    seller_type: Optional[str] = None
    seller_kind: Optional[str] = None
    seller_name: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None

    page: Optional[int] = None
    position: Optional[int] = None
    mode: Optional[str] = None
    data_source: Optional[str] = None


# Row classes by --mode, so an engine maps its mode to a schema in one place.
ROW_CLASS_BY_MODE = {"search": Listing, "ad": Ad}

# Modes whose rows are one-per-sku, and therefore safe to dedupe on `sku`
# and to hand to diff_runs.py. Both qualify: a listing and an advert each
# appear once per run.
UNIQUE_BY_SKU_MODES = ("search", "ad")

# Kept under the family's name: CI steps across the family import `Product`
# from output_writer, and a rename that drops it passes every local check
# and reddens CI on the first push.
Product = Listing


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages.

    On this site the drop count is NOT expected to be zero on a long run,
    and that is a property of the data rather than a fault. The listings
    are LIVE: the unfiltered catalogue's `Count` read 348,455, 348,458 and
    348,317 at three moments of one session on 2026-09-28. A listing that gains an entry at the top
    between page 1 and page 2 pushes one row from page 1 onto page 2, where
    it is fetched a second time. The duplicate is dropped here. The mirror
    case, an entry REMOVED above the cut, pushes one row from page 2 onto
    page 1 after page 1 was fetched, and no scraper can see that row. The
    README says so.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


# Kept under its old name: the engines and smoke tests in this family all
# call it.
def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


# CSV cannot hold a list. Joining with " | " keeps the cell readable in a
# spreadsheet and round-trippable by splitting on the same separator; the
# JSON output keeps the real list, so nothing is lost for a consumer that
# wants structure. `repr()` of a Python list (the default if this is not
# handled) is neither readable nor parseable by anything but Python.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    return v


def write_json(rows: Sequence[Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Listing) -> None:
    # An empty result still gets the header row. A zero-byte file makes a
    # consumer fail on read (no columns to parse) instead of reading a valid
    # table with zero rows — and "an empty result is still a well-formed
    # result" is the same principle as `save` refusing to overwrite good data.
    #
    # The header comes from `row_cls`, not from the first row, so an empty
    # run still writes the columns of the mode that produced it.
    fieldnames = [f.name for f in fields(row_cls)]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: _csv_value(v) for k, v in asdict(r).items()})


# Exit code used when a run completes but produced nothing. Distinct from 1
# (crash) so a caller can tell "ran, found nothing" from "blew up".
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked before any data arrived: PerimeterX's refusal,
# or CloudFront refusing the address. Distinct from EXIT_NO_PRODUCTS so a
# caller can tell "the listing genuinely has nothing in it" from "something
# stood between us and the listing".
#
# An empty listing is NOT this code. A search that matches nothing answers
# HTTP 200 with `SearchResults: []`, and that is EXIT_NO_PRODUCTS: the
# request was served exactly as asked.
EXIT_BLOCKED = 3

# A REMOTE service failed — the Scraping Browser refusing the connection
# (`profile_locked` is the common one: a profile allows a single live
# connection), or the Scraper API answering an error. Distinct from 1 (a
# crash in this code) and from 2 (bad usage) because it means "try again, or
# use a different profile", not "there is a bug here". Defined once, here,
# because the browser engines and scraper_api_client.py both return it and
# two definitions of the same code is exactly how a family's exit contract
# drifts.
EXIT_API_ERROR = 5

# Exit code for a run that gathered SOME rows and then stopped early — a
# page-load timeout, a refusal on page 3 of 10. The output file is still
# written (throwing away three good pages would be worse), but it is not a
# complete picture, and a consumer that cannot tell the difference will read
# the pages that were never fetched as listings that disappeared from the
# catalogue. See write_run_meta.
EXIT_PARTIAL = 6


# Exit code for a run that never GOT its pages: a navigation timeout, a dead
# or unauthenticated proxy, a DNS failure, or an edge answering with
# something that is not the page that was asked for.
#
# Distinct from EXIT_NO_PRODUCTS because those are opposite facts. Exit 4 is
# a statement about the CATALOGUE — "we asked, and the answer was nothing" —
# so handing it to a run that never reached the site tells a pipeline the
# listing is empty when nothing was read at all.
#
# 5 rather than a new number, and 5 rather than EXIT_PARTIAL:
#
#   * this family's contract already reserves 5 for a transport failure
#     (scraper_api_client has used it for a remote API error since it was
#     written), so this needs no new code and no per-repo table for a caller
#     driving more than one of these scrapers;
#   * EXIT_PARTIAL (6) means "some rows were gathered and the output is
#     incomplete". A run holding nothing writes no output at all, so a
#     consumer that reads the file on a 6 finds either nothing or the
#     PREVIOUS run's good data, which `save` deliberately does not
#     overwrite. Exit 5 promises no file.
#
# Deliberately NOT applied when rows WERE gathered: a timeout on page 7 of
# 10 is a partial run (exit 6, output written), which is already right. This
# decides only what a run holding nothing reports.
EXIT_FETCH_FAILED = 5


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path.

    Deliberately a separate `<out>.meta.json` rather than columns on every
    row: this describes the RUN, not the product, and repeating it across
    every row would both bloat the output and change the schema every
    consumer of this project already parses.

    diff_runs.py reads it to refuse a comparison between runs that are not
    both complete, and between runs of different `mode`.
    """
    path = f"{out_prefix}.meta.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "listing", source: str = SOURCE_DEFAULT,
             extra: Optional[dict] = None) -> dict:
    """Build the metadata dict for a finished run.

    `status` is the field a consumer branches on:
      complete — every requested page was fetched, or the site's own
                 pagination genuinely ran out (nothing more existed to get)
      partial  — rows were gathered, then the run stopped early
      failed   — nothing was gathered at all

    `mode` and `source` are recorded because `mode` is not implied by the
    repo: one output prefix can hold a search run or an advert run, and
    those have different row classes. diff_runs.py refuses a pair whose
    modes or sources differ.

    `extra` carries facts about the run that are not about any single row:
    the query that was sent (the listing address, ordering and page size)
    and the site's OWN count of what matched (`total_results`,
    `pages_available`, `capped_by_site`). The count is the only honest way to
    say how much of a listing a run holds. A 3-page run over the whole
    catalogue is complete as a REQUEST and a 141-of-348,317 sample as a
    LISTING, and only the sidecar can say so.

    `pages_failed` lists the pages that did not yield data, by number.
    `pages_completed` alone was enough only while pages were fetched strictly
    in order, where "3 of 10 completed" could only mean 1-2-3: a count is not
    a description once pages can be fetched independently and page 3 can fail
    while 4 and 5 succeed. Recording the numbers keeps the sidecar honest
    about WHICH part of the catalogue is missing, not just how much.
    """
    meta = {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        # Named "products" even though these are vehicle listings, and kept that way deliberately: every repo in this family
        # writes this key, and a consumer reading several of them reads one
        # sidecar shape. The row TYPE is `mode`, right beside it.
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        # Merged rather than nested under a key, so a consumer reads
        # `shop_rating` at the top level beside `products`. Run fields win a
        # name collision: a caller cannot accidentally overwrite `status`.
        meta.update({k: v for k, v in extra.items() if k not in meta})
    return meta


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Listing) -> int:
    """Write JSON/CSV and return a process exit code.

    Returns 0 when rows were written, EXIT_NO_PRODUCTS when there were none.
    Callers are expected to exit with it.

    On zero rows, nothing is written at all unless `allow_empty`. Two reasons,
    and a live run demonstrated both. A page-load timeout produced
    `Saved 0 rows -> out.json` and exit 0: a two-byte `[]` that a
    consuming pipeline reads as a successful run with no stock. Worse, if the
    file already held a good result from an earlier run, that result is now
    gone — the failure destroyed the last known good data. So an empty result
    leaves the previous file intact and says why.

    `allow_empty=True` is for the legitimate case: a filter that genuinely
    matches nothing, where an empty file is the answer.
    """
    if not rows and not allow_empty:
        print(f"[!] 0 rows — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected answer.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} rows -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} rows -> {out_prefix}.csv")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see. Anything
# else ended the page loop early, so the result is only a partial view.
#
# "no_new_products" belongs here and "pagination_exhausted" is kept for the
# engines that still stop on a missing next-link: the first is a property of
# the DATA (a page contributed nothing not already seen, so the listing is
# over), while the second is a property of a CSS SELECTOR and is therefore
# the weaker signal — a renamed attribute looks identical to a short
# catalogue.
#
# On this site there is a third and stronger signal, the site's own
# arithmetic. Every listing states its total on page 1, so the number of
# pages is PLANNED rather than discovered, and a run that fetched them all
# ends "completed". "end_of_listing" is the data-side stop: a page came back
# empty, meaning the live listing shrank below the plan during the run.
# That is complete too, because there was nothing more to get.
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted", "no_new_products",
                         "end_of_listing")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "listing", source: str = SOURCE_DEFAULT,
               extra: Optional[dict] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them.

    The metadata sidecar is written ONLY when the row file was written.
    Otherwise a failed run would leave a "status": "failed" sidecar next to
    the previous run's still-intact good output (which `save` deliberately
    does not overwrite) — the two files would contradict each other, and
    diff_runs.py would refuse to compare data that is in fact fine.
    """
    # Completeness is decided by the reason AND by the evidence. A named
    # list of stop reasons cannot cover a failure recorded somewhere else,
    # and `pages_failed` is somewhere else: a run whose loop ended for a
    # COMPLETE reason while individual pages failed reported exit 0 and
    # `status: complete` with a non-empty `pages_failed` in the same
    # sidecar — a file that contradicts itself, and a pipeline branching
    # on `status` reading a short run as a whole one.
    #
    # Found by a third-party audit of a sibling repo and measured across
    # the family by CALLING each `finish_run` rather than grepping for the
    # fix: 28 of 32 repos behaved this way. Same shape as the exit-code
    # unification this file already carries — a rule keyed on a list of
    # names has a hole for every name nobody added to it.
    complete = stop_reason in COMPLETE_STOP_REASONS and not pages_failed
    row_cls = ROW_CLASS_BY_MODE.get(mode, Listing)
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls)
    wrote_output = bool(rows) or allow_empty

    if wrote_output:
        status = "complete" if (rows and complete) else (
            "partial" if rows else "failed")
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, mode=mode, source=source,
            start_url=start_url, final_url=final_url, products=len(rows),
            extra=extra))

    if not rows:
        # Nothing gathered at all, and WHY decides the code. The three
        # outcomes are different facts and a pipeline branches on them
        # (blocked is not empty is not "never reached"):
        #
        #   blocked            something stood between the run and the content
        #   did not complete   we never got the pages — a dead proxy, a load
        #                      timeout, an edge serving something else
        #   completed          we asked, and the answer was nothing
        #
        # Keyed on `not complete` rather than on a list of stop reasons, on
        # purpose: a list cannot cover a reason nobody has added to it yet,
        # so a new one falls silently through to "the catalogue is empty" —
        # which is the defect this branch exists to prevent.
        if blocked:
            return EXIT_BLOCKED
        if not complete:
            print(f"[!] Nothing was gathered and the run did not finish "
                  f"({stop_reason}) — exit {EXIT_FETCH_FAILED}, NOT an empty "
                  f"result (exit {EXIT_NO_PRODUCTS}). Nothing can be "
                  f"concluded about the catalogue from this run.")
            return EXIT_FETCH_FAILED
        return rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
