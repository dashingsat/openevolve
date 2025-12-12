"""
HypoPG-backed evaluator for evolving Postgres index candidates on JOB workload.

Environment:
    PG_CONN_STR: libpq connection string, e.g.
        postgres://user:pass@localhost:5432/job
Requires:
    - Postgres with the hypopg extension installed
    - JOB data loaded in the target database
    - psycopg (v3) installed
"""

import importlib.util
import os
import glob
from pathlib import Path
from typing import Dict, List, Sequence, Tuple, Any

import psycopg

ROOT = Path(__file__).resolve().parent
JOB_DATA_DIR = ROOT / "job_data"
PG_CONN_ENV = "PG_CONN_STR"
STATS_SUMMARY: str = ""
WORKLOAD_SUMMARY: str = ""
STATS_COLLECTED: bool = False

# Subset of queries to optimize
QUERY_NAMES = [
    "1a.sql", "2a.sql", "3a.sql", "4a.sql", 
    "5a.sql", "6a.sql", "10a.sql", "16a.sql"
]

# Queries that grant a score bonus if significantly improved
CRITICAL_QUERIES = {"6a.sql", "16a.sql"}

def _read_queries() -> List[Tuple[str, str]]:
    """Read query SQLs from the job_data directory."""
    global WORKLOAD_SUMMARY
    queries = []
    summary_parts = []
    for name in QUERY_NAMES:
        p = JOB_DATA_DIR / name
        if p.exists():
            sql = p.read_text()
            queries.append((name, sql))
            
            header = f"--- Query {name}"
            if name in CRITICAL_QUERIES:
                header += " (CRITICAL: 1.2x Score Bonus if improved >50%)"
            header += " ---"
            
            summary_parts.append(f"{header}\n{sql}\n")
            
    WORKLOAD_SUMMARY = "\n".join(summary_parts)
    return queries

QUERIES = _read_queries()

def _load_index_candidates(program_path: str) -> List[Dict[str, str]]:
    """Import program file and extract INDEX_CANDIDATES."""
    spec = importlib.util.spec_from_file_location("candidate_program", program_path)
    if spec is None or spec.loader is None:
        return []
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

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

    # Sanity check
    try:
        cur.execute("SELECT count(*) FROM hypopg_list_indexes;")
    except Exception as exc:
        raise RuntimeError(f"hypopg functions missing (hypopg_list_indexes): {exc}") from exc

    cur.execute("SELECT hypopg_reset();")


def _format_common_vals(vals: List[str], freqs: List[float], max_items: int = 3) -> str:
    pairs = []
    for v, f in list(zip(vals, freqs))[:max_items]:
        pairs.append(f"{v}({f:.2f})")
    return ", ".join(pairs)


def _collect_stats(conn: psycopg.Connection) -> None:
    """Collect row counts and stats for prompt context."""
    global STATS_SUMMARY, STATS_COLLECTED

    if STATS_COLLECTED:
        return
    summaries: List[str] = []
    
    # Tables in JOB
    tables = [
        "aka_name", "aka_title", "cast_info", "char_name", "comp_cast_type",
        "company_name", "company_type", "complete_cast", "info_type", "keyword",
        "kind_type", "link_type", "movie_companies", "movie_info", "movie_info_idx",
        "movie_keyword", "movie_link", "name", "person_info", "role_type", "title"
    ]

    with conn.cursor() as cur:
        # Row counts
        placeholders = ",".join(["%s"] * len(tables))
        cur.execute(
            f"""
            SELECT relname, reltuples::bigint
            FROM pg_class
            WHERE relname IN ({placeholders})
            ORDER BY reltuples DESC
            """,
            tables
        )
        rows = cur.fetchall()
        row_counts = ", ".join(f"{r[0]}~{r[1]}" for r in rows)
        summaries.append(f"row_counts: {row_counts}")

        # Basic stats on a few columns (just example, can be expanded)
        # For brevity, we just rely on row counts in the prompt mostly.
        # Maybe add stats for 'kind_type.kind' etc.
        target_cols = [
            ("kind_type", "kind"),
            ("role_type", "role"),
            ("info_type", "info"),
        ]
        
        for tbl, col in target_cols:
             cur.execute(
                """
                SELECT n_distinct, null_frac,
                       most_common_vals, most_common_freqs
                FROM pg_stats
                WHERE schemaname = 'public' AND tablename = %s AND attname = %s
                """,
                (tbl, col),
            )
             res = cur.fetchone()
             if res:
                n_distinct, null_frac, mc_vals, mc_freqs = res
                common = ""
                if mc_vals and mc_freqs:
                    common = _format_common_vals(list(mc_vals), list(mc_freqs))
                summaries.append(f"{tbl}.{col}: ndist={n_distinct:.1f}, common={common}")

    STATS_SUMMARY = "; ".join(summaries)
    STATS_COLLECTED = True


def _explain_details(cur: psycopg.Cursor, queries: List[Tuple[str, str]]) -> Tuple[float, Dict[str, float]]:
    """Return total cost and per-query costs."""
    total_cost = 0.0
    details = {}
    for name, sql in queries:
        try:
            cur.execute("EXPLAIN (FORMAT JSON) " + sql)
            plan_json = cur.fetchone()[0][0]
            c = float(plan_json["Plan"]["Total Cost"])
            details[name] = c
            total_cost += c
        except Exception as e:
            # If a query fails (e.g. timeout or syntax), penalize heavily but don't crash
            print(f"Query {name} failed: {e}")
            penalty = 1e9
            details[name] = penalty
            total_cost += penalty
    return total_cost, details


def _apply_hypo_indexes(cur: psycopg.Cursor, ddls: Sequence[str]) -> List[int]:
    oids: List[int] = []
    for ddl in ddls:
        try:
            cur.execute("SELECT indexrelid FROM hypopg_create_index(%s);", (ddl,))
            res = cur.fetchone()
            if res:
                oids.append(int(res[0]))
        except Exception as e:
            # Log invalid index definitions
            print(f"Failed to create index {ddl}: {e}")
            pass
    return oids


def _hypo_storage_bytes(cur: psycopg.Cursor) -> int:
    try:
        cur.execute("SELECT COALESCE(SUM(hypopg_relation_size(indexrelid)), 0) FROM hypopg_list_indexes;")
        return int(cur.fetchone()[0])
    except Exception:
        return 0


def evaluate(program_path: str) -> Dict[str, float]:
    """Entry point for OpenEvolve evaluator."""
    try:
        candidates = _load_index_candidates(program_path)
    except Exception as exc:
        return {
            "combined_score": 0.0,
            "cost": float("inf"),
            "storage_mb": 0.0,
            "index_count": 0,
            "message": f"failed to load candidates: {exc}",
        }

    ddls = [c["ddl"] for c in candidates if c.get("ddl")]
    if not ddls and len(candidates) > 0:
        print(f"Warning: Candidates found but no DDLs extracted: {candidates}")
    elif ddls:
        print(f"Evaluating {len(ddls)} indexes...")
    
    index_count = len(ddls)

    try:
        with _get_conn() as conn:
            conn.execute("SET statement_timeout = 30000;") # 30s per query
            _collect_stats(conn)
            
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

                # Baseline
                baseline_cost, baseline_details = _explain_details(cur, QUERIES)

                # Apply Indexes
                try:
                    if ddls:
                        _apply_hypo_indexes(cur, ddls)
                except Exception:
                    pass

                # Plan with indexes
                plan_cost, plan_details = _explain_details(cur, QUERIES)
                
                storage_bytes = _hypo_storage_bytes(cur) if ddls else 0
                storage_mb = storage_bytes / (1024 * 1024)

                # Fitness logic: minimize cost, penalize storage > 100MB
                # We are more lenient with storage for JOB as it's a big dataset
                storage_overhead = max(storage_mb - 200.0, 0.0)
                
                # Penalty factor
                penalty = 1.0 + (storage_overhead * 0.0001) + (index_count * 0.005)
                
                # Score is improvement ratio
                raw_score = max(
                    0.0, baseline_cost / max(plan_cost * penalty, 1e-6)
                )

                # Apply Critical Query Bonus
                bonus_multiplier = 1.0
                for q in CRITICAL_QUERIES:
                    b_cost = baseline_details.get(q, 0)
                    p_cost = plan_details.get(q, 0)
                    # If improved by > 50%
                    if b_cost > 0 and p_cost < (b_cost * 0.5):
                        bonus_multiplier += 0.2 # 20% bonus per critical query solved
                
                combined_score = raw_score * bonus_multiplier

                cur.execute("SELECT hypopg_reset();")

            conn.commit()
    except Exception as exc:
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
        "stats_summary": STATS_SUMMARY,
        "workload": WORKLOAD_SUMMARY,
    }
