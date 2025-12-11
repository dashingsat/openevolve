# Postgres HypoPG JOB Example

This example evolves hypothetical indexes for the Join Order Benchmark (JOB) workload using HypoPG.

## Files
- `job_data/` – Cloned JOB repository containing queries and schema.
- `evaluator.py` – Scores index sets via HypoPG cost on a subset of JOB queries.
- `config.py` – Python config.
- `prompts/` – Templates.
- `run_job.py` – Runner.
- `install_data.sh` – Script to download and load JOB data.
- `setup.sh` – Sets up the python environment using `uv`.

## Prereqs
1. **Tools**:
   - `uv` (for python package management): `curl -LsSf https://astral.sh/uv/install.sh | sh`
   - Postgres (v12+)
   - `wget` (for downloading data)

2. **Environment**:
   - `PG_CONN_STR`: Connection string to the JOB database.
   - `OPENAI_API_KEY`: For the LLM.

## Setup & Run

1. **Setup Environment**:
   ```bash
   # Creates .venv and installs dependencies using uv
   ./setup.sh
   ```

2. **Ingest Data**:
   Ensure your local Postgres is running.
   ```bash
   # Downloads JOB data and loads into 'job_db'
   ./install_data.sh
   ```

3. **Run Evolution**:
   ```bash
   export PG_CONN_STR="postgresql://user:pass@localhost:5432/job_db"
   export OPENAI_API_KEY=...

   # Run using the uv-managed venv
   ./.venv/bin/python run_job.py
   ```

## Workload
The evaluator currently targets a subset of queries: 1a, 2a, 3a, 4a, 5a, 6a, 10a, 16a.
You can modify `QUERY_NAMES` in `evaluator.py` to add more.
