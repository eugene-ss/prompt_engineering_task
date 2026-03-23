from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from prompt_engineering.client.llm_client import LLMClient
from prompt_engineering.config import VERSION_MAP
from prompt_engineering.util.data_loader import load_dataset, load_golden
from prompt_engineering.util.prompt_loader import (
    load_prompt,
    parse_json_response,
    render_prompt,
)

EVALUATION_PROMPT = load_prompt("eval/evaluate.md")

logger = logging.getLogger(__name__)

# Max characters of JSON sent to the judge.
_JUDGE_JSON_CHAR_BUDGET = 120_000

def _truncate_for_judge(text: str, budget: int, label: str) -> str:
    if len(text) <= budget:
        return text
    logger.warning("Judge %s payload truncated to %s chars", label, budget)
    return text[:budget]

def _json_dumps_for_judge(obj: Any, budget: int, label: str) -> str:
    return _truncate_for_judge(json.dumps(obj), budget, label)

@dataclass
class JudgeVerdict:
    """LLM-as-judge output."""
    evaluation: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

class LLMJudge:
    def __init__(self, client: LLMClient) -> None:
        self._client = client

    async def judge(
        self,
        dataset_csv: str,
        analysis_json: str,
        golden_row_errors_json: str,
    ) -> JudgeVerdict:
        messages = [
            {"role": "system", "content": EVALUATION_PROMPT},
            {
                "role": "user",
                "content": (
                    "## Dataset\n\n```csv\n"
                    f"{dataset_csv}\n"
                    "```\n\n"
                    "## Analysis output (JSON)\n\n```json\n"
                    f"{analysis_json}\n"
                    "```\n\n"
                    "## Golden reference (row_errors JSON)\n\n```json\n"
                    f"{golden_row_errors_json}\n"
                    "```\n"
                ),
            },
        ]
        resp = await self._client.chat(messages)
        data = parse_json_response(resp.content)
        if data is None:
            logger.warning("Judge returned non-JSON: %s", resp.content[:200])
            return JudgeVerdict(
                evaluation=f"Parse failure: {resp.content[:200]}",
            )

        return JudgeVerdict(
            evaluation=str(data.get("evaluation", "")),
        )

@dataclass
class VersionReport:
    version: str
    latency_ms: float = 0.0
    raw_response: str = ""
    parsed_ok: bool = False
    parsed_response: dict[str, Any] | None = None
    judge_verdict: JudgeVerdict | None = None

class PromptEvaluator:
    def __init__(
        self,
        client: LLMClient,
        *,
        dataset_path: str | None = None,
        golden_path: str | None = None,
    ) -> None:
        self._client = client
        self._dataset_csv = load_dataset(dataset_path)
        self._golden_path = golden_path
        self._golden_doc = load_golden(golden_path)
        self._judge = LLMJudge(client)

    def golden_metadata(self) -> dict[str, Any]:
        """Metadata stored next to LLM eval output."""
        src = (
            "golden_data.json"
            if self._golden_path is None
            else str(Path(self._golden_path).resolve())
        )
        rows = self._golden_doc.get("row_errors", [])
        n = len(rows) if isinstance(rows, list) else 0
        return {
            "golden_data_source": src,
            "total_rows": int(self._golden_doc.get("total_rows", 0)),
            "golden_error_row_count": n,
        }

    async def run_single(
        self,
        prompt_version: str,
        *,
        run_judge: bool = True,
        prior_findings: list[dict[str, Any]] | None = None,
    ) -> VersionReport:
        """Run a single prompt version and evaluate with LLM judge."""
        prompt_path = VERSION_MAP.get(prompt_version)
        if not prompt_path:
            raise ValueError(f"Unknown prompt version: {prompt_version}")

        template = load_prompt(prompt_path)
        rendered = render_prompt(template, dataset=self._dataset_csv)

        if prior_findings:
            rendered += (
                "\n\n---\n\n"
                "## Previously Verified Findings\n\n"
                "The following errors were identified in a prior analysis run. "
                "Treat them as context. Focus on finding additional errors that "
                "may have been missed, and include these findings in your output "
                "as well.\n\n"
                "```json\n"
                + json.dumps(prior_findings, indent=2)
                + "\n```\n"
            )

        messages = [{"role": "user", "content": rendered}]

        t0 = time.perf_counter()
        resp = await self._client.chat(messages)
        latency_ms = (time.perf_counter() - t0) * 1000

        report = VersionReport(
            version=prompt_version,
            latency_ms=latency_ms,
            raw_response=resp.content,
        )

        b = _JUDGE_JSON_CHAR_BUDGET
        golden_json = _json_dumps_for_judge(
            self._golden_doc.get("row_errors", []), b, "golden row_errors"
        )

        parsed = parse_json_response(resp.content)
        if parsed is None:
            logger.warning(
                "Could not parse JSON from %s response", prompt_version,
            )
            if run_judge:
                analysis_json = _truncate_for_judge(
                    resp.content, b, "analysis (unparsed)"
                )
                report.judge_verdict = await self._judge.judge(
                    self._dataset_csv, analysis_json, golden_json
                )
            return report

        report.parsed_ok = True
        report.parsed_response = parsed

        if run_judge:
            analysis_json = _json_dumps_for_judge(parsed, b, "analysis JSON")
            report.judge_verdict = await self._judge.judge(
                self._dataset_csv, analysis_json, golden_json
            )

        return report