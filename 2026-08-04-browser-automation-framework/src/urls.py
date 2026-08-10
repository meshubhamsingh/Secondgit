"""Small shared helper — kept separate from `scenario_runner`/`executor` so
both can use the same normalization without duplicating it."""


def normalize_url(url: str) -> str:
    """Playwright's page.goto() requires a full scheme and will NOT guess
    https:// for a bare domain the way a browser address bar does — a value
    like "www.amazon.in" raises "Cannot navigate to invalid URL". Fix that
    common omission instead of failing the run."""
    url = url.strip()
    if not url.startswith(("http://", "https://", "file://")):
        return f"https://{url}"
    return url
