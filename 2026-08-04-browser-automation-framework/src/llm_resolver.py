"""
Swappable LLM resolver.

This is the ONLY place in the codebase that talks to an LLM. Everything else
(walker, executor, recorder, scenario runner) calls `LLMResolver.resolve()`
and never knows or cares which provider answered.

Switching providers later = editing LLM_PROVIDER / LLM_MODEL in .env.
No code changes required, because LiteLLM normalizes the request/response
shape across OpenAI, Anthropic, Gemini, and others behind one call signature.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import litellm

from exceptions import ResolutionError
from shadow_walker import Candidate

_SYSTEM_PROMPT = """You are the element-resolution brain for a browser automation tool.
You will be given:
1. A plain-English instruction describing one action to take on the current web page.
2. A numbered list of candidate elements found on the page (including elements
   inside open shadow DOM and iframes), each with tag, role, visible text,
   aria-label, and other attributes.

Pick the single best-matching candidate and the action to perform on it.
If the instruction implies typing/filling a value, extract that value.
If no candidate plausibly matches, set candidate_index to null and explain why
in "reasoning" rather than guessing.

Respond with ONLY a JSON object matching this schema, no prose, no markdown:
{
  "candidate_index": <int or null>,
  "action": "click" | "fill" | "select" | "check" | "uncheck" | "hover" | "press",
  "value": <string or null>,
  "confidence": <float 0-1>,
  "reasoning": <short string>
}
"""


@dataclass
class ActionDecision:
    candidate_index: int | None
    action: str
    value: str | None
    confidence: float
    reasoning: str


class LLMResolver:
    def __init__(self, provider: str | None = None, model: str | None = None) -> None:
        self.provider = provider or os.getenv("LLM_PROVIDER", "openai")
        self.model = model or os.getenv("LLM_MODEL", "gpt-4.1")

    def _candidates_payload(self, candidates: list[Candidate]) -> list[dict[str, Any]]:
        return [
            {
                "index": c.index,
                "tag": c.tag,
                "role": c.role,
                "text": c.text,
                "aria_label": c.aria_label,
                "type": c.type,
                "placeholder": c.placeholder,
                "href": c.href,
                "in_shadow": c.in_shadow,
                "frame": c.frame,
            }
            for c in candidates
        ]

    def resolve(
        self,
        command: str,
        candidates: list[Candidate],
        extra_context: str | None = None,
    ) -> ActionDecision:
        if not candidates:
            raise ResolutionError(f"No candidates were found on the page for command: {command!r}")

        user_content = {
            "instruction": command,
            "candidates": self._candidates_payload(candidates),
        }
        if extra_context:
            user_content["previous_attempt_feedback"] = extra_context

        try:
            response = litellm.completion(
                model=self.model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(user_content)},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            raw = response["choices"][0]["message"]["content"]
        except Exception as exc:  # network/auth/provider errors
            raise ResolutionError(f"LLM call failed ({self.provider}/{self.model}): {exc}") from exc

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ResolutionError(f"LLM returned non-JSON output: {raw!r}") from exc

        if parsed.get("candidate_index") is None:
            raise ResolutionError(
                f"LLM couldn't match a candidate for {command!r}: {parsed.get('reasoning')}"
            )

        return ActionDecision(
            candidate_index=parsed["candidate_index"],
            action=parsed["action"],
            value=parsed.get("value"),
            confidence=float(parsed.get("confidence", 0.0)),
            reasoning=parsed.get("reasoning", ""),
        )
