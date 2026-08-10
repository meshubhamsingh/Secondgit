"""
Shadow-DOM-aware candidate enumeration.

Playwright's own locator engines (css, text, role) already pierce OPEN shadow
roots automatically when *resolving* a selector at click/fill time — that part
needs no special handling. The gap is on the *enumeration* side: plain
`document.querySelectorAll` does NOT descend into shadow roots, so building a
"here is everything on this page you could interact with" list for the LLM to
choose from requires a manual recursive walk that explicitly follows
`element.shadowRoot`.

This module does that walk and, for each candidate, produces the MOST STABLE
selector it can (data-testid > id > aria-label > role+text > visible text),
which is then handed straight to Playwright/Robot Framework Browser library —
no custom shadow-piercing selector syntax needed on the execution side.

Known limits (see project README for the full breakdown):
  - Closed shadow roots (`mode: 'closed'`) are invisible to this walker by
    design of the platform; not supported.
  - iframes are walked at one level (best effort) and tagged with their frame
    URL; deeply nested cross-frame + shadow combos may need extra handling.
  - Elements that only render after a scroll/async fetch won't appear until
    the page is re-scanned; the scenario runner re-scans before every step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MAX_DEPTH = 25
MAX_CANDIDATES = 400

# Injected into the page/frame via page.evaluate(). Walks `document`
# recursively, descending into any open shadow root it finds, and collects
# a lightweight description of every element that looks interactive.
_WALKER_JS = r"""
() => {
  const MAX_DEPTH = %(max_depth)d;
  const MAX_CANDIDATES = %(max_candidates)d;
  const results = [];

  const INTERACTIVE_TAGS = new Set([
    'a', 'button', 'input', 'select', 'textarea', 'option', 'label', 'summary'
  ]);

  function isVisible(el) {
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return false;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    return true;
  }

  function shortText(el) {
    const t = (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ');
    return t.slice(0, 80);
  }

  function isInteractive(el) {
    const tag = el.tagName.toLowerCase();
    if (INTERACTIVE_TAGS.has(tag)) return true;
    if (el.hasAttribute('role')) return true;
    if (el.hasAttribute('onclick')) return true;
    if (el.tabIndex !== undefined && el.tabIndex >= 0 && el.tabIndex !== null) return true;
    // Custom elements (web components) are frequently the clickable surface
    // in shadow-DOM-heavy apps even without an explicit role.
    if (tag.includes('-') && (el.hasAttribute('aria-label') || el.hasAttribute('data-testid'))) return true;
    return false;
  }

  function buildSelector(el) {
    const testid = el.getAttribute('data-testid') || el.getAttribute('data-test-id') || el.getAttribute('data-qa');
    if (testid) return { selector: `css=[data-testid="${testid}"]`, confidence: 'high' };
    if (el.id) return { selector: `css=#${CSS.escape(el.id)}`, confidence: 'high' };
    const ariaLabel = el.getAttribute('aria-label');
    if (ariaLabel) return { selector: `css=[aria-label="${ariaLabel.replace(/"/g, '\\"')}"]`, confidence: 'high' };
    const name = el.getAttribute('name');
    if (name && (el.tagName.toLowerCase() === 'input' || el.tagName.toLowerCase() === 'select' || el.tagName.toLowerCase() === 'textarea')) {
      return { selector: `css=[name="${name}"]`, confidence: 'medium' };
    }
    const text = shortText(el);
    if (text && text.length <= 60) {
      return { selector: `text=${text}`, confidence: 'medium' };
    }
    // Fallback: structural path from nearest ancestor with an id, else tag+nth-of-type chain.
    let path = [];
    let node = el;
    let depth = 0;
    while (node && node.nodeType === 1 && depth < 6) {
      let seg = node.tagName.toLowerCase();
      if (node.id) {
        path.unshift(`#${CSS.escape(node.id)}`);
        break;
      }
      const parent = node.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter(c => c.tagName === node.tagName);
        const idx = siblings.indexOf(node) + 1;
        seg += `:nth-of-type(${idx})`;
      }
      path.unshift(seg);
      node = node.parentElement;
      depth += 1;
    }
    return { selector: `css=${path.join(' > ')}`, confidence: 'low' };
  }

  function walk(root, inShadow, depth, frameLabel) {
    if (depth > MAX_DEPTH || results.length >= MAX_CANDIDATES) return;
    const children = root.querySelectorAll ? root.querySelectorAll('*') : [];
    for (const el of children) {
      if (results.length >= MAX_CANDIDATES) break;
      if (isInteractive(el) && isVisible(el)) {
        const { selector, confidence } = buildSelector(el);
        results.push({
          index: results.length,
          tag: el.tagName.toLowerCase(),
          role: el.getAttribute('role') || null,
          text: shortText(el),
          aria_label: el.getAttribute('aria-label') || null,
          type: el.getAttribute('type') || null,
          placeholder: el.getAttribute('placeholder') || null,
          href: el.getAttribute('href') || null,
          selector: selector,
          selector_confidence: confidence,
          in_shadow: inShadow,
          frame: frameLabel,
        });
      }
      if (el.shadowRoot) {
        walk(el.shadowRoot, true, depth + 1, frameLabel);
      }
    }
  }

  walk(document, false, 0, 'main');
  return results;
}
""" % {"max_depth": MAX_DEPTH, "max_candidates": MAX_CANDIDATES}


@dataclass
class Candidate:
    index: int
    tag: str
    role: str | None
    text: str
    aria_label: str | None
    type: str | None
    placeholder: str | None
    href: str | None
    selector: str
    selector_confidence: str
    in_shadow: bool
    frame: str
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "Candidate":
        return cls(
            index=raw["index"],
            tag=raw["tag"],
            role=raw.get("role"),
            text=raw.get("text", ""),
            aria_label=raw.get("aria_label"),
            type=raw.get("type"),
            placeholder=raw.get("placeholder"),
            href=raw.get("href"),
            selector=raw["selector"],
            selector_confidence=raw["selector_confidence"],
            in_shadow=raw.get("in_shadow", False),
            frame=raw.get("frame", "main"),
            raw=raw,
        )


def get_candidates(page, include_iframes: bool = True) -> list[Candidate]:
    """Enumerate interactive elements on the page, piercing open shadow roots.

    Re-run this before every step — content that renders lazily (after a
    click, scroll, or async fetch) won't be visible until the page is
    re-scanned.
    """
    raw_candidates = page.evaluate(_WALKER_JS)
    candidates = [Candidate.from_raw(r) for r in raw_candidates]

    if include_iframes:
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                raw = frame.evaluate(_WALKER_JS)
            except Exception:
                # Cross-origin or not-yet-loaded frames can't be evaluated; skip.
                continue
            for r in raw:
                r["index"] = len(candidates)
                r["frame"] = frame.url or "iframe"
                candidates.append(Candidate.from_raw(r))

    return candidates
