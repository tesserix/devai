"""Generic schema-aware mock-data seeder for live previews.

When a preview has an ephemeral Postgres, the backend migrates its schema on
boot — but the tables start empty, so the UI shows nothing. This seeder runs
as a sidecar: it waits for the backend to create tables, then introspects the
live schema and inserts realistic fake rows so end-to-end flows return data.

Design goals (the "works for any repo" property):
  * No per-repo knowledge — pure information_schema introspection.
  * FK-tolerant — multiple passes so parent rows land before children; a row
    that still violates a constraint is skipped, never fatal.
  * Idempotent — tables that already have rows are left alone (safe re-run).
  * Dependency-light — psycopg2 + faker installed at sidecar start; falls back
    to plain values if faker is unavailable.

The script text is kept here (version-controlled + unit-testable via compile())
and shipped to the sidecar in a ConfigMap by build_fullstack_preview_manifests.
"""

from __future__ import annotations

# The seeder runs inside the preview pod against localhost:5432. Connection +
# row count come from env so the manifest can tune them without editing this.
SEEDER_SCRIPT = r'''
import os, time, random, datetime, uuid

DSN = os.environ.get("SEED_DSN", "postgresql://preview:preview@localhost:5432/app")
ROWS = int(os.environ.get("SEED_ROWS", "12"))
WAIT_SECONDS = int(os.environ.get("SEED_WAIT_SECONDS", "240"))
# Bookkeeping tables we never seed.
SKIP = {"alembic_version", "django_migrations", "schema_migrations", "_prisma_migrations",
        "knex_migrations", "knex_migrations_lock", "goose_db_version", "flyway_schema_history"}

try:
    import psycopg2  # type: ignore
except Exception:
    import subprocess, sys
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "psycopg2-binary"], check=False)
    import psycopg2  # type: ignore

try:
    from faker import Faker  # type: ignore
    fake = Faker()
except Exception:
    fake = None


def _connect():
    last = None
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        try:
            c = psycopg2.connect(DSN); c.autocommit = True; return c
        except Exception as e:
            last = e; time.sleep(3)
    raise SystemExit(f"[seed] could not connect to DB: {last}")


def _wait_for_tables(conn):
    # Give the backend time to run its migrations and create the schema.
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        with conn.cursor() as cur:
            cur.execute("""SELECT count(*) FROM information_schema.tables
                           WHERE table_schema='public' AND table_type='BASE TABLE'""")
            if (cur.fetchone()[0] or 0) > len(SKIP):
                return True
        time.sleep(3)
    return False


def _tables(conn):
    with conn.cursor() as cur:
        cur.execute("""SELECT table_name FROM information_schema.tables
                       WHERE table_schema='public' AND table_type='BASE TABLE'""")
        return [r[0] for r in cur.fetchall() if r[0] not in SKIP]


def _columns(conn, table):
    with conn.cursor() as cur:
        cur.execute("""SELECT column_name, data_type, is_nullable, column_default,
                              is_identity, character_maximum_length
                       FROM information_schema.columns
                       WHERE table_schema='public' AND table_name=%s
                       ORDER BY ordinal_position""", (table,))
        return cur.fetchall()


def _is_empty(conn, table):
    with conn.cursor() as cur:
        cur.execute(f'SELECT 1 FROM "{table}" LIMIT 1')
        return cur.fetchone() is None


def _value(dtype, maxlen):
    d = (dtype or "").lower()
    if "int" in d or d in ("numeric", "real", "double precision", "decimal"):
        return random.randint(1, 9999) if "int" in d else round(random.uniform(1, 9999), 2)
    if "bool" in d:
        return random.choice([True, False])
    if "uuid" in d:
        return str(uuid.uuid4())
    if "timestamp" in d or d == "date":
        return datetime.datetime.utcnow()
    if "json" in d:
        return "{}"
    # text-ish
    if fake is not None:
        v = fake.sentence(nb_words=4)
    else:
        v = "sample-" + uuid.uuid4().hex[:8]
    if maxlen:
        v = v[: int(maxlen)]
    return v


def _insert_row(conn, table, cols):
    names, vals = [], []
    for (name, dtype, nullable, default, identity, maxlen) in cols:
        # Let the DB fill identities, serials, and defaulted columns.
        if identity == "YES" or (default is not None) or (isinstance(default, str) and "nextval" in default):
            continue
        if nullable == "YES" and random.random() < 0.3:
            continue  # leave some nullables empty for realism
        names.append(f'"{name}"'); vals.append(_value(dtype, maxlen))
    if not names:
        return False
    ph = ", ".join(["%s"] * len(vals))
    with conn.cursor() as cur:
        cur.execute(f'INSERT INTO "{table}" ({", ".join(names)}) VALUES ({ph})', vals)
    return True


def main():
    conn = _connect()
    if not _wait_for_tables(conn):
        print("[seed] no tables appeared — backend may not use a DB; nothing to seed"); return
    tables = _tables(conn)
    cols = {t: _columns(conn, t) for t in tables}
    pending = [t for t in tables if _is_empty(conn, t)]
    print(f"[seed] {len(pending)}/{len(tables)} empty tables to seed (rows={ROWS})")
    # Multiple passes so FK parents get rows before children; a row that still
    # violates a constraint is skipped (savepoint rollback), never fatal.
    for _pass in range(4):
        if not pending:
            break
        still = []
        for t in pending:
            ok = 0
            for _ in range(ROWS):
                try:
                    with conn.cursor() as cur:
                        cur.execute("SAVEPOINT s")
                    if _insert_row(conn, t, cols[t]):
                        ok += 1
                except Exception:
                    with conn.cursor() as cur:
                        cur.execute("ROLLBACK TO SAVEPOINT s")
            if ok == 0 and _is_empty(conn, t):
                still.append(t)  # likely FK-blocked; retry next pass
            else:
                print(f"[seed]   {t}: +{ok}")
        if len(still) == len(pending):
            break  # no progress — stop
        pending = still
    print("[seed] done")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(e)
    except Exception as e:  # never crash the pod on seed failure
        print(f"[seed] non-fatal error: {e}")
'''


def seed_configmap(name: str, namespace: str, labels: dict) -> dict:
    """A ConfigMap carrying the seeder script for a preview pod."""
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": f"{name}-seed", "namespace": namespace, "labels": labels},
        "data": {"seed.py": SEEDER_SCRIPT},
    }


def seed_sidecar(cm_name: str, *, rows: int = 12) -> dict:
    """A sidecar container that runs the seeder against the in-pod Postgres."""
    return {
        "name": "db-seeder",
        "image": "python:3.12-slim",
        "command": [
            "sh",
            "-lc",
            "pip install -q psycopg2-binary faker 2>/dev/null; python /seed/seed.py",
        ],
        "env": [
            {"name": "SEED_DSN", "value": "postgresql://preview:preview@localhost:5432/app"},
            {"name": "SEED_ROWS", "value": str(rows)},
        ],
        "volumeMounts": [{"name": "seed-script", "mountPath": "/seed"}],
        "resources": {"requests": {"memory": "128Mi"}, "limits": {"memory": "512Mi"}},
    }


__all__ = ["SEEDER_SCRIPT", "seed_configmap", "seed_sidecar"]
