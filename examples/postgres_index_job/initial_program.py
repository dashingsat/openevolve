"""
Seed program for evolving Postgres index candidates with OpenEvolve + HypoPG on JOB workload.
"""

# EVOLVE-BLOCK-START
#
# Compact index spec format (token-light, correctness-first):
# - t: table name (string)
# - k: key columns in order (list[str], max 3)
# - m: index method (optional; defaults to "btree"), e.g. "btree", "gin", "gist", "brin", "hash", "spgist"
# - n: short note (string)
#
# Example:
# INDEX_CANDIDATES = [
#     {"t": "title", "k": ["production_year", "id"], "n": "range + join (composite)", "m": "btree"},
# ]
INDEX_CANDIDATES = []
# EVOLVE-BLOCK-END
