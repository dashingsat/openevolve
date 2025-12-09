"""
Seed program for evolving Postgres index candidates with OpenEvolve + HypoPG.

The evaluator reads the `INDEX_CANDIDATES` list to simulate hypothetical indexes
against the target workload using HypoPG and scores them for plan cost and
storage footprint.

Each entry should be a dict with:
    - ddl: required Postgres CREATE INDEX statement compatible with HypoPG
    - note: optional short rationale
"""

# EVOLVE-BLOCK-START
INDEX_CANDIDATES = [
    # Example entry the model can follow:
    # {"ddl": "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_orders_status_created ON orders (status, created_at)", "note": "fast status/date filter"},
]
# EVOLVE-BLOCK-END

