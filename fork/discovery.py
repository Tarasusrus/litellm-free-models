"""Fork hooks for upstream's find-shared-models.py (ADR 0001 §2).

Two things upstream's discovery cannot know about the fork:

* Google AI Studio is Gemini's own vendor. Upstream denies every
  `gemini-*` id everywhere because aggregators resell the Gemini API;
  at `google-ai` the same names are the free tier the fork routes to
  first. The denylist keeps applying at every other provider.
* `fork/models.yaml` holds deployments outside the template. The stale
  check reads the template only, so the fragment has to be merged in —
  the same way `fork/render.py` does before rendering.
"""
from __future__ import annotations

import re
from pathlib import Path

# provider -> pattern of the models that are that provider's own line.
FIRST_PARTY_MODELS: dict[str, re.Pattern] = {
    "google-ai": re.compile(r"(?:^|/)gemini(?:-|$)", re.IGNORECASE),
}

FORK_FRAGMENT = Path("fork") / "models.yaml"


def is_first_party(model_id: str, provider: str) -> bool:
    """True when `provider` is the vendor of `model_id` (never a resale)."""
    pattern = FIRST_PARTY_MODELS.get(provider)
    return bool(pattern and pattern.search(model_id))


def template_with_fragment(template_path: Path) -> str:
    """Template text with `fork/models.yaml` (next to the template) first
    in model_list; the bare template when there is no fragment."""
    text = template_path.read_text(encoding="utf-8")
    fragment = template_path.resolve().parent / FORK_FRAGMENT
    if not fragment.exists():
        return text
    marker = "model_list:\n"
    if marker not in text:
        raise RuntimeError(f"model_list not found in {template_path}")
    return text.replace(marker, marker + fragment.read_text(encoding="utf-8"), 1)
