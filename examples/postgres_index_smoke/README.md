# Postgres HypoPG Smoke Test

This example evolves hypothetical indexes for a small 3-table workload using HypoPG. It stays lightweight so you can validate the pipeline before tackling larger DOJ-style benchmarks.

## Files
- `schema.sql` – customers/orders/order_items tables.
- `workload.sql` – representative join + filter query.
- `initial_program.py` – mutable `INDEX_CANDIDATES` list the LLM edits.
- `evaluator.py` – scores index sets via HypoPG cost and storage.
- `config.py` – Python config (no YAML) tuned for a short run.
- `prompts/` – system + user templates tailored to this workload.
- `run_smoke.py` – convenience runner for the example.
- `setup.sh` – orchestrates the modular installers below.
- `install_env.sh` – create/reuse `.venv` under this folder (uv).
- `install_requirements.sh` – install openevolve editable + example deps into `.venv`.
- `install_hypopg.sh` – run `CREATE EXTENSION hypopg` via `psql` using `PG_CONN_STR`.

## Prereqs
- Postgres with `hypopg` available (install via `brew install hypopg` if missing).
- Env var `PG_CONN_STR` pointing at the target database.
- LLM key (OpenAI-compatible) via `OPENAI_API_KEY`.
- Deps via `setup.sh` (uses `.venv` inside this folder) or run sub-steps:
  - `./install_env.sh`
  - `./install_requirements.sh`
  - `PG_CONN_STR=... ./install_hypopg.sh`

## Run
Full setup + run:
```bash
cd /Users/dashingsat/Documents/singularity/openevolve/examples/postgres_index_smoke
export PG_CONN_STR="postgresql://user:pass@localhost:5432/postgres"
./setup.sh
export OPENAI_API_KEY=...
./.venv/bin/python run_smoke.py
```

If you only need hypopg creation after the venv is ready:
```bash
cd /Users/dashingsat/Documents/singularity/openevolve/examples/postgres_index_smoke
export PG_CONN_STR="postgresql://user:pass@localhost:5432/postgres"
./install_hypopg.sh
```

The evaluator seeds a small dataset if the tables are empty, then uses HypoPG for index what-if scoring. Metrics returned: `combined_score`, `cost` (plan cost with hypo indexes), `baseline_cost`, `storage_mb`, `index_count`, `penalty`.

