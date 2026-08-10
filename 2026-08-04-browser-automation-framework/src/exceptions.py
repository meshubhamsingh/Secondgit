"""Custom exception types so the scenario runner can distinguish failure
kinds in reports and decide whether a retry is worth attempting."""


class AutomationError(Exception):
    """Base class for all framework-raised errors."""


class ResolutionError(AutomationError):
    """The LLM could not map a command to a candidate element, or returned
    output that couldn't be parsed."""


class ElementNotFoundError(AutomationError):
    """The candidate list was empty, or the chosen selector didn't resolve
    to a real element on the page at execution time."""


class ExecutionError(AutomationError):
    """Playwright raised while attempting to perform the resolved action
    (click/fill/navigate/etc.)."""


class ScenarioValidationError(AutomationError):
    """The scenario .yaml file itself is malformed — e.g. `steps` was written
    as a single string instead of a list, `base_url` is missing, etc. Caught
    at load time, before a browser is even launched, so a typo can't burn
    through hundreds of pointless retries."""


class ConfigurationError(AutomationError):
    """Required environment configuration (API key for the selected LLM
    provider, etc.) is missing. Checked before the run starts rather than
    surfacing as a wall of per-step LLM failures."""
