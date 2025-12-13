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
import hashlib
import math
import os
import re
from pathlib import Path
from typing import Dict, List, Sequence, Tuple, Any, Optional, Set, Iterable

import psycopg

ROOT = Path(__file__).resolve().parent
JOB_DATA_DIR = ROOT / "job_data"
PG_CONN_ENV = "PG_CONN_STR"
STATS_SUMMARY: str = ""
SCHEMA_SUMMARY: str = ""
WORKLOAD_DIGEST: str = ""
STATS_COLLECTED: bool = False
SCHEMA_COLLECTED: bool = False

# Phase 2: evaluator-local caches (per process)
SCHEMA_COLS_CACHE: Optional[Dict[str, Set[str]]] = None
BASELINE_CACHED: bool = False
BASELINE_COST_CACHE: float = float("inf")
BASELINE_DETAILS_CACHE: Dict[str, float] = {}

ALLOWED_INDEX_METHODS: Set[str] = {"btree", "hash", "gin", "gist", "brin", "spgist"}
MAX_INDEX_COLS: int = 3
MAX_INDEX_CANDIDATES: int = 20

# Subset of queries to optimize
QUERY_NAMES = [
    "1a.sql", "2a.sql", "3a.sql", "4a.sql", 
    "5a.sql", "6a.sql", "10a.sql", "16a.sql"
]

# Queries that grant a score bonus if significantly improved
CRITICAL_QUERIES = {"6a.sql", "16a.sql"}

_RE_JOIN_EDGE = re.compile(r"(\b\w+\b)\.(\b\w+\b)\s*=\s*(\b\w+\b)\.(\b\w+\b)", re.IGNORECASE)
_RE_FROM_JOIN = re.compile(r"\b(from|join)\s+([a-zA-Z_]\w*)\s*(?:as\s+)?([a-zA-Z_]\w*)?", re.IGNORECASE)
_RE_WHERE = re.compile(r"\bwhere\b([\s\S]*?)(?:\bgroup\s+by\b|\border\s+by\b|$)", re.IGNORECASE)
_RE_GROUP_BY = re.compile(r"\bgroup\s+by\b([\s\S]*?)(?:\border\s+by\b|$)", re.IGNORECASE)
_RE_ORDER_BY = re.compile(r"\border\s+by\b([\s\S]*?)$", re.IGNORECASE)
_RE_COLREF = re.compile(r"(\b\w+\b)\.(\b\w+\b)")
_RE_PREDICATE_COL = re.compile(r"(\b\w+\b)\.(\b\w+\b)\s*(=|<|>|<=|>=|<>|!=|like\b|ilike\b|in\b|between\b)", re.IGNORECASE)


def _dedupe_preserve_order(items: Iterable[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _alias_map(sql: str) -> Dict[str, str]:
    """
    Best-effort alias resolution.
    Returns mapping alias -> base table, and includes table -> table.
    """
    m: Dict[str, str] = {}
    for _, tbl, alias in _RE_FROM_JOIN.findall(sql):
        if tbl:
            m[tbl] = tbl
        if alias:
            m[alias] = tbl
    return m


def _extract_workload_facts(sql: str) -> Dict[str, Any]:
    alias_to_table = _alias_map(sql)

    # Tables
    tables = _dedupe_preserve_order(sorted({t for t in alias_to_table.values() if t}))

    # Join edges
    join_edges = []
    for a1, c1, a2, c2 in _RE_JOIN_EDGE.findall(sql):
        t1 = alias_to_table.get(a1, a1)
        t2 = alias_to_table.get(a2, a2)
        join_edges.append(f"{t1}.{c1} = {t2}.{c2}")
    join_edges = _dedupe_preserve_order(join_edges)

    # WHERE predicates (best-effort)
    where_cols = []
    where_ops = []
    m_where = _RE_WHERE.search(sql)
    where_part = m_where.group(1) if m_where else ""
    for a, c, op in _RE_PREDICATE_COL.findall(where_part):
        t = alias_to_table.get(a, a)
        where_cols.append(f"{t}.{c}")
        where_ops.append(f"{t}.{c} {op.upper()}")
    where_cols = _dedupe_preserve_order(where_cols)
    where_ops = _dedupe_preserve_order(where_ops)

    # GROUP BY / ORDER BY cols
    group_cols = []
    m_group = _RE_GROUP_BY.search(sql)
    group_part = m_group.group(1) if m_group else ""
    for a, c in _RE_COLREF.findall(group_part):
        t = alias_to_table.get(a, a)
        group_cols.append(f"{t}.{c}")
    group_cols = _dedupe_preserve_order(group_cols)

    order_cols = []
    m_order = _RE_ORDER_BY.search(sql)
    order_part = m_order.group(1) if m_order else ""
    for a, c in _RE_COLREF.findall(order_part):
        t = alias_to_table.get(a, a)
        order_cols.append(f"{t}.{c}")
    order_cols = _dedupe_preserve_order(order_cols)

    return {
        "tables": tables,
        "join_edges": join_edges,
        "where_cols": where_cols,
        "where_ops": where_ops,
        "group_cols": group_cols,
        "order_cols": order_cols,
        "alias_to_table": alias_to_table,
    }


def _build_workload_digest(queries: List[Tuple[str, str]]) -> Tuple[str, Set[str]]:
    lines: List[str] = []
    used_tables: Set[str] = set()

    for name, sql in queries:
        facts = _extract_workload_facts(sql)
        used_tables.update(facts["tables"])

        header = f"- {name}"
        if name in CRITICAL_QUERIES:
            header += " (CRITICAL: +20% bonus if improved >50%)"
        lines.append(header)

        if facts["tables"]:
            lines.append(f"  tables: {', '.join(facts['tables'])}")
        if facts["join_edges"]:
            lines.append(f"  joins: {', '.join(facts['join_edges'][:8])}" + (" ..." if len(facts["join_edges"]) > 8 else ""))
        if facts["where_ops"]:
            lines.append(f"  where: {', '.join(facts['where_ops'][:10])}" + (" ..." if len(facts["where_ops"]) > 10 else ""))
        if facts["group_cols"]:
            lines.append(f"  group_by: {', '.join(facts['group_cols'][:8])}" + (" ..." if len(facts["group_cols"]) > 8 else ""))
        if facts["order_cols"]:
            lines.append(f"  order_by: {', '.join(facts['order_cols'][:8])}" + (" ..." if len(facts["order_cols"]) > 8 else ""))

    return "\n".join(lines).strip(), used_tables


def _read_queries() -> List[Tuple[str, str]]:
    """Read query SQLs from the job_data directory and precompute workload digest."""
    global WORKLOAD_DIGEST
    queries: List[Tuple[str, str]] = []
    for name in QUERY_NAMES:
        p = JOB_DATA_DIR / name
        if p.exists():
            queries.append((name, p.read_text()))

    WORKLOAD_DIGEST, _ = _build_workload_digest(queries)
    return queries

QUERIES = _read_queries()

def _load_index_candidates(program_path: str) -> List[Dict[str, Any]]:
    """Import program file and extract INDEX_CANDIDATES."""
    spec = importlib.util.spec_from_file_location("candidate_program", program_path)
    if spec is None or spec.loader is None:
        return []
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    raw = getattr(module, "INDEX_CANDIDATES", [])
    normalized: List[Dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, dict):
            normalized.append(entry)
        elif isinstance(entry, str):
            # Backward-compat: raw DDL strings (discouraged in Phase 1 prompt)
            normalized.append({"ddl": entry.strip(), "note": ""})
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


def _load_public_schema_columns(conn: psycopg.Connection) -> Dict[str, Set[str]]:
    """
    Return {table_name -> set(column_names)} for public schema.
    """
    out: Dict[str, Set[str]] = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
            ORDER BY table_name, ordinal_position
            """
        )
        for tbl, col in cur.fetchall():
            out.setdefault(str(tbl), set()).add(str(col))
    return out


def _collect_schema_summary(conn: psycopg.Connection) -> None:
    """
    Build a token-light schema summary for tables referenced in the workload.
    """
    global SCHEMA_SUMMARY, SCHEMA_COLLECTED
    if SCHEMA_COLLECTED:
        return

    tables_used = _build_workload_digest(QUERIES)[1]
    schema_cols = _get_schema_cols(conn)

    lines: List[str] = []
    for tbl in sorted(tables_used):
        cols = sorted(schema_cols.get(tbl, set()))
        if not cols:
            continue
        lines.append(f"- {tbl}: {', '.join(cols)}")

    SCHEMA_SUMMARY = "\n".join(lines).strip()
    SCHEMA_COLLECTED = True


def _get_schema_cols(conn: psycopg.Connection) -> Dict[str, Set[str]]:
    """Get cached schema columns map, loading it once per process."""
    global SCHEMA_COLS_CACHE
    if SCHEMA_COLS_CACHE is None:
        SCHEMA_COLS_CACHE = _load_public_schema_columns(conn)
    return SCHEMA_COLS_CACHE


def _get_or_compute_baseline(
    cur: psycopg.Cursor, queries: List[Tuple[str, str]]
) -> Tuple[float, Dict[str, float]]:
    """
    Cache baseline costs once per worker process.
    Baseline is computed with hypopg_reset() applied (no hypothetical indexes).
    """
    global BASELINE_CACHED, BASELINE_COST_CACHE, BASELINE_DETAILS_CACHE
    if BASELINE_CACHED and BASELINE_DETAILS_CACHE:
        return BASELINE_COST_CACHE, BASELINE_DETAILS_CACHE

    # Ensure no hypo indexes are present before computing baseline.
    try:
        cur.execute("SELECT hypopg_reset();")
    except Exception:
        pass

    baseline_cost, baseline_details = _explain_details(cur, queries)
    BASELINE_COST_CACHE = baseline_cost
    BASELINE_DETAILS_CACHE = baseline_details
    BASELINE_CACHED = True
    return baseline_cost, baseline_details


def _candidate_to_key(entry: Dict[str, Any]) -> Optional[Tuple[str, str, Tuple[str, ...]]]:
    t = entry.get("t")
    k = entry.get("k")
    if not isinstance(t, str) or not t.strip():
        return None
    if not isinstance(k, list) or not k:
        return None
    cols = tuple(str(c).strip() for c in k if str(c).strip())
    if not cols:
        return None
    m = str(entry.get("m", "btree")).strip().lower()
    return (t.strip(), m, cols)


def _prune_prefix_redundancy(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    If a btree index exists on (a,b,...) drop a redundant btree index on (a).
    Best-effort, conservative: only applies when method is btree and no include/predicate fields exist.
    """
    keys = []
    for e in entries:
        key = _candidate_to_key(e)
        if not key:
            continue
        t, m, cols = key
        if m != "btree":
            continue
        if "i" in e or "p" in e:
            continue
        keys.append((t, cols))

    # Identify redundant prefixes per table
    redundant: Set[Tuple[str, Tuple[str, ...]]] = set()
    by_table: Dict[str, List[Tuple[str, ...]]] = {}
    for t, cols in keys:
        by_table.setdefault(t, []).append(cols)
    for t, col_lists in by_table.items():
        col_sets = set(col_lists)
        for cols in col_lists:
            if len(cols) == 1:
                for other in col_sets:
                    if len(other) > 1 and other[:1] == cols:
                        redundant.add((t, cols))
                        break

    pruned: List[Dict[str, Any]] = []
    for e in entries:
        key = _candidate_to_key(e)
        if not key:
            pruned.append(e)
            continue
        t, m, cols = key
        if m == "btree" and (t, cols) in redundant and "i" not in e and "p" not in e:
            continue
        pruned.append(e)
    return pruned


def _compile_candidates_to_ddls(
    candidates: List[Dict[str, Any]],
    schema_cols: Dict[str, Set[str]],
) -> Tuple[List[str], List[str], int]:
    """
    Compile compact index specs to CREATE INDEX statements.

    Returns:
      - ddls: list of CREATE INDEX statements (valid)
      - notes: list of notes aligned with ddls (for debugging)
      - dropped_count: number of candidates dropped due to validation/pruning
    """
    # Keep only up to MAX_INDEX_CANDIDATES to bound output/runtime.
    trimmed = candidates[:MAX_INDEX_CANDIDATES]

    # Remove exact duplicates by key (table, method, cols) while preserving order.
    deduped: List[Dict[str, Any]] = []
    seen_keys: Set[Tuple[str, str, Tuple[str, ...]]] = set()
    for e in trimmed:
        key = _candidate_to_key(e)
        if not key:
            deduped.append(e)  # keep for possible backward-compat "ddl"
            continue
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(e)

    # Prefer composites by pruning redundant prefixes (btree only).
    deduped = _prune_prefix_redundancy(deduped)

    ddls: List[str] = []
    notes: List[str] = []
    dropped = 0

    for entry in deduped:
        # Backward-compat direct DDL (discouraged by prompt).
        if "ddl" in entry and isinstance(entry.get("ddl"), str):
            ddl = str(entry["ddl"]).strip()
            if ddl:
                ddls.append(ddl)
                notes.append(str(entry.get("note", "")).strip())
            else:
                dropped += 1
            continue

        t = entry.get("t")
        k = entry.get("k")
        if not isinstance(t, str) or not t.strip():
            dropped += 1
            continue
        if not isinstance(k, list) or not k:
            dropped += 1
            continue

        table = t.strip()
        cols = [str(c).strip() for c in k if str(c).strip()]
        if not cols:
            dropped += 1
            continue
        if len(cols) > MAX_INDEX_COLS:
            dropped += 1
            continue

        method = str(entry.get("m", "btree")).strip().lower() or "btree"
        if method not in ALLOWED_INDEX_METHODS:
            dropped += 1
            continue

        valid_cols = schema_cols.get(table, set())
        if not valid_cols:
            dropped += 1
            continue
        if any(c not in valid_cols for c in cols):
            dropped += 1
            continue

        col_sql = ", ".join(cols)
        ddl = f"CREATE INDEX ON {table} USING {method} ({col_sql})"
        ddls.append(ddl)
        notes.append(str(entry.get("n", entry.get("note", ""))).strip())

    return ddls, notes, dropped


def _safe_ident(s: str) -> str:
    """Best-effort identifier sanitizer for generated index names."""
    s = re.sub(r"[^a-zA-Z0-9_]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "x"


def _make_index_name(table: str, method: str, cols: Sequence[str]) -> str:
    """
    Deterministically generate a unique-ish index name.
    Postgres identifier limit is 63 bytes, so we truncate and add a hash suffix.
    """
    base = f"idx_{_safe_ident(table)}_{_safe_ident('_'.join(cols))}_{_safe_ident(method)}"
    h = hashlib.sha1(f"{table}|{method}|{','.join(cols)}".encode("utf-8")).hexdigest()[:8]
    # leave room for "_" + hash
    prefix = base[: max(1, 63 - 1 - len(h))]
    return f"{prefix}_{h}".lower()


def render_index_sql(
    candidates: List[Dict[str, Any]],
    conn: psycopg.Connection,
    *,
    schema: str = "public",
    if_not_exists: bool = True,
) -> str:
    """
    Expand compact index specs into concrete CREATE INDEX statements (for manual use).

    This is intended for exporting the final "best" solution into a readable SQL file.
    It validates against the live DB schema to avoid producing invalid SQL.
    """
    schema_cols = _get_schema_cols(conn)

    # Keep bounded and apply the same dedupe/pruning logic as evaluation.
    trimmed = candidates[:MAX_INDEX_CANDIDATES]
    deduped: List[Dict[str, Any]] = []
    seen_keys: Set[Tuple[str, str, Tuple[str, ...]]] = set()
    for e in trimmed:
        key = _candidate_to_key(e)
        if key and key in seen_keys:
            continue
        if key:
            seen_keys.add(key)
        deduped.append(e)
    deduped = _prune_prefix_redundancy(deduped)

    lines: List[str] = []
    dropped = 0

    schema_sql = _safe_ident(schema)
    ine = " IF NOT EXISTS" if if_not_exists else ""

    for entry in deduped:
        # Phase 1: no raw DDL escape hatch in prompt, but handle gracefully if present.
        if "ddl" in entry and isinstance(entry.get("ddl"), str):
            note = str(entry.get("note", "")).strip()
            if note:
                lines.append(f"-- {note}")
            lines.append(str(entry["ddl"]).rstrip(";") + ";")
            continue

        t = entry.get("t")
        k = entry.get("k")
        if not isinstance(t, str) or not t.strip():
            dropped += 1
            continue
        if not isinstance(k, list) or not k:
            dropped += 1
            continue

        table = t.strip()
        cols = [str(c).strip() for c in k if str(c).strip()]
        if not cols or len(cols) > MAX_INDEX_COLS:
            dropped += 1
            continue

        method = str(entry.get("m", "btree")).strip().lower() or "btree"
        if method not in ALLOWED_INDEX_METHODS:
            dropped += 1
            continue

        valid_cols = schema_cols.get(table, set())
        if not valid_cols or any(c not in valid_cols for c in cols):
            dropped += 1
            continue

        name = _make_index_name(table, method, cols)
        note = str(entry.get("n", entry.get("note", ""))).strip()
        if note:
            lines.append(f"-- {note}")

        col_sql = ", ".join(cols)
        lines.append(
            f"CREATE INDEX{ine} {name} ON {schema_sql}.{table} USING {method} ({col_sql});"
        )

    if dropped:
        lines.insert(0, f"-- NOTE: {dropped} candidate(s) were dropped during export due to validation/pruning.")

    return "\n".join(lines).strip() + ("\n" if lines else "")


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

    # Keep token/runtime bounded by limiting candidate count.
    if len(candidates) > MAX_INDEX_CANDIDATES:
        candidates = candidates[:MAX_INDEX_CANDIDATES]

    try:
        with _get_conn() as conn:
            conn.execute("SET statement_timeout = 30000;") # 30s per query
            _collect_stats(conn)
            _collect_schema_summary(conn)
            schema_cols = _get_schema_cols(conn)

            ddls, ddl_notes, dropped = _compile_candidates_to_ddls(candidates, schema_cols)
            index_count = len(ddls)
            if dropped:
                print(f"Dropped {dropped} invalid/redundant candidates.")
            if ddls:
                print(f"Evaluating {len(ddls)} indexes...")
            
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

                # Baseline (cached per process)
                baseline_cost, baseline_details = _get_or_compute_baseline(cur, QUERIES)

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

                # ---- Phase 1 scoring (EXPLAIN-only, workload-wide) ----
                # Per-query speedup ratios
                ratios: List[float] = []
                regress_excesses: List[float] = []
                query_lines: List[str] = []
                for q, _ in QUERIES:
                    b = float(baseline_details.get(q, 1e9))
                    p = float(plan_details.get(q, 1e9))
                    r = b / max(p, 1e-9)
                    r = max(r, 1e-12)
                    ratios.append(r)

                    # Regression tolerance: allow up to 10% slower (p <= 1.1*b)
                    if b > 0 and p > 1.1 * b:
                        # excess regression beyond 10%
                        excess = (p / (1.1 * b)) - 1.0
                        regress_excesses.append(max(0.0, excess))

                    # Compact per-query summary line for prompt
                    query_lines.append(f"{q}: base={b:.1f} cand={p:.1f} x{r:.3f}")

                # Geometric mean of ratios
                if ratios:
                    geom_mean = math.exp(sum(math.log(x) for x in ratios) / len(ratios))
                else:
                    geom_mean = 0.0

                # Regression penalty (convex beyond 10%)
                # Strongly discourage big regressions without making small noise catastrophic.
                penalty_reg = 1.0
                for ex in regress_excesses:
                    penalty_reg *= (1.0 + 10.0 * (ex * ex))

                # Storage penalty: soft convex after 500MB
                storage_over_mb = max(storage_mb - 500.0, 0.0)
                storage_over_frac = storage_over_mb / 500.0
                penalty_storage = 1.0 + 2.0 * (storage_over_frac * storage_over_frac)

                # Simple operational penalty by index count
                penalty_count = 1.0 + (index_count * 0.005)
                penalty = penalty_reg * penalty_storage * penalty_count

                # Apply Critical Query Bonus
                bonus_multiplier = 1.0
                for q in CRITICAL_QUERIES:
                    b_cost = baseline_details.get(q, 0)
                    p_cost = plan_details.get(q, 0)
                    # If improved by > 50%
                    if b_cost > 0 and p_cost < (b_cost * 0.5):
                        bonus_multiplier += 0.2 # 20% bonus per critical query solved
                
                combined_score = (geom_mean / max(penalty, 1e-12)) * bonus_multiplier

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
        "penalty_reg": penalty_reg,
        "penalty_storage": penalty_storage,
        "penalty_count": penalty_count,
        "geom_mean_speedup": geom_mean,
        "stats_summary": STATS_SUMMARY,
        "schema_summary": SCHEMA_SUMMARY,
        "workload_digest": WORKLOAD_DIGEST,
        "query_summary": "\n".join(query_lines),
    }
