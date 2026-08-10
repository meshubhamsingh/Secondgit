"""
End-to-end scenario runner.

Reads a scenario file (plain-English steps), drives a real browser through
every step start to finish, records a replayable .robot file as it goes, and
produces both Robot Framework's own report/log/output and a short custom
run summary.

Usage:
    python scenario_runner.py scenarios/example_scenario.yaml
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

import yaml
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from exceptions import ConfigurationError, ScenarioValidationError
from executor import ActionExecutor, StepResult
from llm_resolver import LLMResolver
from recorder import RobotRecorder
from urls import normalize_url

# Which env var holds the API key for each LLM_PROVIDER value, so we can
# fail fast with one clear message instead of N per-step LLM failures.
_PROVIDER_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


def _validate_scenario(scenario: dict, scenario_path: Path) -> None:
    # base_url may come from the scenario file itself, or fall back to
    # BASE_URL in .env if the scenario doesn't set one — but at least one
    # of the two has to be present.
    has_scenario_url = bool(str(scenario.get("base_url", "")).strip())
    has_env_url = bool(os.getenv("BASE_URL", "").strip())
    if not has_scenario_url and not has_env_url:
        raise ScenarioValidationError(
            f"{scenario_path}: no 'base_url' in the scenario file, and no BASE_URL fallback in .env"
        )

    steps = scenario.get("steps")
    if isinstance(steps, str):
        raise ScenarioValidationError(
            f"{scenario_path}: 'steps' is a single string, not a list — Python will happily "
            "iterate over it character by character (that's why you'd see hundreds of "
            "one-letter steps). Use YAML '-' bullets instead, e.g.:\n"
            "steps:\n  - \"click the login button\"\n  - \"go to the customers page\""
        )
    if not isinstance(steps, list) or not steps:
        raise ScenarioValidationError(f"{scenario_path}: 'steps' must be a non-empty YAML list")
    for i, s in enumerate(steps, start=1):
        if not isinstance(s, str) or not s.strip():
            raise ScenarioValidationError(f"{scenario_path}: step #{i} is empty or not a string: {s!r}")


def _check_llm_credentials(provider: str) -> None:
    key_env = _PROVIDER_KEY_ENV.get(provider.lower())
    if key_env is None:
        return  # unrecognized/custom provider — let LiteLLM validate it itself
    value = os.getenv(key_env, "")
    if not value or value.strip().lower() in ("", "sk-replace-me"):
        raise ConfigurationError(
            f"{key_env} is not set (or is still the placeholder from .env.example). "
            f"LLM_PROVIDER={provider!r} needs a real key in .env before running a scenario."
        )

ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = ROOT / ".env"
# override=True: if a stray environment variable (e.g. left over from a
# `set LLM_PROVIDER=...` typed earlier in the same terminal session while
# troubleshooting) is already present in the process, .env should still win.
# Without this, python-dotenv leaves pre-existing OS env vars untouched,
# which silently defeats whatever you just edited in .env.
_env_loaded = load_dotenv(_ENV_PATH, override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(ROOT / "output" / "run.log", mode="a", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("scenario_runner")


def run_scenario(scenario_path: str | Path) -> dict:
    scenario_path = Path(scenario_path)
    scenario = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))

    # Loud and explicit on purpose: silently falling back to defaults when
    # .env isn't found is exactly what caused confusion before (a run
    # "using" Gemini per .env but actually still hitting OpenAI, because
    # .env was never found/read in the first place).
    log.info(
        "Config: .env path=%s (found=%s) | LLM_PROVIDER=%s LLM_MODEL=%s",
        _ENV_PATH, _env_loaded, os.getenv("LLM_PROVIDER", "openai (default)"),
        os.getenv("LLM_MODEL", "gpt-4.1 (default)"),
    )

    # Fail fast on a malformed scenario file or missing LLM credentials —
    # before a browser is even launched — rather than burning through
    # hundreds of doomed per-step retries.
    _validate_scenario(scenario, scenario_path)
    provider = os.getenv("LLM_PROVIDER", "openai")
    _check_llm_credentials(provider)

    name = scenario.get("name", scenario_path.stem)
    base_url = normalize_url(str(scenario.get("base_url") or os.getenv("BASE_URL")))
    steps = scenario["steps"]

    on_failure = os.getenv("ON_FAILURE", "continue").lower()  # "stop" | "continue"
    max_retries = int(os.getenv("MAX_RETRIES_PER_STEP", "2"))
    headless = os.getenv("HEADLESS", "false").lower() == "true"
    # "networkidle" times out on sites with continuous background network
    # activity (analytics, personalization, long-polling) — Amazon is a
    # textbook example. "load" is a far more reliable default; override via
    # NAV_WAIT_UNTIL in .env if a specific site needs something else.
    nav_wait_until = os.getenv("NAV_WAIT_UNTIL", "load")

    output_dir = ROOT / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    robot_path = output_dir / f"{scenario_path.stem}.robot"
    summary_path = output_dir / f"{scenario_path.stem}-summary.json"

    resolver = LLMResolver()
    recorder = RobotRecorder()
    results: list[StepResult] = []

    log.info("Starting scenario '%s' against %s (%d steps, on_failure=%s)", name, base_url, len(steps), on_failure)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        page = browser.new_page()

        start_result = StepResult(
            command=f"go to {base_url}",
            success=True,
            action="navigate",
            selector=base_url,
            attempts=1,
        )
        try:
            page.goto(base_url, wait_until=nav_wait_until, timeout=30_000)
            # Large commercial sites (Amazon included) often keep
            # client-side hydrating/rendering well after the "load" event —
            # give it a moment before the first candidate scan, or the
            # walker can see a noticeably sparser DOM than what's actually
            # on screen a couple seconds later.
            page.wait_for_timeout(int(os.getenv("POST_NAV_SETTLE_MS", "2000")))
        except Exception as exc:
            start_result = StepResult(command=f"go to {base_url}", success=False, error=str(exc))
        recorder.record(start_result)
        results.append(start_result)

        executor = ActionExecutor(page, resolver, max_retries=max_retries, screenshot_dir=output_dir / "screenshots")

        if start_result.success:
            for i, step in enumerate(steps, start=1):
                if page.is_closed():
                    log.error(
                        "Browser page was closed unexpectedly before step %d/%d — "
                        "stopping the run instead of attempting the remaining steps.",
                        i, len(steps),
                    )
                    results.append(StepResult(
                        command=step, success=False,
                        error="Page/browser was already closed when this step was reached.",
                    ))
                    break

                t0 = time.monotonic()
                result = executor.run_step(step, i)
                elapsed = int((time.monotonic() - t0) * 1000)
                results.append(result)
                recorder.record(result)

                status = "PASS" if result.success else "FAIL"
                log.info(
                    "[%d/%d] %-4s (%dms, %d attempt%s) — %s",
                    i, len(steps), status, elapsed, result.attempts,
                    "s" if result.attempts != 1 else "", step,
                )
                if not result.success:
                    log.warning("  reason: %s", result.error)
                    if result.screenshot_path:
                        log.warning("  screenshot: %s", result.screenshot_path)
                    if on_failure == "stop":
                        log.error("Stopping scenario on first failure (ON_FAILURE=stop).")
                        break
        else:
            log.error("Initial navigation to %s failed, aborting scenario: %s", base_url, start_result.error)

        browser.close()

    recorder.write(robot_path, test_name=name, headless=headless)

    passed = sum(1 for r in results if r.success)
    failed = sum(1 for r in results if not r.success)
    retried = sum(1 for r in results if r.attempts > 1)

    summary = {
        "scenario": name,
        "base_url": base_url,
        "total_steps": len(results),
        "passed": passed,
        "failed": failed,
        "retried": retried,
        "robot_file": str(robot_path),
        "failures": [
            {"command": r.command, "error": r.error, "screenshot": r.screenshot_path}
            for r in results if not r.success
        ],
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    log.info("=" * 60)
    log.info(
        "Scenario '%s' complete: %d/%d passed, %d failed, %d retried",
        name, passed, len(results), failed, retried,
    )
    log.info("Replayable Robot Framework file: %s", robot_path)
    log.info("Run summary: %s", summary_path)
    log.info("=" * 60)

    return summary


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scenario_runner.py <scenario.yaml>")
        sys.exit(1)
    try:
        run_scenario(sys.argv[1])
    except (ScenarioValidationError, ConfigurationError) as exc:
        # Clean one-line message instead of a full traceback for the
        # mistakes we can actually anticipate (bad yaml, missing API key).
        log.error("%s: %s", type(exc).__name__, exc)
        sys.exit(1)
