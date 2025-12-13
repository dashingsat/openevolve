from pathlib import Path
from dotenv import load_dotenv

# Load .env from this example directory regardless of cwd
load_dotenv(dotenv_path=Path(__file__).with_name(".env"), override=False)

from openevolve.api import run_evolution
from config import load as load_config


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


if __name__ == "__main__":
    main()
