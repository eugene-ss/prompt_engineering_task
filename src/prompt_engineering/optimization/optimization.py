from __future__ import annotations

import logging

from prompt_engineering.client.llm_client import LLMClient
from prompt_engineering.util.prompt_loader import load_prompt, parse_json_response

logger = logging.getLogger(__name__)

META_OPTIMIZE = load_prompt("optimization/meta_optimize.md")

class MetaPrompter:
    """Uses the LLM to refine a prompt."""
    def __init__(self, client: LLMClient) -> None:
        self._client = client

    async def refine(self, prompt: str) -> str:
        """Take a prompt and return improved version."""
        messages = [
            {"role": "system", "content": META_OPTIMIZE},
            {
                "role": "user",
                "content": (
                    "## Prompt to Improve\n\n"
                    f"```markdown\n{prompt}\n```\n\n"
                    "Apply all improvement strategies and return the refined prompt."
                ),
            },
        ]

        resp = await self._client.chat(messages)
        parsed = parse_json_response(resp.content) or {"refined_prompt": resp.content}

        improvements = parsed.get("improvements_applied", [])
        if improvements:
            logger.info("Meta-prompter applied %d improvements:", len(improvements))
            for imp in improvements:
                logger.info("  - %s", imp)

        return parsed.get("refined_prompt", prompt)
