from __future__ import annotations
import re
from pathlib import Path

_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


class TemplateManager:
    """Loads email templates from a directory and fills {{placeholder}} variables."""

    def __init__(self, templates_dir: str | Path = "templates") -> None:
        self._dir = Path(templates_dir)
        self._templates: dict[int, str] = {}
        self._reload()

    def _reload(self) -> None:
        self._templates.clear()
        for path in sorted(self._dir.glob("template_*.txt")):
            try:
                num = int(path.stem.split("_")[1])
            except (IndexError, ValueError):
                continue
            self._templates[num] = path.read_text(encoding="utf-8")

    def fill(self, template_num: int, **kwargs: str) -> str:
        """Return the template with all {{key}} placeholders replaced by kwargs values.

        Any placeholder ending in _cap (e.g. {{trade_plural_cap}}) is automatically
        resolved as the capitalised form of the base placeholder (trade_plural.capitalize()).
        Unknown placeholders are left as-is so the user can see what still needs filling.
        """
        if template_num not in self._templates:
            available = sorted(self._templates)
            raise ValueError(
                f"Template {template_num} not found. Available: {available}"
            )
        body = self._templates[template_num]
        return _PLACEHOLDER_RE.sub(
            lambda m: self._resolve(m.group(1), kwargs), body
        )

    def fill_followup(self, followup_num: int, **kwargs: str) -> str:
        """Return a follow-up template (followup_N.txt) with placeholders filled."""
        path = self._dir / f"followup_{followup_num}.txt"
        if not path.exists():
            raise ValueError(f"Follow-up template {followup_num} not found at {path}")
        body = path.read_text(encoding="utf-8")
        return _PLACEHOLDER_RE.sub(
            lambda m: self._resolve(m.group(1), kwargs), body
        )

    @staticmethod
    def _resolve(key: str, kwargs: dict[str, str]) -> str:
        """Resolve a placeholder key, handling the _cap suffix automatically."""
        if key in kwargs:
            return kwargs[key]
        if key.endswith("_cap"):
            base = key[:-4]
            if base in kwargs:
                return kwargs[base].capitalize()
        return "{{" + key + "}}"

    def get_raw(self, template_num: int) -> str:
        """Return the raw template text with placeholders intact, or '' if not found."""
        return self._templates.get(template_num, "")

    def list_templates(self) -> list[tuple[int, str]]:
        """Return (num, first_line_preview) for each template that has real content."""
        result = []
        for n in sorted(self._templates):
            body = self._templates[n].strip()
            if not body or body.upper() == "TO BE WRITTEN BY USER":
                continue
            preview = self._templates[n].splitlines()[0][:80]
            result.append((n, preview))
        return result

    @property
    def count(self) -> int:
        return len(self._templates)

    @property
    def ready_count(self) -> int:
        """Number of templates with real content (not placeholder)."""
        return len(self.list_templates())
