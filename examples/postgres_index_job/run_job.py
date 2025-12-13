from pathlib import Path
from dotenv import load_dotenv

# Load .env from this example directory regardless of cwd
load_dotenv(dotenv_path=Path(__file__).with_name(".env"), override=False)

from openevolve.api import run_evolution
from config import load as load_config
from evaluator import _get_conn, _load_index_candidates, render_index_sql


def main() -> None:
    cfg = load_config()
    base_dir = Path(__file__).parent

    result = run_evolution(
        initial_program=base_dir / "initial_program.py",
        evaluator=base_dir / "evaluator.py",
        config=cfg,
        iterations=cfg.max_iterations,
        output_dir=base_dir / "openevolve_output",
        cleanup=False,  # keep artifacts for inspection
    )

    print("Best combined_score:", result.best_score)
    # Filter out huge fields for cleaner output
    safe_metrics = {
        k: v
        for k, v in result.metrics.items()
        if k
        not in [
            "workload",
            "stats_summary",
            "schema_summary",
            "workload_digest",
            "query_summary",
        ]
    }
    print("Best metrics:", safe_metrics)
    print("Output dir:", result.output_dir)

    # Export a concrete SQL file for manual index creation from the best program.
    try:
        out_dir = Path(result.output_dir) if result.output_dir else (base_dir / "openevolve_output")
        best_program_path = out_dir / "best" / "best_program.py"
        best_sql_path = out_dir / "best" / "best_indexes.sql"

        candidates = _load_index_candidates(str(best_program_path))
        with _get_conn() as conn:
            # Schema defaults to PG_SCHEMA env var (or "public") inside evaluator helpers.
            sql = render_index_sql(candidates, conn, schema=None, if_not_exists=True)

        best_sql_path.write_text(
            f"-- Generated from {best_program_path.name}\n"
            f"-- Best combined_score: {result.best_score}\n\n"
            f"{sql}",
            encoding="utf-8",
        )
        print("Best indexes SQL:", str(best_sql_path))
    except Exception as exc:
        print("Warning: failed to export best_indexes.sql:", exc)


if __name__ == "__main__":
    main()
