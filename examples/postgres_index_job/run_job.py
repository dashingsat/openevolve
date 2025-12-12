from pathlib import Path
from dotenv import load_dotenv

# Load .env from this example directory regardless of cwd
load_dotenv(dotenv_path=Path(__file__).with_name(".env"), override=False)

from openevolve.api import run_evolution
from config import load as load_config


def main() -> None:
    cfg = load_config()
    base_dir = Path(__file__).parent

    # Dynamic Workload Injection (Safe approach)
    # We read the queries here and append to System Prompt to avoid logging spam
    query_names = [
        "1a.sql", "2a.sql", "3a.sql", "4a.sql", 
        "5a.sql", "6a.sql", "10a.sql", "16a.sql"
    ]
    # Critical queries for labeling
    critical_queries = {"6a.sql", "16a.sql"}
    
    workload_text = []
    job_data_dir = base_dir / "job_data"
    
    for name in query_names:
        p = job_data_dir / name
        if p.exists():
            header = f"--- Query {name}"
            if name in critical_queries:
                header += " (CRITICAL: 1.2x Score Bonus if improved >50%)"
            header += " ---"
            workload_text.append(f"{header}\n{p.read_text()}\n")
            
    full_workload = "\n".join(workload_text)

    # Inject into System Prompt config
    system_prompt_path = base_dir / "prompts" / "index_system.txt"
    if system_prompt_path.exists():
        base_system_prompt = system_prompt_path.read_text()
        cfg.prompt.system_message = f"{base_system_prompt}\n\n## WORKLOAD\n{full_workload}"

    result = run_evolution(
        initial_program=base_dir / "initial_program.py",
        evaluator=base_dir / "evaluator.py",
        config=cfg,
        iterations=cfg.max_iterations,
        output_dir=base_dir / "openevolve_output",
        cleanup=False,  # keep artifacts for inspection
    )

    print("Best combined_score:", result.best_score)
    # Filter out huge fields like 'stats_summary' for cleaner output
    safe_metrics = {k: v for k, v in result.metrics.items() if k not in ["workload", "stats_summary"]}
    print("Best metrics:", safe_metrics)
    print("Output dir:", result.output_dir)


if __name__ == "__main__":
    main()
