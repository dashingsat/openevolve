import os
from pathlib import Path

from openevolve.config import Config, DatabaseConfig, EvaluatorConfig, LLMModelConfig, PromptConfig


def load() -> Config:
    """
    Build an in-memory Config tuned for the Postgres/HypoPG smoke test.
    Edit here (Python) instead of YAML per repo preference.
    """
    cfg = Config()

    # LLM setup with optional provider override (env LLM_PROVIDER=grok to use x.ai)
    provider = os.environ.get("LLM_PROVIDER", "").lower()
    if provider == "grok":
        cfg.llm.models = [
            LLMModelConfig(
                name="grok-4-1-fast",
                api_base="https://api.x.ai/v1",
            )
        ]
    else:
        cfg.llm.models = [
            LLMModelConfig(
                name="gemini-3-pro-preview",
                api_base="https://generativelanguage.googleapis.com/v1beta/openai/",
                max_tokens=8192,
                timeout=180,  # Increase timeout for complex reasoning
            )
        ]
    cfg.llm.timeout = 180  # Global timeout
    cfg.llm.evaluator_models = cfg.llm.models.copy()

    # Prompt customizations (templates live next to this config)
    prompt_dir = Path(__file__).with_name("prompts")
    cfg.prompt = PromptConfig(
        template_dir=str(prompt_dir),
        system_message="index_system",
        include_artifacts=False,
        use_template_stochasticity=False,
        num_top_programs=2,
        num_diverse_programs=1,
    )

    # Database / MAP-Elites knobs for quick smoke runs
    cfg.database = DatabaseConfig(
        in_memory=True,
        population_size=50,
        archive_size=50,
        num_islands=4,
        feature_dimensions=["storage_mb", "index_count"],
        feature_bins={"storage_mb": 10, "index_count": 8},
        migration_interval=20,
        migration_rate=0.1,
        log_prompts=True,
    )

    # Evaluator timeouts tuned for JOB workload
    cfg.evaluator = EvaluatorConfig(
        timeout=300,  # Increased for multiple complex queries
        parallel_evaluations=1,
        cascade_evaluation=False,
        enable_artifacts=False,
    )

    cfg.max_iterations = 20  # More iterations for bigger scale
    cfg.diff_based_evolution = True
    cfg.log_dir = str(Path(__file__).parent / "output" / "logs")
    cfg.file_suffix = ".py"

    return cfg

