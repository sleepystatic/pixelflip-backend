# tests

Smoke suites for the scraper. Plain scripts, no pytest dependency.

```bash
.venv\Scripts\python.exe tests\run_all.py          # everything, one exit code
.venv\Scripts\python.exe tests\smoke_pro_gate.py   # one suite
```

**Always run them with `.venv\Scripts\python.exe`.** System Python has no
Playwright, so browser paths return nothing and read as scraper bugs.

## Why these are in the repo

They used to live in a session scratchpad, which is wiped between sessions. They
were rebuilt from scratch twice and their HTML fixtures re-fetched from live
marketplaces each time — traffic spent re-proving what was already proven.
Fixtures are committed for the same reason: a test that needs Facebook to be up
is not a test you can run.

## What each suite covers

| suite | covers |
|---|---|
| `smoke_pro_gate.py` | description scanning is Pro-only at **both** gates (API/save and scrape time); title and description are separate haystacks; both inside one scrape |
| `smoke_fb_description.py` | Facebook `redacted_description` extraction, id pinning, two-pass include/exclude, zero fetches when it cannot change the outcome |
| `smoke_cl_description.py` | Craigslist `#postingbody`, boilerplate stripping, non-mutating soup copy, warning when `CRAIGSLIST_FETCH_DETAIL=0` |
| `smoke_ou_description.py` | OfferUp descriptions via its listing-details API (since 2026-11-23, over a fake session): OfferUp's own operation trimmed to id/description/postDate (nothing about the seller), direct connection, paced with the search, cached so a listing is looked up once; 403 drops the session and opens a breaker of its own; a reply about another listing fails closed; blank body = answered; end to end: body-only include, distance checked before any lookup, zero lookups without keywords, the real post date feeds the age filter, refused = red line, switched off = info line, past the per-scan lookup cap a listing waits for the next scan rather than being decided on its title. The walled item-page fallback runs only if `ou_item_*.html` fixtures exist |
| `smoke_mc_description.py` | Mercari `ItemDetail:<id>` extraction, JSON-LD fallback, `og:description` rejected, fail-closed on redirect |
| `smoke_mc_proxy_route.py` | Mercari item pages probe direct once per scrape, reuse the probe, honour `MERCARI_ITEM_PROXY` |
| `smoke_multiuser.py` | browser-free memory budget (`memory_capped_workers`), two concurrent users isolated, one failing user cannot take the other down |
| `smoke_search_cache.py` | shared search cache: per-platform keys, TTL, never caches an empty (blocked) result |
| `smoke_scan_timing.py` | the `Scan timing:` / `Marketplaces scanned:` lines, coalescing window |
| `smoke_digest_cap.py` | email digest cap and the first-scan message |
| `smoke_scrape_lease.py` | cross-instance lease (runs against the REAL database; touches one row's `scrape_claimed_until` and clears it) |
| `smoke_pacing.py` | process-wide pacing (one OfferUp/Facebook request in flight, gap between), cache hits skip the gap, browser budget, continuous-dispatch fairness, merged Mercari cycle cache |
| `smoke_offerup_browserless.py` | OfferUp GraphQL over curl_cffi against the committed live capture; warm-up paid once per thread; 403 drops the session; browser only as fallback |
| `smoke_alert_dedup.py` | still-live listings do not re-alert after the 7-day `listings` purge; dedup is per user; helpers fail open |
| `smoke_pwreset.py` | password reset through the real Flask endpoints: 60s email throttle, 3 wrong codes kill the code and lock the email, no enumeration |
| `smoke_mercari_reason.py` | Mercari names why it is down (JWT expired + when / never minted / unreadable) |
| `smoke_capacity_billing.py` | Stripe price → tier fails closed (unknown price = Basic), unpaid START → 402, `MAX_ACTIVE_USERS` START → 409 under an advisory lock, checkout paused for new subscribers at capacity |
| `smoke_account_security.py` | password change verifies the CURRENT password (5 misses → 429); email change: 60s resend throttle, 3 wrong codes kill the code and lock, the OLD address is notified; reset endpoints never echo exception text; `/api/status` per-user and reset-mailer per-IP rate limits; no 5xx anywhere echoes `str(e)` |
| `smoke_boot.py` | `python app.py` honours `ENABLE_SCRAPER_THREAD=0`; under gunicorn the threads start on the first request in the serving process (Render injects `--preload`); `/health` version/server/thread/preload/billing/warnings/Mercari fields; boot report never prints a secret; `verify_stripe` reports bad ids instead of crashing |
| `smoke_launch_check.py` | `tools/launch_check.py` offline: nslookup TXT parsing, two-SPF-records = FAIL, DMARC, relative logo on a NESTED page = FAIL, old-code `/health` = FAIL, test Stripe key / dead thread / expired Mercari token = FAIL |
| `smoke_trial.py` | 3-day free trial: normalisers (Gmail aliases, IPv6 /64, address formats), no trial offered for a used account / former customer / seen email, IP or device; after checkout a reused card or billing address, a prepaid card or no card ends the trial; webhook + return trip settle once; unverifiable trial = webhook 500; paid checkouts never blocked and remembered; no second subscription; cancel keeps is_pro |
| `smoke_includes.py` | include keywords: word boundaries ('blue' is not Bluetooth), OR semantics, empty = no filter, exclusions outrank includes, per-term scoping, separate title/description haystacks, the two-pass check_includes=False flow |
| `smoke_coalesce.py` | coalescing window config, the sweep and its exact bound, countdown to held terms, floors inside the sweep, /api/status uses the scheduler's window |
| `smoke_term_intervals.py` | scraper-side per-term clamp (forged 5 on Basic scans at 10, slower choice wins), app/scraper/dashboard tables equal, /api/status countdown clamps the same way |
| `smoke_app_intervals.py` | settings save: per-tier menus, off-menu values fall back, the 3-term cap, terms already at 5 outrank newly raised ones, old `priority` clients |
| `smoke_ctx_persist.py` | Mercari token store (in memory, never the real DB): JWT exp is the gate, renew margin (incl. the workflow's 4.5 days), age cap only when exp is unreadable, structural validation, state for the console |
| `smoke_mercari_capture.py` | capture_mercari_token.py exit codes (the GitHub Action's), and per scrape: replay first, MERCARI_ALLOW_CAPTURE=0 never launches Chrome, cooldown not re-armed, bundled-Chromium retry, cooldown armed on double failure |
| `smoke_stop_and_wall.py` | through the real scan function: STOP sends no digest for what was already found (a quick Stop+Start still alerts), marketplaces skipped by a STOP are an info line not a red "NOT scanned"; OfferUp item pages are never requested by default and the breaker stops after two refusals when they are enabled |

## Multi-user load test (manual — not part of `run_all`)

```bash
.venv\Scripts\python.exe tests\loadtest_multiuser.py --users 10 --pro-frac 0.5 --workers 2
.venv\Scripts\python.exe tests\loadtest_multiuser.py --users 100 --pro-frac 0.3 --workers 4
```

Drives the REAL scheduler (`main`) and scrape pipeline for N users on a virtual
clock (default 10x), with marketplace responses replayed from fixtures at
measured latencies and an in-memory database — it touches neither production
nor a marketplace. Reports per-scan duration and CPU, how late each priority /
standard term ran, worker utilisation, duplicate alerts, and the request rate
each marketplace saw. Takes a few real minutes; set `SEARCH_CACHE_ENABLED=1` in
the environment to measure shared scanning.

## Start/stop chaos test (manual — not part of `run_all`)

```bash
.venv\Scripts\python.exe tests\loadtest_startstop.py            # 6 users, 2 workers
.venv\Scripts\python.exe tests\loadtest_startstop.py --seed 7 --users 8
```

The REAL scheduler, lease and abort paths with a mutable armed state: a chaos thread stops users
mid-scan, flips random users and does rapid Stop+Start, then leaves everyone on. Checks: never two
scans of one user at once, no marketplace work for a user who was off the whole time, no alert
digest while a scanner is off, everyone scanned again after re-arming, no duplicate alerts, every
lease released, no completion stamp for a stopped scan, no unfaked SQL or errors. ~2 real minutes.

## Exit codes

`0` pass, `1` fail, `3` skipped for a missing fixture. `run_all.py` treats a
skip as **not green** — a suite that silently does nothing is worse than one
that fails. For the same reason, a suite that exits `0` without printing its
`PASSED n   FAILED n` line is reported as **NO-SUMMARY**, also not green.

## Fixtures

`tests/fixtures/*.html` are real pages captured from live marketplaces. Suites
parse them offline and deterministically. `offerup_listing_graphql.json` is the
exception: it has the exact shape of OfferUp's reply to the trimmed listing
lookup (observed 2026-11-23), with synthetic text, because the real reply is a
stranger's listing.

Refresh only when a platform changes its markup and a suite fails for real:

```bash
.venv\Scripts\python.exe tests\refresh_fixtures.py             # all four
.venv\Scripts\python.exe tests\refresh_fixtures.py craigslist  # one platform
```

`refresh_fixtures.py` is **the only script here that touches a marketplace.**
It is bounded to one search plus two item pages per platform and stops after
three consecutive refusals. Do not loop it:

- **Mercari** is Cloudflare-fronted and needs a live captured token
  (`capture_mercari_token.py` first, or the refresh reports why it cannot).
- **Facebook** shares the residential proxy production uses. IPRoyal's pool
  contains exits that cannot reach `facebook.com` at all — the refresher rotates
  the sticky session once, mirroring the scrape loop.
- **OfferUp** throttles on request *rate*, not IP, so another proxy does not
  help. If its fixtures will not refresh, wait tens of minutes.
