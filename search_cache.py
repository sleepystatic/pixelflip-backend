"""
Short-lived cache for platform SEARCH results, shared across users.

WHY
Ten users on 5-minute intervals is ~40 platform searches every five minutes.
Where two users track the same thing, those are byte-identical requests: the
search is a function of (platform, term, location), and nothing about it is
per-user — the per-user part is the filtering that happens afterwards. So the
second user can have the first one's rows for free.

That cuts two things at once, which is why it is worth more than it looks:
  * DURATION — a cache hit skips a network round trip AND its parsing, so the
    user's scrape finishes sooner. On a box that runs one scrape at a time this
    is the only kind of speed-up available, because there is no parallelism to
    add.
  * MARKETPLACE TRAFFIC — the harder ceiling. OfferUp throttles on request
    RATE, not IP, so rotating proxies does not buy headroom; the only thing that
    does is making fewer requests.

CACHE KEYS ARE PER-PLATFORM, because what a search actually depends on differs:
  mercari     term only        — /v1/api searchFacetQuery takes no location at
                                 all, so any two users share a result
  facebook    region + term    — the region slug in the path is the ONLY thing
                                 that controls location; regions are metro-sized,
                                 so many zips collapse onto one key
  offerup     lat/lon + term   — coordinates are rounded (see _round_coord),
                                 since a few hundred metres cannot change a feed
  craigslist  site+postal+radius+term — results are scoped by postal and
                                 distance, so this is the least shareable. It is
                                 also the cheapest platform, so it matters least.

TTL MUST STAY BELOW THE SHORTEST SCAN INTERVAL. If an entry could outlive a
user's own interval, their next scrape would be served their previous one's rows
and they would see no new listings until it expired. At the 5-minute floor, the
120s default leaves a wide margin.

The cost of a hit is latency, never a missed listing: a listing posted during
the TTL window is picked up on the next scan, because `known_links` dedupe is
per-user and unaffected by this. Worst case a user learns about a listing up to
TTL seconds later than they would have.

NEVER CACHE A FAILURE. An empty result is what a block looks like, and storing
one would propagate a single user's block to everyone sharing the key — turning
one user's bad proxy draw into a platform-wide outage for the rest. Only
non-empty results are stored.
"""
import os
import threading
import time

_LOCK = threading.Lock()
_STORE = {}          # key -> (stored_at, rows)
_STATS = {'hits': 0, 'misses': 0, 'stores': 0, 'evictions': 0, 'rejected_empty': 0}


def _cfg_float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _cfg_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def enabled():
    """Off by default: a cache that shares rows between accounts is worth opting into."""
    raw = (os.getenv('SEARCH_CACHE_ENABLED') or '').strip().lower()
    return raw in ('1', 'true', 'yes', 'on')


def ttl_sec():
    return max(0.0, _cfg_float('SEARCH_CACHE_TTL_SEC', 120.0))


def max_entries():
    return max(1, _cfg_int('SEARCH_CACHE_MAX_ENTRIES', 256))


def _round_coord(v):
    """
    One decimal place, ~11km. Two users a few streets apart get the same
    OfferUp feed, and pretending otherwise would make the key per-user and the
    cache useless. Coarser than this starts changing which city leads the feed.
    """
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return None


def key_for(platform, term, **parts):
    """
    Build a cache key. `parts` carries only what that platform's search actually
    varies on — see the module docstring.
    """
    plat = (platform or '').strip().lower()
    t = (term or '').strip().lower()
    if plat == 'mercari':
        return ('mercari', t)
    if plat == 'facebook':
        return ('facebook', (parts.get('region') or '').strip().lower(), t)
    if plat == 'offerup':
        return ('offerup', _round_coord(parts.get('lat')), _round_coord(parts.get('lon')), t)
    if plat == 'craigslist':
        return ('craigslist', (parts.get('site') or '').strip().lower(),
                (parts.get('postal') or '').strip(), str(parts.get('radius') or ''), t)
    # Unknown platform: include everything so it can never collide with another.
    return (plat, t, tuple(sorted((str(k), str(v)) for k, v in parts.items())))


def get(key):
    """Cached rows for `key`, or None. Expired entries are dropped on read."""
    if not enabled():
        return None
    ttl = ttl_sec()
    if ttl <= 0:
        return None
    now = time.time()
    with _LOCK:
        entry = _STORE.get(key)
        if not entry:
            _STATS['misses'] += 1
            return None
        stored_at, rows = entry
        if now - stored_at > ttl:
            del _STORE[key]
            _STATS['misses'] += 1
            return None
        _STATS['hits'] += 1
        # A copy, so a consumer mutating its rows cannot corrupt what the next
        # user is served. The scrape loops do annotate rows as they go.
        return [dict(r) for r in rows]


def put(key, rows):
    """
    Store a SUCCESSFUL result. Empty or falsy results are rejected — see the
    module docstring on why caching a failure is the dangerous case.
    """
    if not enabled() or ttl_sec() <= 0:
        return False
    if not rows:
        with _LOCK:
            _STATS['rejected_empty'] += 1
        return False
    snapshot = [dict(r) for r in rows]
    with _LOCK:
        _STORE[key] = (time.time(), snapshot)
        _STATS['stores'] += 1
        # Bounded. Evict the oldest rather than the least-used: entries are
        # short-lived by design, so age is the only signal that matters.
        cap = max_entries()
        if len(_STORE) > cap:
            for k in sorted(_STORE, key=lambda k2: _STORE[k2][0])[:len(_STORE) - cap]:
                del _STORE[k]
                _STATS['evictions'] += 1
    return True


def stats():
    """Counters for measuring the real hit rate in production."""
    with _LOCK:
        out = dict(_STATS)
        out['entries'] = len(_STORE)
    total = out['hits'] + out['misses']
    out['hit_rate'] = (out['hits'] / total) if total else 0.0
    return out


def format_stats():
    s = stats()
    return ("search cache: %d hits / %d lookups (%.0f%%), %d entries, "
            "%d stored, %d evicted, %d empty results rejected"
            % (s['hits'], s['hits'] + s['misses'], s['hit_rate'] * 100.0,
               s['entries'], s['stores'], s['evictions'], s['rejected_empty']))


def reset(stats_too=False):
    """Clear the store. Tests use this; production never needs it."""
    with _LOCK:
        _STORE.clear()
        if stats_too:
            for k in _STATS:
                _STATS[k] = 0
