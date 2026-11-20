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
| `smoke_ou_description.py` | OfferUp date+body from **one** fetch, identity scoping, legacy posted-date path unchanged |
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

## Exit codes

`0` pass, `1` fail, `3` skipped for a missing fixture. `run_all.py` treats a
skip as **not green** — a suite that silently does nothing is worse than one
that fails. For the same reason, a suite that exits `0` without printing its
`PASSED n   FAILED n` line is reported as **NO-SUMMARY**, also not green.

## Fixtures

`tests/fixtures/*.html` are real pages captured from live marketplaces. Suites
parse them offline and deterministically.

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
