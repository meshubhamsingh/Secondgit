# Browser Automation Framework

Drive a browser with plain-English commands, resolved by an LLM, executed via
Playwright (with automatic open-shadow-DOM support), while simultaneously
recording every action as a standalone Robot Framework regression script.

## How it fits together

```
scenarios/*.yaml  (plain-English steps)
        │
        ▼
scenario_runner.py  ── orchestrates a run start to finish
        │
        ├── shadow_walker.py   → enumerates clickable/fillable elements on the
        │                        current page, recursing into open shadow
        │                        roots (Playwright's own locators already
        │                        pierce shadow DOM at execution time; this
        │                        walker exists purely so the LLM has a list
        │                        of real candidates to choose from)
        │
        ├── llm_resolver.py    → the ONLY file that talks to an LLM. Given a
        │                        command + candidate list, returns which
        │                        element and what action. Swap providers by
        │                        editing .env — no code changes.
        │
        ├── executor.py        → performs the action via Playwright, retries
        │                        with the error fed back to the LLM, captures
        │                        a screenshot on failure
        │
        └── recorder.py        → turns each executed step into a Robot
                                   Framework `Browser` library keyword line,
                                   writes output/<scenario>.robot at the end
```

The generated `.robot` file has zero dependency on the LLM layer — run it
with plain `robot` any time afterward for a fast, deterministic regression
check, complete with Robot Framework's own `report.html` / `log.html` /
`output.xml`.

## One-time setup

```bash
python -m venv .venv
source .venv/bin/activate        # .venv\Scripts\activate on Windows
pip install -r requirements.txt
playwright install chromium      # browser binaries for the live authoring run
rfbrowser init                   # Node-based Playwright driver for Robot Framework's Browser library (needs Node.js installed)
cp .env.example .env
# edit .env: set LLM_PROVIDER / LLM_MODEL / the matching API key, and BASE_URL
```

## Running a scenario end to end

```bash
cd src
python scenario_runner.py ../scenarios/example_scenario.yaml
```

This will:
1. Launch a real browser and navigate to `base_url`.
2. Execute every step in `steps`, in order, live.
3. On failure: capture a screenshot, retry up to `MAX_RETRIES_PER_STEP` times
   (feeding the error back to the LLM), then either stop or continue to the
   next step depending on `ON_FAILURE` in `.env` (defaults to `continue`,
   so one bad step doesn't abort the whole run — every step's result still
   shows up in the summary).
4. Write `output/<scenario>.robot`, `output/<scenario>-summary.json`, and
   append to `output/run.log`.

## Getting Robot Framework's full report

Once you have a generated `.robot` file, run it directly (no LLM involved):

```bash
robot --outputdir output output/example_scenario.robot
```

This produces `report.html` (pass/fail summary), `log.html` (step detail with
a screenshot auto-attached on any keyword failure, via the `[Teardown]`
in the generated file), and `output.xml` (machine-readable — convertible to
JUnit for CI later if needed).

## Switching LLM providers later

Edit two lines in `.env`:

```
LLM_PROVIDER=anthropic
LLM_MODEL=claude-sonnet-5
ANTHROPIC_API_KEY=...
```

`llm_resolver.py` uses LiteLLM under the hood, which normalizes the request
across OpenAI/Anthropic/Gemini/etc. — no other file needs to change.

## Troubleshooting

- **`Cannot navigate to invalid URL`** — a bare domain like `www.amazon.in`
  with no `http(s)://` scheme. Fixed automatically as of this version
  (`urls.py` normalizes it), but double check `base_url` in your scenario
  yaml if you still see this.
- **`Page.goto: Timeout ...ms exceeded` while "waiting until networkidle"**
  — the site never stops making background network calls (analytics,
  personalization, long-polling), so Playwright's `networkidle` condition
  never fires. This is common on large commercial sites (Amazon included).
  Fix: leave `NAV_WAIT_UNTIL=load` in `.env` (the default) rather than
  `networkidle`.
- **VS Code shows `Unresolved library: Browser`** — `robotframework-browser`
  isn't installed in the interpreter VS Code is using, or `rfbrowser init`
  hasn't been run. See the setup section above; run
  `Ctrl+Shift+P` → "Robot Framework: Clear caches and restart" after fixing.

## Known limitations (be aware of these, not silently papered over)

- **Closed shadow roots** (`mode: 'closed'`) are invisible to the walker by
  design of the browser platform. Not supported. (Not applicable if your app
  only uses open shadow roots, as discussed.)
- **iframes** are walked one level deep, best-effort; deeply nested
  cross-frame + shadow combinations may need extra handling if you hit them.
- **Lazily-rendered content** (virtualized lists, content appearing after an
  async fetch) won't show up until the page is re-scanned — the runner
  re-scans before every step, but very slow-loading content can still be
  missed; increase `MAX_RETRIES_PER_STEP` if you see this.
- **LLM mis-picks** on ambiguous commands (e.g. three "Edit" buttons in a
  table) are a prompt/data problem, not a shadow-DOM problem — make step
  wording as specific as the page allows ("click edit on the Jane Doe row").

## Project layout

```
2026-08-04-browser-automation-framework/
├── README.md
├── requirements.txt
├── .env.example
├── config/
├── scenarios/
│   └── example_scenario.yaml
├── src/
│   ├── shadow_walker.py
│   ├── llm_resolver.py
│   ├── executor.py
│   ├── recorder.py
│   ├── exceptions.py
│   └── scenario_runner.py
└── output/              (generated at runtime: .robot files, screenshots, logs, reports)
```
