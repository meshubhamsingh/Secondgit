"""
Translates each executed step into a Robot Framework `Browser` library
keyword line and writes a complete, standalone .robot file.

The generated file has no dependency on the LLM/OpenAI/LiteLLM layer at all —
it's plain Robot Framework + Browser library, runnable with `robot` on its
own, forever, as a regression test.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from executor import StepResult

_ACTION_TO_KEYWORD = {
    "click": "Click",
    "fill": "Fill Text",
    "select": "Select Options By",
    "check": "Check Checkbox",
    "uncheck": "Uncheck Checkbox",
    "hover": "Hover",
    "press": "Keyboard Key",
}


class RobotRecorder:
    def __init__(self) -> None:
        self._lines: list[str] = []
        self._page_opened = False

    def record(self, result: StepResult) -> None:
        comment = f"    # ${{ {result.command!r} }}".replace("$", "")
        comment = f"    # command: {result.command}"

        if result.action == "navigate" and result.selector:
            keyword = "New Page" if not self._page_opened else "Go To"
            self._page_opened = True
            self._lines.append(comment)
            self._lines.append(f"    {keyword}    {result.selector}")
            return

        if not result.success or not result.selector:
            self._lines.append(comment)
            self._lines.append(
                f"    Fail    Could not resolve step: {result.error or 'no matching element found'}"
            )
            return

        keyword = _ACTION_TO_KEYWORD.get(result.action, None)
        self._lines.append(comment)
        if keyword is None:
            self._lines.append(f"    # Unrecognized action '{result.action}' — recorded as comment only")
            return

        if result.action == "fill":
            self._lines.append(f"    {keyword}    {result.selector}    {result.value or ''}")
        elif result.action == "select":
            self._lines.append(f"    {keyword}    {result.selector}    value    {result.value or ''}")
        elif result.action == "press":
            self._lines.append(f"    {keyword}    press    {result.value or 'Enter'}")
        else:
            self._lines.append(f"    {keyword}    {result.selector}")

    def write(
        self,
        path: str | Path,
        test_name: str,
        browser: str = "chromium",
        headless: bool = False,
    ) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        header = f"""*** Settings ***
Library    Browser    auto_closing_level=TEST
Documentation    Auto-generated {datetime.now().strftime('%Y-%m-%d %H:%M')} by the
...    browser-automation-framework scenario runner. Edit freely — this file
...    has no runtime dependency on the LLM layer.

*** Test Cases ***
{test_name}
    [Setup]    New Browser    browser={browser}    headless={headless}
    [Teardown]    Run Keyword If Test Failed    Take Screenshot
"""
        body = "\n".join(self._lines)
        path.write_text(header + body + "\n", encoding="utf-8")
        return path
