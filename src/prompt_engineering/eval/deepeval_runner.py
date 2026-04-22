"""DeepEval-backed evaluator. This runner exposes a metric bundle:
* ``GEval("ErrorCoverage")`` — reuses the rubric from ``eval/evaluate.md``
  so the scoring signal stays consistent with the custom judge.
* ``GoldenRecallMetric`` — deterministic precision / recall / F1 against
  ``golden_data.json``. No LLM call, which makes it cheap and reliable.
* ``FaithfulnessMetric`` (optional, LLM-based) — flags hallucinated
  reasons / rows that are not grounded in the dataset CSV.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from prompt_engineering.client.llm_client import LLMClient
from prompt_engineering.util.prompt_loader import load_prompt, parse_json_response

logger = logging.getLogger(__name__)

EVALUATION_PROMPT = load_prompt("eval/evaluate.md")

@dataclass
class MetricResult:
    name: str
    score: float
    threshold: float
    passed: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "score": round(self.score, 4),
            "threshold": self.threshold,
            "passed": self.passed,
            "reason": self.reason,
        }

@dataclass
class DeepEvalVerdict:
    evaluation: str = ""
    metrics: list[MetricResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluation": self.evaluation,
            "metrics": [m.to_dict() for m in self.metrics],
        }

def _safe_row_index(entry: Any) -> int | None:
    if not isinstance(entry, dict):
        return None
    try:
        return int(entry.get("row_index"))
    except (TypeError, ValueError):
        return None

def _row_index_set(rows: Any) -> set[int]:
    if not isinstance(rows, list):
        return set()
    out: set[int] = set()
    for row in rows:
        idx = _safe_row_index(row)
        if idx is not None:
            out.add(idx)
    return out

def compute_golden_recall(
    analyst_rows: Any, golden_rows: Any
) -> tuple[float, float, float, dict[str, int]]:

    # Deterministic precision / recall / F1
    analyst = _row_index_set(analyst_rows)
    golden = _row_index_set(golden_rows)
    if not golden:
        return 0.0, 0.0, 0.0, {"tp": 0, "fp": len(analyst), "fn": 0}

    tp = len(analyst & golden)
    fp = len(analyst - golden)
    fn = len(golden - analyst)

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall)
        else 0.0
    )
    return precision, recall, f1, {"tp": tp, "fp": fp, "fn": fn}

class ProxyDeepEvalLLM:
    """Adapter that lets DeepEval metrics call the project's LLM proxy."""
    def __new__(cls, client: LLMClient):
        try:
            from deepeval.models import DeepEvalBaseLLM
        except ImportError as exc:  # pragma: no cover - optional dep
            raise RuntimeError(
                "deepeval is not installed. Install with "
                "`uv sync --extra deepeval` to use the deepeval evaluator."
            ) from exc

        class _Impl(DeepEvalBaseLLM):
            def __init__(self, inner: LLMClient) -> None:
                self._client = inner

            def load_model(self):
                return self._client

            def generate(self, prompt: str) -> str:
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    pass
                else:
                    raise RuntimeError(
                        "ProxyDeepEvalLLM.generate() was called inside a running "
                        "event loop; use a_generate / a_measure instead."
                    )
                resp = asyncio.run(
                    self._client.chat([{"role": "user", "content": prompt}])
                )
                return resp.content

            async def a_generate(self, prompt: str) -> str:
                resp = await self._client.chat(
                    [{"role": "user", "content": prompt}]
                )
                return resp.content

            def get_model_name(self) -> str:
                return self._client._llm_config.model  # noqa: SLF001

        return _Impl(client)

class DeepEvalRunner:
    def __init__(
        self,
        client: LLMClient,
        *,
        threshold: float = 0.7,
        run_faithfulness: bool = False,
    ) -> None:
        self._client = client
        self._threshold = threshold
        self._run_faithfulness = run_faithfulness

    async def evaluate(
        self,
        dataset_csv: str,
        analysis_json: str,
        golden_row_errors_json: str,
    ) -> DeepEvalVerdict:
        try:
            from deepeval.metrics import GEval
            from deepeval.test_case import LLMTestCase, LLMTestCaseParams
        except ImportError as exc:  # pragma: no cover - optional dep
            raise RuntimeError(
                "deepeval is not installed. Install with "
                "`uv sync --extra deepeval` to use the deepeval evaluator."
            ) from exc

        llm = ProxyDeepEvalLLM(self._client)

        input_blob = (
            "Identify all data-quality errors in the provided CSV. "
            "Respond with a row_errors JSON array."
        )
        test_case = LLMTestCase(
            input=input_blob,
            actual_output=analysis_json,
            expected_output=golden_row_errors_json,
            context=[dataset_csv],
            retrieval_context=[dataset_csv],
        )

        metrics: list[MetricResult] = []

        g_eval = GEval(
            name="ErrorCoverage",
            criteria=(
                "Evaluate whether the analyst's row_errors JSON (ACTUAL_OUTPUT) "
                "matches the golden row_errors (EXPECTED_OUTPUT). Use the "
                "rubric provided in the system prompt from evaluate.md."
            ),
            evaluation_steps=[
                "Parse ACTUAL_OUTPUT; if it is not valid JSON, score low.",
                (
                    "Compare row_index coverage: the golden file covers rows "
                    "121-300 (180 rows). Rows 1-120 are clean by design, so "
                    "any row_index < 121 in ACTUAL_OUTPUT is a false positive."
                ),
                (
                    "Check that error categories used in ACTUAL_OUTPUT match "
                    "those in EXPECTED_OUTPUT for overlapping rows."
                ),
                (
                    "Penalise missing error categories, missing rows, and "
                    "false positives in the clean range."
                ),
                "Return a score in [0, 1] and a short reason.",
            ],
            evaluation_params=[
                LLMTestCaseParams.INPUT,
                LLMTestCaseParams.ACTUAL_OUTPUT,
                LLMTestCaseParams.EXPECTED_OUTPUT,
            ],
            model=llm,
            threshold=self._threshold,
        )

        try:
            await g_eval.a_measure(test_case)
            metrics.append(
                MetricResult(
                    name="GEval.ErrorCoverage",
                    score=float(g_eval.score or 0.0),
                    threshold=self._threshold,
                    passed=bool(g_eval.success),
                    reason=str(g_eval.reason or ""),
                )
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("GEval metric failed: %s", exc)
            metrics.append(
                MetricResult(
                    name="GEval.ErrorCoverage",
                    score=0.0,
                    threshold=self._threshold,
                    passed=False,
                    reason=f"metric error: {exc}",
                )
            )

        analyst_rows: list = []
        parsed_analysis = parse_json_response(analysis_json)
        if isinstance(parsed_analysis, dict):
            raw = parsed_analysis.get("row_errors")
            if isinstance(raw, list):
                analyst_rows = raw

        try:
            golden_rows = json.loads(golden_row_errors_json)
        except json.JSONDecodeError:
            golden_rows = []

        precision, recall, f1, counts = compute_golden_recall(
            analyst_rows, golden_rows
        )
        metrics.append(
            MetricResult(
                name="GoldenRecall.F1",
                score=f1,
                threshold=self._threshold,
                passed=f1 >= self._threshold,
                reason=(
                    f"precision={precision:.3f}, recall={recall:.3f}, "
                    f"tp={counts['tp']}, fp={counts['fp']}, fn={counts['fn']}"
                ),
            )
        )

        if self._run_faithfulness:
            try:
                from deepeval.metrics import FaithfulnessMetric
            except ImportError:
                logger.warning(
                    "FaithfulnessMetric requested but deepeval is missing."
                )
            else:
                try:
                    faith = FaithfulnessMetric(
                        threshold=self._threshold, model=llm
                    )
                    await faith.a_measure(test_case)
                    metrics.append(
                        MetricResult(
                            name="Faithfulness",
                            score=float(faith.score or 0.0),
                            threshold=self._threshold,
                            passed=bool(faith.success),
                            reason=str(faith.reason or ""),
                        )
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("Faithfulness metric failed: %s", exc)

        overall = sum(m.score for m in metrics) / len(metrics) if metrics else 0.0
        summary_bits = "; ".join(
            f"{m.name}={m.score:.2f}" for m in metrics
        )
        evaluation = (
            f"Overall DeepEval score {overall:.2f} (threshold "
            f"{self._threshold}). Breakdown: {summary_bits}."
        )

        return DeepEvalVerdict(evaluation=evaluation, metrics=metrics)