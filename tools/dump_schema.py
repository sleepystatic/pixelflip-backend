# -*- coding: utf-8 -*-
"""
Snapshot the production PUBLIC schema as SQL (structure only, never rows), so a
fresh dev Supabase project can be built to match it.

    .venv\\Scripts\\python.exe tools\\dump_schema.py            # -> schema/base_schema.sql
    .venv\\Scripts\\python.exe tools\\dump_schema.py --stdout

READ-ONLY: catalog queries inside a read-only transaction against the
DATABASE_URL in pixelFlipScraper/.env (PRODUCTION). It reads no table rows.

Why this exists rather than the migrations folder: the base tables were made by
hand in Supabase and are in no migration, and production has drifted from the
files that do exist — e.g. migration 005 creates user_dashboard_admin WITHOUT
security_invoker, which in Supabase would expose every user's email to the
public anon key. The catalog is the truth; this reproduces it.

Why not pg_dump: it is not installed here and must match the server's major
version (17). The schema is simple enough to reproduce exactly: sequences,
tables with their constraints, indexes, RLS, policies, views (with options),
functions, the auth.users signup trigger and custom event triggers.

The output is written to be applied ONCE to an EMPTY project (Supabase SQL
editor), inside one transaction, and to be harmless if re-run.
"""
import argparse
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
OUT = os.path.join(REPO, 'schema', 'base_schema.sql')


def q(cur, sql, args=None):
    cur.execute(sql, args)
    return cur.fetchall()


def ident(name):
    """Quote an identifier only when it needs it."""
    if name.replace('_', 'a').isalnum() and name == name.lower() and not name[0].isdigit():
        return name
    return '"%s"' % name.replace('"', '""')


def guarded(check_sql, body, soft_notice=None):
    """
    A DO block that runs `body` only when `check_sql` finds nothing.

    With `soft_notice`, a failure is caught (PL/pgSQL rolls back to an implicit
    savepoint) and reported as a NOTICE, so an optional object cannot abort the
    whole transaction.
    """
    handler = ("\nEXCEPTION WHEN OTHERS THEN\n  RAISE NOTICE '%s (%%)', SQLERRM;"
               % soft_notice.replace("'", "''")) if soft_notice else ''
    return ("DO $$\nBEGIN\n  IF NOT EXISTS (%s) THEN\n    %s;\n  END IF;%s\nEND\n$$;"
            % (check_sql, body.replace('\n', '\n    '), handler))


def build(cur):
    out = []
    add = out.append
    server = q(cur, "SELECT current_setting('server_version')")[0][0]

    # -- sequences behind serial columns (identity sequences are created by their table)
    seqs = q(cur, """
        SELECT c.relname, format_type(s.seqtypid, NULL), s.seqstart, s.seqincrement,
               s.seqmin, s.seqmax, s.seqcache, s.seqcycle
        FROM pg_sequence s
        JOIN pg_class c ON c.oid = s.seqrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND NOT EXISTS (SELECT 1 FROM pg_depend d
                          WHERE d.objid = c.oid AND d.deptype = 'i')
        ORDER BY c.relname""")
    if seqs:
        add('-- Sequences (serial columns)')
        for name, typ, start, inc, mn, mx, cache, cycle in seqs:
            add('CREATE SEQUENCE IF NOT EXISTS public.%s AS %s START %d INCREMENT %d '
                'MINVALUE %d MAXVALUE %d CACHE %d%s;'
                % (ident(name), typ, start, inc, mn, mx, cache, ' CYCLE' if cycle else ''))
        add('')

    tables = q(cur, """
        SELECT c.oid, c.relname, c.relrowsecurity, c.relforcerowsecurity
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
        ORDER BY c.relname""")
    add('-- Tables, with their primary key / unique / check constraints')
    for oid, name, _rls, _force in tables:
        cols = q(cur, """
            SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
                   pg_get_expr(d.adbin, d.adrelid), a.attidentity, a.attgenerated
            FROM pg_attribute a
            LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
            WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY a.attnum""", (oid,))
        lines = []
        for col, typ, notnull, default, identity, generated in cols:
            line = '    %s %s' % (ident(col), typ)
            if generated == 's':
                line += ' GENERATED ALWAYS AS (%s) STORED' % default
            elif identity in ('a', 'd'):
                line += ' GENERATED %s AS IDENTITY' % ('ALWAYS' if identity == 'a' else 'BY DEFAULT')
            elif default is not None:
                line += ' DEFAULT %s' % default
            if notnull:
                line += ' NOT NULL'
            lines.append(line)
        for conname, condef in q(cur, """
                SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
                WHERE conrelid = %s AND contype IN ('p', 'u', 'c', 'x')
                ORDER BY CASE contype WHEN 'p' THEN 0 ELSE 1 END, conname""", (oid,)):
            lines.append('    CONSTRAINT %s %s' % (ident(conname), condef))
        add('CREATE TABLE IF NOT EXISTS public.%s (\n%s\n);' % (ident(name), ',\n'.join(lines)))
    add('')

    owned = q(cur, """
        SELECT s.relname, t.relname, a.attname
        FROM pg_depend d
        JOIN pg_class s ON s.oid = d.objid AND s.relkind = 'S'
        JOIN pg_class t ON t.oid = d.refobjid
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid
        JOIN pg_namespace n ON n.oid = s.relnamespace
        WHERE n.nspname = 'public' AND d.deptype = 'a'
        ORDER BY 1""")
    if owned:
        add('-- Serial sequences belong to their columns')
        for seq, table, col in owned:
            add('ALTER SEQUENCE public.%s OWNED BY public.%s.%s;' % (ident(seq), ident(table), ident(col)))
        add('')

    fks = q(cur, """
        SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid)
        FROM pg_constraint WHERE connamespace = 'public'::regnamespace AND contype = 'f'
        ORDER BY 1, 2""")
    if fks:
        add('-- Foreign keys')
        for table, conname, condef in fks:
            add(guarded("SELECT 1 FROM pg_constraint WHERE conname = '%s' "
                        "AND connamespace = 'public'::regnamespace" % conname,
                        'ALTER TABLE %s ADD CONSTRAINT %s %s' % (table, ident(conname), condef)))
        add('')

    idx = q(cur, """
        SELECT pg_get_indexdef(i.indexrelid)
        FROM pg_index i
        JOIN pg_class t ON t.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'public'
          AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = i.indexrelid)
        ORDER BY 1""")
    if idx:
        add('-- Indexes (other than those behind constraints)')
        for (d,) in idx:
            d = d.replace('CREATE UNIQUE INDEX ', 'CREATE UNIQUE INDEX IF NOT EXISTS ', 1)
            d = d.replace('CREATE INDEX ', 'CREATE INDEX IF NOT EXISTS ', 1)
            add(d + ';')
        add('')

    add('-- Row level security. Every table has it ON, with NO policies: the anon and')
    add('-- authenticated roles (the public Supabase keys) see nothing, while the backend')
    add('-- connects as the table owner and is unaffected.')
    for _oid, name, rls, force in tables:
        if rls:
            add('ALTER TABLE public.%s ENABLE ROW LEVEL SECURITY;' % ident(name))
        if force:
            add('ALTER TABLE public.%s FORCE ROW LEVEL SECURITY;' % ident(name))
    add('')

    pols = q(cur, """
        SELECT tablename, policyname, permissive, roles::text[], cmd, qual, with_check
        FROM pg_policies WHERE schemaname = 'public' ORDER BY 1, 2""")
    if pols:
        add('-- Policies')
        for table, pol, permissive, roles, cmd, qual, check in pols:
            body = 'CREATE POLICY %s ON public.%s AS %s FOR %s TO %s' % (
                ident(pol), ident(table), permissive, cmd, ', '.join(roles))
            if qual:
                body += ' USING (%s)' % qual
            if check:
                body += ' WITH CHECK (%s)' % check
            add(guarded("SELECT 1 FROM pg_policies WHERE schemaname = 'public' AND tablename = '%s' "
                        "AND policyname = '%s'" % (table, pol), body))
        add('')

    funcs = q(cur, """
        SELECT p.proname, pg_get_functiondef(p.oid)
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.prokind = 'f'
        ORDER BY 1""")
    if funcs:
        add('-- Functions')
        for _name, d in funcs:
            add(d.strip() + ';\n')

    views = q(cur, """
        SELECT c.relname, c.reloptions, pg_get_viewdef(c.oid, true)
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'v'
        ORDER BY 1""")
    if views:
        add('-- Views. KEEP security_invoker: without it a Supabase view runs as its owner,')
        add('-- bypasses RLS, and anyone holding the public anon key can read it.')
        for name, opts, d in views:
            with_ = ' WITH (%s)' % ', '.join(opts) if opts else ''
            add('CREATE OR REPLACE VIEW public.%s%s AS\n%s;' % (ident(name), with_, d.rstrip().rstrip(';')))
        add('')

    trigs = q(cur, """
        SELECT n.nspname, c.relname, t.tgname, pg_get_triggerdef(t.oid, true)
        FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE NOT t.tgisinternal
          AND (n.nspname = 'public' OR (n.nspname = 'auth' AND c.relname = 'users'))
        ORDER BY 1, 2, 3""")
    if trigs:
        add('-- Triggers (incl. the signup trigger on auth.users)')
        for schema, table, name, d in trigs:
            add('DROP TRIGGER IF EXISTS %s ON %s.%s;' % (ident(name), schema, ident(table)))
            add(d + ';')
        add('')

    evts = q(cur, """
        SELECT e.evtname, e.evtevent, e.evttags, e.evtenabled, p.proname
        FROM pg_event_trigger e
        JOIN pg_proc p ON p.oid = e.evtfoid
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
        ORDER BY 1""")
    if evts:
        add('-- Custom event triggers (Supabase\'s own built-in ones exist in every project)')
        for name, event, tags, enabled, func in evts:
            when = (" WHEN TAG IN (%s)" % ', '.join("'%s'" % t for t in tags)) if tags else ''
            add(guarded("SELECT 1 FROM pg_event_trigger WHERE evtname = '%s'" % name,
                        'CREATE EVENT TRIGGER %s ON %s%s EXECUTE FUNCTION public.%s()'
                        % (ident(name), event, when, ident(func)),
                        soft_notice='%s not created; every table above already has RLS on, '
                                    'and new tables need it enabled by hand (or turn on '
                                    'automatic RLS in the dashboard)' % name))
            if enabled == 'D':
                add('ALTER EVENT TRIGGER %s DISABLE;' % ident(name))
        add('')

    exts = q(cur, """SELECT e.extname, n.nspname FROM pg_extension e
                     JOIN pg_namespace n ON n.oid = e.extnamespace ORDER BY 1""")
    header = [
        '-- PixelFlip base schema: a snapshot of PRODUCTION\'s public schema (structure only,',
        '-- no rows). Generated %s by tools/dump_schema.py from PostgreSQL %s.'
        % (datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'), server),
        '--',
        '-- USE: create a NEW, EMPTY Supabase project for development, open its SQL editor,',
        '-- paste this whole file and run it. It is one transaction (all or nothing) and is',
        '-- safe to re-run. Do not run it against production.',
        '--',
        '-- Extensions present in production (a new Supabase project already has these):',
        '--   ' + ', '.join('%s (%s)' % (e, s) for e, s in exts),
        '-- Grants are not dumped: Supabase\'s default privileges give new public objects',
        '-- the same anon / authenticated / service_role grants production has.',
        '--',
        '-- Regenerate after any schema change:  .venv\\Scripts\\python.exe tools\\dump_schema.py',
        '',
        'BEGIN;',
        '',
    ]
    return '\n'.join(header + out + ['COMMIT;', ''])


def main(argv=None):
    ap = argparse.ArgumentParser(description='Snapshot the production public schema (no rows).')
    ap.add_argument('--env', default=os.path.join(REPO, '.env'))
    ap.add_argument('--stdout', action='store_true')
    args = ap.parse_args(argv)

    import psycopg2
    from dotenv import dotenv_values
    url = (dotenv_values(args.env).get('DATABASE_URL') or '').strip()
    if not url:
        sys.exit('no DATABASE_URL in %s' % args.env)
    conn = psycopg2.connect(url, connect_timeout=15)
    try:
        conn.set_session(readonly=True)
        cur = conn.cursor()
        cur.execute('SHOW transaction_read_only')
        if cur.fetchone()[0] != 'on':
            sys.exit('refusing to continue: the session is not read-only')
        cur.execute("SET statement_timeout = '20s'")
        # As pg_dump does: with only pg_catalog on the path, every definition the
        # server prints (defaults, views, triggers) comes out schema-qualified,
        # so the file does not depend on the search_path of whoever applies it.
        cur.execute('SET search_path = pg_catalog')
        sql = build(cur)
        conn.rollback()
    finally:
        conn.close()
    if args.stdout:
        sys.stdout.write(sql)
        return 0
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8', newline='\n') as f:
        f.write(sql)
    print('wrote %s (%d lines)' % (os.path.relpath(OUT, REPO), sql.count('\n')))
    return 0


if __name__ == '__main__':
    sys.exit(main())
