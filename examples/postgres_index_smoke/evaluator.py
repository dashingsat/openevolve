"""
HypoPG-backed evaluator for evolving Postgres index candidates.

Environment:
    PG_CONN_STR: libpq connection string, e.g.
        postgres://user:pass@localhost:5432/openevolve
Requires:
    - Postgres with the hypopg extension installed
    - psycopg (v3) installed in the active environment
"""

import importlib.util
import os
from pathlib import Path
from typing import Dict, List, Sequence

import psycopg

ROOT = Path(__file__).resolve().parent
SCHEMA_SQL = (ROOT / "schema.sql").read_text()
WORKLOAD_SQL = (ROOT / "workload.sql").read_text()
PG_CONN_ENV = "PG_CONN_STR"


def _load_index_candidates(program_path: str) -> List[Dict[str, str]]:
    """Import program file and extract INDEX_CANDIDATES."""
    spec = importlib.util.spec_from_file_location("candidate_program", program_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)  # type: ignore[arg-type]

    raw = getattr(module, "INDEX_CANDIDATES", [])
    normalized: List[Dict[str, str]] = []
    for entry in raw:
        if isinstance(entry, str):
            normalized.append({"ddl": entry.strip(), "note": ""})
        elif isinstance(entry, dict) and "ddl" in entry:
            ddl = str(entry["ddl"]).strip()
            note = str(entry.get("note", "")).strip()
            if ddl:
                normalized.append({"ddl": ddl, "note": note})
    return normalized


def _get_conn() -> psycopg.Connection:
    conn_str = os.environ.get(PG_CONN_ENV)
    if not conn_str:
        raise RuntimeError(f"Set {PG_CONN_ENV} to a valid Postgres connection string.")
    return psycopg.connect(conn_str, autocommit=False)


def _ensure_hypopg(cur: psycopg.Cursor) -> None:
    """Ensure hypopg exists and is usable in this session."""
    try:
        cur.execute("CREATE EXTENSION IF NOT EXISTS hypopg;")
    except Exception as exc:
        raise RuntimeError(f"hypopg extension unavailable: {exc}") from exc

    cur.execute("SELECT 1 FROM pg_extension WHERE extname='hypopg';")
    if cur.fetchone() is None:
        raise RuntimeError("hypopg extension not installed in this database.")

    # Sanity check the functions we rely on
    try:
        cur.execute("SELECT count(*) FROM hypopg_list_indexes;")
    except Exception as exc:
        raise RuntimeError(f"hypopg functions missing (hypopg_list_indexes): {exc}") from exc

    cur.execute("SELECT hypopg_reset();")


def _ensure_schema_and_data(conn: psycopg.Connection) -> None:
    """Create tables and seed a small dataset if empty."""
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
        cur.execute("SELECT COUNT(*) FROM customers;")
        count = cur.fetchone()[0]
        if count and count > 0:
            return

        # Seed deterministic synthetic data for quick plans
        cur.execute(
            """
            INSERT INTO customers (name, segment, region, created_at, is_active)
            SELECT
                'Customer ' || g,
                CASE WHEN g % 3 = 0 THEN 'enterprise'
                     WHEN g % 3 = 1 THEN 'midmarket'
                     ELSE 'smb' END,
                CASE WHEN g % 4 = 0 THEN 'na'
                     WHEN g % 4 = 1 THEN 'emea'
                     WHEN g % 4 = 2 THEN 'latam'
                     ELSE 'apac' END,
                NOW() - (g % 730) * INTERVAL '1 day',
                TRUE
            FROM generate_series(1, 6000) AS g;
            """
        )

        cur.execute(
            """
            INSERT INTO orders (customer_id, status, created_at, total_cents, shipping_postcode, sales_rep_id)
            SELECT
                1 + (g % 6000),
                CASE WHEN g % 4 = 0 THEN 'shipped'
                     WHEN g % 4 = 1 THEN 'processing'
                     WHEN g % 4 = 2 THEN 'pending'
                     ELSE 'cancelled' END,
                NOW() - (g % 60) * INTERVAL '1 day',
                10000 + (g % 5000),
                LPAD((10000 + g)::text, 5, '0'),
                100 + (g % 50)
            FROM generate_series(1, 300000) AS g;
            """
        )

        cur.execute(
            """
            INSERT INTO order_items (order_id, product_id, quantity, price_cents, discount_cents)
            SELECT
                1 + (g % 300000),
                10 + (g % 5000),
                1 + (g % 5),
                500 + (g % 20000),
                CASE WHEN g % 7 = 0 THEN 50 ELSE 0 END
            FROM generate_series(1, 900000) AS g;
            """
        )
    conn.commit()


def _explain_cost(cur: psycopg.Cursor, query: str) -> float:
    cur.execute("EXPLAIN (FORMAT JSON) " + query)
    plan_json = cur.fetchone()[0][0]
    return float(plan_json["Plan"]["Total Cost"])


def _apply_hypo_indexes(cur: psycopg.Cursor, ddls: Sequence[str]) -> List[int]:
    oids: List[int] = []
    for ddl in ddls:
        cur.execute("SELECT indexrelid FROM hypopg_create_index(%s);", (ddl,))
        oid = cur.fetchone()[0]
        oids.append(int(oid))
    return oids


def _hypo_storage_bytes(cur: psycopg.Cursor) -> int:
    try:
        cur.execute("SELECT COALESCE(SUM(hypopg_relation_size(indexrelid)), 0) FROM hypopg_list_indexes;")
        return int(cur.fetchone()[0])
    except Exception:
        # If storage lookup fails, treat as zero to avoid hard failure mid-run
        return 0


def evaluate(program_path: str) -> Dict[str, float]:
    """Entry point for OpenEvolve evaluator."""
    try:
        candidates = _load_index_candidates(program_path)
    except Exception as exc:  # pragma: no cover - defensive for LLM edits
        return {
            "combined_score": 0.0,
            "cost": float("inf"),
            "storage_mb": 0.0,
            "index_count": 0,
            "message": f"failed to load candidates: {exc}",
        }

    ddls = [c["ddl"] for c in candidates if c.get("ddl")]
    index_count = len(ddls)

    try:
        with _get_conn() as conn:
            conn.execute("SET statement_timeout = 5000;")
            _ensure_schema_and_data(conn)
            with conn.cursor() as cur:
                try:
                    _ensure_hypopg(cur)
                except Exception as exc:
                    conn.rollback()
                    return {
                        "combined_score": 0.0,
                        "cost": float("inf"),
                        "storage_mb": 0.0,
                        "index_count": 0,
                        "message": f"hypopg not available: {exc}",
                    }
                baseline_cost = _explain_cost(cur, WORKLOAD_SQL)

                try:
                    if ddls:
                        _apply_hypo_indexes(cur, ddls)
                except Exception as exc:
                    conn.rollback()
                    return {
                        "combined_score": 0.0,
                        "cost": float("inf"),
                        "storage_mb": 0.0,
                        "index_count": 0,
                        "message": f"invalid hypopg index: {exc}",
                    }

                plan_cost = _explain_cost(cur, WORKLOAD_SQL)
                storage_bytes = _hypo_storage_bytes(cur) if ddls else 0
                storage_mb = storage_bytes / (1024 * 1024)

                # Preference: better than baseline, with softer penalty and a 10MB grace band
                storage_overhead = max(storage_mb - 10.0, 0.0)
                penalty = 1.0 + (storage_overhead * 0.001) + (index_count * 0.01)
                combined_score = max(
                    0.0, baseline_cost / max(plan_cost * penalty, 1e-6)
                )

                cur.execute("SELECT hypopg_reset();")

            conn.commit()
    except Exception as exc:  # pragma: no cover - defensive for DB errors
        return {
            "combined_score": 0.0,
            "cost": float("inf"),
            "storage_mb": 0.0,
            "index_count": 0,
            "message": f"database error: {exc}",
        }

    return {
        "combined_score": combined_score,
        "cost": plan_cost,
        "baseline_cost": baseline_cost,
        "storage_mb": storage_mb,
        "index_count": index_count,
        "penalty": penalty,
    }

