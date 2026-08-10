"""
Executes one plain-English step: re-scan the page for candidates, ask the
LLM resolver which one matches, perform the action via Playwright, and
retry (feeding the previous error back to the LLM) on failure.

Playwright's own locator engines pierce open shadow DOM automatically when
resolving a selector, so once we have a selector string from the walker,
`page.locator(selector).click()` works the same whether the element is at
the top level or three shadow roots deep.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from exceptions import ElementNotFoundError, ExecutionError, ResolutionError
from llm_resolver import LLMResolver
from shadow_walker import Candidate, get_candidates
from urls import normalize_url

# "networkidle" is what most examples show, but Playwright's own docs warn it
# may never fire on pages with ongoing background activity — analytics,
# personalization pings, long-polling, websockets, etc. Amazon (and most
# large commercial sites) never go fully idle, so a wait_until="networkidle"
# navigation reliably times out on them. "load" (all page resources loaded,
# but background XHR doesn't block it) is a far safer default. Override via
# NAV_WAIT_UNTIL in .env if a specific site genuinely needs something else.
NAV_WAIT_UNTIL = os.getenv("NAV_WAIT_UNTIL", "load")

# Large sites frequently keep hydrating/rendering client-side content well
# after the "load" event fires (Amazon included) — the DOM the walker sees
# immediately after navigation can be noticeably sparser than what a human
# sees a couple seconds later. Since a "no candidates found" failure used to
# retry instantly (zero elapsed time for the page to catch up), it could
# fail all `max_retries` attempts in milliseconds without ever giving the
# page a chance to finish rendering. These settle delays fix that.
POST_NAV_SETTLE_MS = int(os.getenv("POST_NAV_SETTLE_MS", "2000"))
RETRY_DELAY_MS = int(os.getenv("RETRY_DELAY_MS", "1500"))


@dataclass
class StepResult:
    command: str
    success: bool
    action: str | None = None
    selector: str | None = None
    frame: str | None = None
    value: str | None = None
    attempts: int = 0
    error: str | None = None
    screenshot_path: str | None = None
    duration_ms: int = 0
    reasoning: str | None = None


class ActionExecutor:
    def __init__(
        self,
        page,
        resolver: LLMResolver,
        max_retries: int = 2,
        screenshot_dir: str | Path = "output/screenshots",
    ) -> None:
        self.page = page
        self.resolver = resolver
        self.max_retries = max_retries
        self.screenshot_dir = Path(screenshot_dir)
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)

    def _frame_for(self, candidate: Candidate):
        if candidate.frame in (None, "main"):
            return self.page
        for frame in self.page.frames:
            if frame.url == candidate.frame:
                return frame
        # Fall back to main page if the frame can't be re-located.
        return self.page

    def _perform(self, candidate: Candidate, action: str, value: str | None) -> None:
        target = self._frame_for(candidate)
        locator = target.locator(candidate.selector).first

        if action == "click":
            locator.click(timeout=10_000)
        elif action == "fill":
            locator.fill(value or "", timeout=10_000)
        elif action == "select":
            locator.select_option(value, timeout=10_000)
        elif action == "check":
            locator.check(timeout=10_000)
        elif action == "uncheck":
            locator.uncheck(timeout=10_000)
        elif action == "hover":
            locator.hover(timeout=10_000)
        elif action == "press":
            locator.press(value or "Enter", timeout=10_000)
        else:
            raise ExecutionError(f"Unknown action type from LLM: {action!r}")

        # Give the page a moment to settle (navigation, re-render, etc.)
        # before the next step re-scans for candidates. Best-effort only —
        # not every action triggers navigation, so a timeout here is fine.
        try:
            self.page.wait_for_load_state(NAV_WAIT_UNTIL, timeout=5_000)
        except Exception:
            pass

    def run_step(self, command: str, step_number: int) -> StepResult:
        start = time.monotonic()
        last_error: str | None = None

        # "go to <url>" / "navigate to <url>" is handled directly — no need
        # to burn an LLM call resolving a URL that's already in the text.
        # Matches both a full "https://..." and a bare domain like
        # "www.amazon.in" (normalized below), but not something like
        # "go to the customers page", which has no dot and contains spaces.
        lowered = command.strip().lower()
        direct_nav_url: str | None = None
        for prefix in ("go to ", "navigate to "):
            if lowered.startswith(prefix):
                remainder = command[len(prefix):].strip()
                if remainder and " " not in remainder and ("." in remainder or remainder.startswith("http")):
                    direct_nav_url = normalize_url(remainder)
                break

        if direct_nav_url:
            url = direct_nav_url
            try:
                self.page.goto(url, wait_until=NAV_WAIT_UNTIL, timeout=30_000)
                self.page.wait_for_timeout(POST_NAV_SETTLE_MS)
                return StepResult(
                    command=command,
                    success=True,
                    action="navigate",
                    selector=url,
                    attempts=1,
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
            except Exception as exc:
                shot = self._capture_failure_screenshot(step_number)
                return StepResult(
                    command=command,
                    success=False,
                    action="navigate",
                    error=str(exc),
                    screenshot_path=shot,
                    attempts=1,
                    duration_ms=int((time.monotonic() - start) * 1000),
                )

        for attempt in range(1, self.max_retries + 2):  # first try + retries
            try:
                candidates = get_candidates(self.page)
                decision = self.resolver.resolve(command, candidates, extra_context=last_error)

                if decision.candidate_index is None or not (0 <= decision.candidate_index < len(candidates)):
                    raise ElementNotFoundError(
                        f"Resolver returned an out-of-range candidate index for {command!r}"
                    )
                candidate = candidates[decision.candidate_index]

                self._perform(candidate, decision.action, decision.value)

                return StepResult(
                    command=command,
                    success=True,
                    action=decision.action,
                    selector=candidate.selector,
                    frame=candidate.frame,
                    value=decision.value,
                    attempts=attempt,
                    duration_ms=int((time.monotonic() - start) * 1000),
                    reasoning=decision.reasoning,
                )

            except (ResolutionError, ElementNotFoundError) as exc:
                last_error = str(exc)
            except Exception as exc:  # Playwright timeouts/errors during the action itself
                last_error = f"Action execution failed: {exc}"

            if attempt < self.max_retries + 1:
                # Give a slow-hydrating page a chance to catch up before the
                # next attempt re-scans — see POST_NAV_SETTLE_MS comment above.
                try:
                    self.page.wait_for_timeout(RETRY_DELAY_MS)
                except Exception:
                    pass

        shot = self._capture_failure_screenshot(step_number)
        return StepResult(
            command=command,
            success=False,
            error=last_error,
            screenshot_path=shot,
            attempts=self.max_retries + 1,
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    def _capture_failure_screenshot(self, step_number: int) -> str | None:
        path = self.screenshot_dir / f"step-{step_number:03d}-failure.png"
        try:
            self.page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception:
            return None
