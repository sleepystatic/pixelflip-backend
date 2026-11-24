# -*- coding: utf-8 -*-
"""
Schema contract check: does the LIVE database have every table and column the
CODE uses? Plus integrity counts. READ-ONLY.

    .venv\\Scripts\\python.exe tools\\schema_check.py

1. Statically extracts SQL from the backend's Python (ast string constants,
   f-string literal parts included) and pulls out table / column references:
   INSERT column lists, UPDATE ... SET targets, simple SELECT lists, WHERE /
   ON CONFLICT columns, and the idempotent DDL in the ensure chains.
2. Reads the live catalog (read-only transaction) and reports:
     ERROR  a referenced column or table that exists nowhere — a runtime crash
            waiting for the code path that uses it;
     PEND   created by an ensure chain / first-use DDL but not in the database
            yet (normal for code that has not been deployed or exercised);
     OK     everything else.
3. Integrity counts (no row data printed): orphans, duplicates, impossible
   values, armed accounts with nothing to scan.

Static SQL parsing is heuristic: a reported reference is something to look at,
and a clean run means "nothing the parser can see is missing".
"""
import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SOURCES = ('app.py', 'scraper_multi_user.py', 'db_schema.py', 'scrape_logs.py',
           'listing_notifications.py', 'trial_guard.py', 'worker.py', 'email_templates.py')

SQL_START = re.compile(r'^\s*(SELECT|INSERT|UPDATE|DELETE|ALTER|CREATE|WITH)\b', re.I)
KEYWORDS = set("""
select from where and or not null is in as on set values insert into update delete
returning conflict do nothing exists case when then else end distinct order by
group limit offset asc desc nulls last first true false interval now coalesce
lower upper trim count max min sum avg extract epoch left right any all like ilike
between join inner outer cross using having default with over partition filter
current_timestamp current_date cast to_regclass row lock for share skip locked
nowait jsonb text boolean integer bigint timestamptz numeric date time zone
greatest least array_agg string_agg json_agg jsonb_build_object length
""".split())


def string_constants(path):
    """Every string literal in a file, f-strings flattened ('?' for expressions)."""
    tree = ast.parse(open(path, encoding='utf-8').read())
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append((node.lineno, node.value))
        elif isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                parts.append(v.value if isinstance(v, ast.Constant) else '?')
            out.append((node.lineno, ''.join(parts)))
    return out


def split_top(s):
    """Split on commas that are not inside parentheses."""
    out, depth, cur = [], 0, []
    for ch in s:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        if ch == ',' and depth == 0:
            out.append(''.join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append(''.join(cur))
    return [x.strip() for x in out if x.strip()]


def idents(expr):
    """Candidate column identifiers in an expression (keywords, literals, params dropped)."""
    expr = re.sub(r"'(?:[^']|'')*'", ' ', expr)          # string literals
    expr = re.sub(r'%\(?\w*\)?s|%s|\?', ' ', expr)        # placeholders
    expr = re.sub(r'::\w+(\[\])?', ' ', expr)             # casts
    found = []
    for m in re.finditer(r'\b([A-Za-z_]\w*)(?:\.([A-Za-z_]\w*))?\b', expr):
        name = m.group(2) or m.group(1)
        if m.group(2) is None and re.match(r'\s*\(', expr[m.end():]):
            continue                                        # a function call
        if name.lower() in KEYWORDS or name.isupper() and name.lower() in KEYWORDS:
            continue
        found.append(name.lower())
    return found


def _simple_select(s):
    """
    (select_list, schema, table, rest) for a single-table SELECT, else None.

    The FROM must be at parenthesis depth 0 — `EXTRACT(EPOCH FROM col)` is not
    the statement's FROM — and statements with a JOIN or a subquery FROM are
    skipped rather than guessed at.
    """
    m = re.match(r'\s*SELECT\s', s, re.I)
    if not m:
        return None
    depth, i = 0, m.end()
    while i < len(s):
        ch = s[i]
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        elif depth == 0 and re.match(r'FROM\s', s[i:i + 5], re.I) and s[i - 1] == ' ':
            break
        i += 1
    else:
        return None
    tm = re.match(r'FROM\s+(?:(\w+)\.)?(\w+)\b(.*)$', s[i:], re.I)
    if not tm or re.search(r'\bJOIN\b|\(\s*SELECT\b', tm.group(3), re.I):
        return None
    schema = tm.group(1).lower() if tm.group(1) else None
    return s[m.end():i], schema, tm.group(2).lower(), tm.group(3)


def extract(sql):
    """(defined, referenced, tables) from one SQL string."""
    s = ' '.join(sql.split())
    defined, refs, tables = set(), set(), set()
    for m in re.finditer(r'ALTER TABLE (?:IF EXISTS )?(?:public\.)?(\w+)\s+(.*?)(?:;|$)', s, re.I):
        table = m.group(1).lower()
        for c in re.finditer(r'ADD COLUMN IF NOT EXISTS (\w+)', m.group(2), re.I):
            defined.add((table, c.group(1).lower()))
    for m in re.finditer(r'CREATE TABLE IF NOT EXISTS (?:public\.)?(\w+)\s*\((.*?)\)\s*;', s, re.I):
        table = m.group(1).lower()
        defined.add((table, '*'))
        for col in split_top(m.group(2)):
            first = col.split()[0].lower() if col.split() else ''
            if first and first not in ('constraint', 'primary', 'unique', 'check', 'foreign'):
                defined.add((table, first))
    for m in re.finditer(r'CREATE (UNIQUE )?INDEX IF NOT EXISTS (\w+) ON (?:public\.)?(\w+)', s, re.I):
        defined.add((m.group(3).lower(), 'index:' + m.group(2).lower()))
    for m in re.finditer(r'INSERT INTO (?:public\.)?(\w+)\s*\(([^)]*)\)', s, re.I):
        table = m.group(1).lower()
        tables.add(table)
        for c in split_top(m.group(2)):
            if re.match(r'^\w+$', c):          # an f-string expression shows up as '?'
                refs.add((table, c.lower()))
    for m in re.finditer(r'UPDATE (?:public\.)?(\w+) SET (.*?)(?: WHERE | RETURNING |$)', s, re.I):
        table = m.group(1).lower()
        tables.add(table)
        for part in split_top(m.group(2)):
            mm = re.match(r'(\w+)\s*=', part)
            if mm:
                refs.add((table, mm.group(1).lower()))
    sel = _simple_select(s)
    if sel:
        select_list, schema, table, rest = sel
        if schema in (None, 'public'):
            tables.add(table)
            for item in split_top(select_list):
                item = re.sub(r'\s+AS\s+\w+$', '', item, flags=re.I)
                if item.strip() == '*':
                    continue
                for name in idents(item):
                    refs.add((table, name))
            for name in re.findall(r'\b(\w+)\s*(?:=|<>|!=|>=|<=|>|<|\bIS\b|\bIN\b|\bLIKE\b)', rest, re.I):
                if name.lower() not in KEYWORDS:
                    refs.add((table, name.lower()))
    for m in re.finditer(r'ON CONFLICT \(([^)]*)\)', s, re.I):
        for t in list(tables)[:1]:
            for c in split_top(m.group(1)):
                refs.add((t, c.lower()))
    return defined, refs, tables


def collect():
    defined, refs = {}, {}
    for name in SOURCES:
        path = os.path.join(REPO, name)
        if not os.path.exists(path):
            continue
        for line, text in string_constants(path):
            if not SQL_START.search(text):
                continue
            d, r, _t = extract(text)
            for item in d:
                defined.setdefault(item, '%s:%d' % (name, line))
            for item in r:
                refs.setdefault(item, '%s:%d' % (name, line))
    return defined, refs


def live_catalog(cur):
    cur.execute("""SELECT table_name, column_name FROM information_schema.columns
                   WHERE table_schema = 'public'""")
    cols = {}
    for t, c in cur.fetchall():
        cols.setdefault(t, set()).add(c)
    cur.execute("SELECT indexname, tablename FROM pg_indexes WHERE schemaname = 'public'")
    idx = {(t, 'index:' + i) for i, t in cur.fetchall()}
    return cols, idx


INTEGRITY = [
    ('user_settings rows without an auth user',
     "SELECT COUNT(*) FROM user_settings us WHERE NOT EXISTS "
     "(SELECT 1 FROM auth.users u WHERE u.id::text = us.user_id)"),
    ('auth users without a user_settings row',
     "SELECT COUNT(*) FROM auth.users u WHERE NOT EXISTS "
     "(SELECT 1 FROM user_settings us WHERE us.user_id = u.id::text)"),
    ('search terms whose user has no settings row',
     "SELECT COUNT(*) FROM user_search_terms t WHERE NOT EXISTS "
     "(SELECT 1 FROM user_settings us WHERE us.user_id = t.user_id)"),
    ('duplicate (user, search term) pairs',
     "SELECT COUNT(*) FROM (SELECT user_id, search_term FROM user_search_terms "
     "GROUP BY 1, 2 HAVING COUNT(*) > 1) d"),
    ('exclusions/includes pointing at a term that no longer exists',
     "SELECT (SELECT COUNT(*) FROM user_exclusions e WHERE e.search_term IS NOT NULL AND NOT EXISTS "
     "(SELECT 1 FROM user_search_terms t WHERE t.user_id = e.user_id AND t.search_term = e.search_term)) + "
     "(SELECT COUNT(*) FROM user_includes i WHERE i.search_term IS NOT NULL AND NOT EXISTS "
     "(SELECT 1 FROM user_search_terms t WHERE t.user_id = i.user_id AND t.search_term = i.search_term))"),
    ('term intervals off every plan menu (5,10,15,30,60)',
     "SELECT COUNT(*) FROM user_search_terms WHERE interval_minutes NOT IN (5, 10, 15, 30, 60)"),
    ('plan_tier values other than basic/pro/inactive/NULL',
     "SELECT COUNT(*) FROM user_settings WHERE plan_tier IS NOT NULL "
     "AND LOWER(TRIM(plan_tier)) NOT IN ('basic', 'pro', 'inactive')"),
    ('armed accounts with no search terms',
     "SELECT COUNT(*) FROM user_settings us WHERE us.is_active AND NOT EXISTS "
     "(SELECT 1 FROM user_search_terms t WHERE t.user_id = us.user_id)"),
    ('listings with no owner',
     "SELECT COUNT(*) FROM listings WHERE user_id IS NULL"),
    ('leases left claimed for over an hour',
     "SELECT COUNT(*) FROM user_settings WHERE scrape_claimed_until > NOW() + INTERVAL '1 hour'"),
    ('Stripe subscription without a customer id',
     "SELECT COUNT(*) FROM user_settings WHERE COALESCE(stripe_subscription_id,'') <> '' "
     "AND COALESCE(stripe_customer_id,'') = ''"),
    ('tables in public WITHOUT row level security',
     "SELECT COUNT(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
     "WHERE n.nspname = 'public' AND c.relkind = 'r' AND NOT c.relrowsecurity"),
    ('RLS policies on public tables (expected 0: the public keys see nothing)',
     "SELECT COUNT(*) FROM pg_policies WHERE schemaname = 'public'"),
]


def main():
    import psycopg2
    from dotenv import dotenv_values
    defined, refs = collect()
    url = (dotenv_values(os.path.join(REPO, '.env')).get('DATABASE_URL') or '').strip()
    conn = psycopg2.connect(url, connect_timeout=15)
    errors = pend = 0
    try:
        conn.set_session(readonly=True)
        cur = conn.cursor()
        cur.execute("SET statement_timeout = '20s'")
        cols, idx = live_catalog(cur)
        known_tables = set(cols) | {'users'}             # auth.users is referenced by id/email
        print('== Code vs database (%d DDL objects, %d column references)' % (len(defined), len(refs)))
        for (table, item), where in sorted(defined.items()):
            have = (item == '*' and table in cols) or (item.startswith('index:') and (table, item) in idx) \
                or item in cols.get(table, set())
            if not have:
                pend += 1
                print('  PEND   %-28s %-34s created on first use (%s)' % (table, item, where))
        ensure_cols = {(t, c) for (t, c) in defined if c != '*' and not c.startswith('index:')}
        for (table, col), where in sorted(refs.items()):
            if table == 'users':
                continue
            if table not in known_tables and (table, '*') not in defined:
                errors += 1
                print('  ERROR  table %-22s does not exist (%s)' % (table, where))
                continue
            if col in cols.get(table, set()) or (table, col) in ensure_cols:
                continue
            errors += 1
            print('  ERROR  %-28s %-34s referenced but exists nowhere (%s)' % (table, col, where))
        if not errors:
            print('  OK     every referenced table and column exists (or is created on first use)')

        print('\n== Integrity (counts only)')
        for label, sql in INTEGRITY:
            cur.execute('SAVEPOINT q')
            try:
                cur.execute(sql)
                n = cur.fetchone()[0]
                cur.execute('RELEASE SAVEPOINT q')
            except Exception as e:
                cur.execute('ROLLBACK TO SAVEPOINT q')
                print('  ????   %-64s %s' % (label, type(e).__name__))
                continue
            expect_zero = 'expected' not in label or 'expected 0' in label
            status = 'OK  ' if (n == 0 or not expect_zero) else 'WARN'
            print('  %s   %-64s %d' % (status, label, n))
        conn.rollback()
    finally:
        conn.close()
    print('\nerrors %d, pending first-use objects %d' % (errors, pend))
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
