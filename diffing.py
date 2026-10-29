"""Split a unified diff by file and choose what fits in the LLM budget - honestly.

Nothing is dropped silently: every omitted or truncated file is reported so the
analysis can be labelled "partial" instead of pretending it saw everything.
"""
import re
from dataclasses import dataclass, field
from typing import List, Tuple

_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)\s*$")
_LOW_VALUE_SUFFIXES = (".lock", ".min.js", ".min.css", ".map", ".svg", ".snap")
_LOW_VALUE_NAMES = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock")
_LOW_VALUE_CAP = 1500  # chars - generated files get a tiny slice of the budget


@dataclass
class DiffSelection:
    text: str = ""
    included: List[str] = field(default_factory=list)
    omitted: List[str] = field(default_factory=list)
    truncated_files: List[str] = field(default_factory=list)

    @property
    def partial(self) -> bool:
        return bool(self.omitted or self.truncated_files)


def split_sections(diff_text: str) -> List[Tuple[str, str]]:
    """Return [(filename, section_text), ...] for each 'diff --git' block."""
    sections = []
    for chunk in re.split(r"(?m)^(?=diff --git )", diff_text):
        if not chunk.strip():
            continue
        match = _HEADER_RE.match(chunk.splitlines()[0]) if chunk.startswith("diff --git ") else None
        name = match.group(2) if match else "(unparsed section)"
        sections.append((name, chunk))
    return sections


def is_low_value(filename: str) -> bool:
    base = filename.rsplit("/", 1)[-1]
    return base in _LOW_VALUE_NAMES or filename.endswith(_LOW_VALUE_SUFFIXES)


def select_for_llm(sections: List[Tuple[str, str]], max_chars: int, max_file_chars: int) -> DiffSelection:
    """Fill the character budget with real code first, generated/lock files last."""
    selection = DiffSelection()
    budget = max_chars
    pieces = []
    for name, text in sorted(sections, key=lambda s: is_low_value(s[0])):  # stable sort
        if budget < 200:
            selection.omitted.append(name)
            continue
        limit = min(max_file_chars, budget)
        if is_low_value(name):
            limit = min(limit, _LOW_VALUE_CAP)
        if len(text) > limit:
            text = text[:limit] + "\n[... diff for this file truncated by analysis limits ...]\n"
            selection.truncated_files.append(name)
        pieces.append(text)
        selection.included.append(name)
        budget -= len(text)
    selection.text = "".join(pieces)
    return selection
