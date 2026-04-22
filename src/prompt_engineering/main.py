from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

import typer
from prompt_engineering.config import PACKAGE_ROOT, VERSION_MAP, setup_logging
from prompt_engineering.util.guardrails import (
    merge_guardrails,
    sanitize_response,
)
from rich.console import Console

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="prompt_engineering_task",
    help="Data Quality Prompt Engineering Pipeline — run, optimize.",
    add_completion=False,
)
console = Console()

OUTPUTS_DIR = Path("outputs")

def _resolve_include_path(include: str) -> Path:
    raw = include.strip()
    if not raw:
        raise FileNotFoundError("--include: path is empty.")
    p = Path(raw).expanduser()
    if p.is_file():
        return p.resolve()

    raise FileNotFoundError(f"--include: file not found: {raw!r}")

def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)

def _build_client(model_config_name: Optional[str] = None):
    from prompt_engineering.client import LLMClient
    from prompt_engineering.config import AppConfig, load_llm_config

    config = AppConfig()
    name = (model_config_name or "").strip() or config.MODEL_NAME
    llm_config = load_llm_config(name)
    logger.info(
        "Using LLM config: %s — model=%s, api=%s, temperature=%s, max_tokens=%s",
        name,
        llm_config.model,
        llm_config.api,
        llm_config.temperature,
        llm_config.max_tokens,
    )
    return LLMClient(config, llm_config), config, llm_config

@app.command()
def run(
    prompt_version: str = typer.Option(
        "v1", "--prompt-version", "-p",
        help="Prompt version (v1, v2, v3).",
    ),
    dataset: Optional[str] = typer.Option(
        None, "--dataset", "-d", help="Path to dataset CSV.",
    ),
    output: Optional[str] = typer.Option(
        None, "--output", "-o",
        help="Path to response JSON (default: outputs/<version>/<version>_response.json).",
    ),
    golden: Optional[str] = typer.Option(
        None,
        "--golden",
        "-g",
        help="Path to golden_data.json (must contain row_errors; default: package data/golden_data.json).",
    ),
    include: Optional[str] = typer.Option(
        None, "--include",
        help=(
            "Full path, or shorthand <version>_response.json "
            "(resolves to outputs/<version>/<version>_response.json)."
        ),
    ),
    model: Optional[str] = typer.Option(
        None, "--model",
        help="LLM config name (uses config/<name>.yaml). Overrides MODEL_NAME from env.",
    ),
    evaluator: str = typer.Option(
        "llm-judge",
        "--evaluator",
        "-e",
        help=(
            "Evaluation backend: 'llm-judge' (default, uses eval/evaluate.md) "
            "or 'deepeval' (DeepEval metrics; requires `uv sync --extra deepeval`)."
        ),
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-V", help="Enable debug logging.",
    ),
) -> None:
    """Run a prompt version, save the response and score it against golden data."""
    setup_logging("DEBUG" if verbose else "INFO")
    if evaluator not in ("llm-judge", "deepeval"):
        console.print(
            f"[red]Unknown evaluator: {evaluator!r}. "
            "Use 'llm-judge' or 'deepeval'.[/red]"
        )
        raise typer.Exit(1)
    asyncio.run(
        _run(prompt_version, dataset, output, golden, include, model, evaluator)
    )

_ALLOWED_ERROR_KEYS = {"field", "value", "reason", "category", "confidence"}

def _sanitize_finding(entry: dict) -> dict:
    """Strip unexpected keys from a finding to prevent prompt injection."""
    clean: dict = {}
    if "row_index" in entry:
        clean["row_index"] = int(entry["row_index"])
    if "errors" in entry and isinstance(entry["errors"], list):
        clean["errors"] = [
            {k: str(v) for k, v in err.items() if k in _ALLOWED_ERROR_KEYS}
            for err in entry["errors"]
            if isinstance(err, dict)
        ]
    return clean

def _load_included_findings(path: str) -> list[dict]:
    """Load and sanitize prior LLM findings from a previous run's response JSON."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "row_errors" in data:
        raw = data["row_errors"]
        if not isinstance(raw, list):
            raw = []
    else:
        raw = data.get("verified_findings", [])
    if not raw:
        logger.warning(
            "No row_errors or verified_findings in %s — running without prior context.",
            path,
        )
        return []
    findings = [_sanitize_finding(f) for f in raw if isinstance(f, dict)]
    logger.info("Loaded %d row_errors entries from %s for --include", len(findings), path)
    return findings

def _write_eval_artifacts(
    run_dir: Path,
    prompt_version: str,
    report: "VersionReport",
    evaluator: "PromptEvaluator",
) -> None:
    """Write <version>_llm_eval.json"""
    llm_path = run_dir / f"{prompt_version}_llm_eval.json"
    g_meta = evaluator.golden_metadata()

    if report.judge_verdict is not None:
        llm_payload: dict = {
            "prompt_version": prompt_version,
            **g_meta,
            **report.judge_verdict.to_dict(),
        }
    else:
        llm_payload = {
            "prompt_version": prompt_version,
            **g_meta,
            "evaluation": "Judge was not run.",
        }

    llm_path.write_text(json.dumps(llm_payload, indent=2), encoding="utf-8")

async def _run(
    prompt_version: str,
    dataset_path: str | None,
    output_path: str | None,
    golden_path: str | None,
    include_path: str | None,
    model_config_name: Optional[str] = None,
    evaluator_name: str = "llm-judge",
) -> None:
    from prompt_engineering.eval.evaluation import PromptEvaluator

    resolved_include: str | None = None
    if include_path:
        try:
            resolved_include = str(_resolve_include_path(include_path))
        except FileNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
    prior_findings = (
        _load_included_findings(resolved_include) if resolved_include else None
    )

    client, _, llm_config = _build_client(model_config_name)
    logger.info("Using evaluator: %s", evaluator_name)
    try:
        evaluator = PromptEvaluator(
            client,
            dataset_path=dataset_path,
            golden_path=golden_path,
            evaluator=evaluator_name,  # type: ignore[arg-type]
        )
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        await client.close()
        raise typer.Exit(1) from exc

    report = await evaluator.run_single(
        prompt_version, run_judge=True, prior_findings=prior_findings,
    )

    row_errors: list = []
    reported_guardrail: Any = None
    if report.parsed_ok and report.parsed_response:
        sanitized, detected = sanitize_response(report.parsed_response)
        raw = sanitized.get("row_errors")
        if isinstance(raw, list):
            row_errors = list(raw)
        reported_guardrail = merge_guardrails(
            sanitized.get("guardrail_triggered"), detected
        )
        if reported_guardrail:
            logger.info("Guardrail triggered on response: %s", reported_guardrail)
    if output_path:
        save_path = Path(output_path)
        run_dir = save_path.parent
    else:
        run_dir = OUTPUTS_DIR / prompt_version
        save_path = run_dir / f"{prompt_version}_response.json"

    run_dir.mkdir(parents=True, exist_ok=True)
    save_payload: dict = {
        "prompt_version": prompt_version,
        "model": llm_config.model,
        "latency_ms": round(report.latency_ms, 1),
        "raw_response": report.raw_response,
        "row_errors": row_errors,
    }
    if reported_guardrail:
        save_payload["guardrail_triggered"] = reported_guardrail
    save_path.write_text(json.dumps(save_payload, indent=2), encoding="utf-8")
    _write_eval_artifacts(run_dir, prompt_version, report, evaluator)

    run_dir_s = _display_path(run_dir)
    run_dir_resolved = run_dir.resolve()
    default_layout = (
        run_dir_resolved == (OUTPUTS_DIR / prompt_version).resolve()
        and save_path.name == f"{prompt_version}_response.json"
    )
    include_hint = (
        save_path.name if default_layout else _display_path(save_path)
    )
    console.print(f"[green]Artifacts saved under {run_dir_s}[/green]")
    console.print(f"[green]  Response: {save_path.name}[/green]")
    console.print(f"[green]  LLM eval: {prompt_version}_llm_eval.json[/green]")
    console.print(
        f"[green]{len(row_errors)} row_errors entries saved "
        f"(use --include {include_hint} in the next run)[/green]"
    )

    if not report.parsed_ok:
        logger.warning(
            "Could not parse LLM analyst response as JSON — judge still ran on raw text."
        )

    await client.close()

@app.command()
def optimize(
    prompt_version: str = typer.Option("v1", "--from", "-f", help="Starting prompt version."),
    output: Optional[str] = typer.Option(
        None, "--output", "-o",
        help="Save the improved prompt to this file.",
    ),
    model: Optional[str] = typer.Option(
        None, "--model",
        help="LLM config name (uses config/<name>.yaml). Overrides MODEL_NAME from env.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-V"),
) -> None:
    """Improve a prompt version."""
    setup_logging("DEBUG" if verbose else "INFO")
    asyncio.run(_optimize(prompt_version, output, model))

async def _optimize(
    prompt_version: str,
    output_path: Optional[str],
    model_config_name: Optional[str],
) -> None:
    from prompt_engineering.optimization import MetaPrompter
    from prompt_engineering.util.prompt_loader import load_prompt

    client, _, _ = _build_client(model_config_name)

    prompt_file = VERSION_MAP.get(prompt_version, VERSION_MAP["v1"])
    original = load_prompt(prompt_file)
    console.print(f"[bold]Improving prompt ({prompt_version})...[/bold]\n{original.strip()[:500]}...\n")

    improved = await MetaPrompter(client).refine(original)
    console.print(f"[bold green]Improved prompt:[/bold green]\n{improved[:500]}...\n")

    prompts_dir = PACKAGE_ROOT / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    next_n = 1
    for path in prompts_dir.glob("v*.md"):
        try:
            n = int(path.stem[1:])
            next_n = max(next_n, n + 1)
        except ValueError:
            pass
    version_label = f"v{next_n}"
    version_path = prompts_dir / f"{version_label}.md"
    version_path.write_text(improved, encoding="utf-8")
    console.print(f"[green]Saved as {version_path.relative_to(PACKAGE_ROOT)} (use run -p {version_label} to run it)[/green]")

    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(improved, encoding="utf-8")
        console.print(f"[green]Saved to {output_path}[/green]")

    await client.close()

if __name__ == "__main__":
    app()
