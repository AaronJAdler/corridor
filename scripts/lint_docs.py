"""Check the documentation: links, diagrams, the exported API description and a deny list.

    uv run poe lint-docs                       check everything below
    uv run poe lint-docs --render              also render every diagram with mermaid-cli
    uv run python scripts/lint_docs.py --write-openapi     rewrite docs/openapi.json

What is checked, over ``README.md``, ``CLAUDE.md`` and every Markdown file under ``docs``:

- Every relative link points at a file that exists, and every ``#anchor`` at a heading the
  target file has. Links to other sites are not fetched.
- Every fenced ``mermaid`` block starts with a diagram type Mermaid knows, has balanced
  brackets, and closes every block it opens. That is a structural check and not a parse.
  With ``--render`` each diagram is also given to mermaid-cli, through ``mmdc`` if it is
  installed and through ``npx`` if not; if neither can be started the diagrams are reported
  as "not rendered" and the run still passes.
- ``docs/openapi.json`` is what the application describes itself as today, and every
  operation in it is named in ``docs/api.md``.
- No document contains a string from the deny list. The list is read from the environment
  variable ``CORRIDOR_DOCS_DENYLIST``, entries separated by semicolons or new lines, and is
  compared without regard to case. A finding names the file, the line and the number of
  the entry, never the entry, so that a list of names need not be stored or printed.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPENAPI_PATH = ROOT / "docs" / "openapi.json"
API_GUIDE_PATH = ROOT / "docs" / "api.md"
DENYLIST_VARIABLE = "CORRIDOR_DOCS_DENYLIST"

MERMAID_TYPES = frozenset(
    {
        "flowchart",
        "graph",
        "sequenceDiagram",
        "stateDiagram",
        "stateDiagram-v2",
        "erDiagram",
        "classDiagram",
        "gantt",
        "pie",
        "journey",
        "gitGraph",
        "mindmap",
        "timeline",
        "quadrantChart",
        "requirementDiagram",
        "C4Context",
    }
)

# The keywords that open a block which a line reading ``end`` closes.
_BLOCK_OPENERS = {
    "sequenceDiagram": ("alt", "opt", "loop", "par", "critical", "break", "rect", "box"),
    "flowchart": ("subgraph",),
    "graph": ("subgraph",),
}

_FENCE = re.compile(r"^(?P<indent> {0,3})(?P<marks>`{3,}|~{3,})(?P<info>.*)$")
_HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_LINK = re.compile(r"!?\[(?:[^\]\\]|\\.)*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_HTML_ANCHOR = re.compile(r"<a\s+(?:id|name)=\"([^\"]+)\"")
_EXTERNAL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_QUOTED = re.compile(r"\"[^\"\n]*\"")
# An entity-relationship line joins two names with a marker such as ``||--o{``, whose
# braces are not brackets.
_ER_RELATION = re.compile(r"[|}][|o]?(?:--|\.\.)[|o]?[|{]|[|}]o(?:--|\.\.)o[|{]")
_PAIRS = {")": "(", "]": "[", "}": "{"}

_PARSE_FAILURE = re.compile(r"parse error|syntax error|lexical error|no diagram type", re.I)


@dataclass(frozen=True)
class Problem:
    path: Path
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path.relative_to(ROOT).as_posix()}:{self.line}: {self.message}"


@dataclass(frozen=True)
class Diagram:
    path: Path
    line: int
    lines: tuple[str, ...]


@dataclass(frozen=True)
class Document:
    path: Path
    # (line number, text) for every line outside a fenced block, with inline code removed.
    prose: tuple[tuple[int, str], ...]
    anchors: frozenset[str]
    diagrams: tuple[Diagram, ...]
    text: str


def documents() -> list[Path]:
    found = [ROOT / "README.md", ROOT / "CLAUDE.md", *sorted((ROOT / "docs").rglob("*.md"))]
    return [path for path in found if path.is_file()]


def slug(heading: str) -> str:
    """The anchor GitHub gives a heading: lower case, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"!?\[((?:[^\]\\]|\\.)*)\]\([^)]*\)", r"\1", heading)
    text = text.replace("`", "").strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def read(path: Path) -> Document:
    text = path.read_text(encoding="utf-8")
    prose: list[tuple[int, str]] = []
    anchors: set[str] = set()
    seen: dict[str, int] = {}
    diagrams: list[Diagram] = []
    fence: str | None = None
    block: list[str] = []
    block_info, block_start = "", 0
    for number, line in enumerate(text.splitlines(), start=1):
        marker = _FENCE.match(line)
        if fence is None:
            if marker is not None:
                fence = marker["marks"]
                block, block_info, block_start = [], marker["info"].strip(), number
                continue
            heading = _HEADING.match(line)
            if heading is not None:
                base = slug(heading[2])
                count = seen.get(base, 0)
                seen[base] = count + 1
                anchors.add(base if count == 0 else f"{base}-{count}")
            anchors.update(_HTML_ANCHOR.findall(line))
            prose.append((number, _INLINE_CODE.sub("", line)))
        elif (
            marker is not None
            and marker["marks"][0] == fence[0]
            and len(marker["marks"]) >= len(fence)
            and not marker["info"].strip()
        ):
            if block_info.split(" ")[0] == "mermaid":
                diagrams.append(Diagram(path, block_start, tuple(block)))
            fence = None
        else:
            block.append(line)
    return Document(path, tuple(prose), frozenset(anchors), tuple(diagrams), text)


# --- links -----------------------------------------------------------------------------------


def check_links(docs: dict[Path, Document]) -> Iterator[Problem]:
    for document in docs.values():
        for number, line in document.prose:
            for target in _LINK.findall(line):
                if _EXTERNAL.match(target):
                    continue
                file_part, _, anchor = target.partition("#")
                destination = (
                    (document.path.parent / file_part).resolve() if file_part else document.path
                )
                if ROOT not in destination.parents and destination != ROOT:
                    yield Problem(document.path, number, f"link leaves the repository: {target}")
                    continue
                if not destination.exists():
                    yield Problem(document.path, number, f"link to a missing file: {target}")
                    continue
                if not anchor:
                    continue
                if destination.suffix.lower() != ".md" or not destination.is_file():
                    continue
                known = docs.get(destination) or read(destination)
                if anchor not in known.anchors:
                    yield Problem(document.path, number, f"link to a missing heading: {target}")


# --- diagrams --------------------------------------------------------------------------------


def check_diagram(diagram: Diagram) -> Iterator[Problem]:
    body = [line for line in diagram.lines if line.strip() and not line.strip().startswith("%%")]
    if not body:
        yield Problem(diagram.path, diagram.line, "empty mermaid block")
        return
    kind = body[0].split()[0].rstrip(":;")
    if kind not in MERMAID_TYPES:
        yield Problem(diagram.path, diagram.line, f"unknown mermaid diagram type: {kind}")
        return

    stack: list[tuple[str, int]] = []
    depth = 0
    openers = _BLOCK_OPENERS.get(kind, ())
    for offset, raw in enumerate(diagram.lines, start=1):
        number = diagram.line + offset
        line = _QUOTED.sub("", raw)
        if kind == "erDiagram":
            line = _ER_RELATION.sub("", line)
        words = line.split()
        if openers and words:
            if words[0] in openers:
                depth += 1
            elif words[0] == "end" and len(words) == 1:
                depth -= 1
                if depth < 0:
                    yield Problem(diagram.path, number, "mermaid: 'end' with no block to close")
                    depth = 0
        for character in line:
            if character in "([{":
                stack.append((character, number))
            elif character in _PAIRS:
                if not stack or stack[-1][0] != _PAIRS[character]:
                    yield Problem(diagram.path, number, f"mermaid: unbalanced '{character}'")
                    return
                stack.pop()
    for character, number in stack:
        yield Problem(diagram.path, number, f"mermaid: '{character}' is never closed")
    if depth > 0:
        yield Problem(diagram.path, diagram.line, f"mermaid: {depth} block(s) without 'end'")


def _mermaid_cli() -> list[str] | None:
    """The command that runs mermaid-cli here, or None if it cannot be started."""
    candidates: list[list[str]] = []
    installed = shutil.which("mmdc")
    if installed is not None:
        candidates.append([installed])
    npx = shutil.which("npx")
    if npx is not None:
        candidates.append([npx, "--yes", "-p", "@mermaid-js/mermaid-cli", "mmdc"])
    for command in candidates:
        try:
            probe = subprocess.run(
                [*command, "--version"], capture_output=True, text=True, timeout=300, check=False
            )
        except OSError, subprocess.TimeoutExpired:
            continue
        if probe.returncode == 0:
            return command
    return None


def render_diagrams(diagrams: list[Diagram]) -> tuple[list[Problem], str]:
    """Render each diagram. Returns what failed to parse, and one line saying what was done."""
    command = _mermaid_cli()
    if command is None:
        return [], f"{len(diagrams)} mermaid diagram(s) not rendered: mermaid-cli is not available"
    problems: list[Problem] = []
    rendered = 0
    unknown: list[str] = []
    with tempfile.TemporaryDirectory() as scratch:
        for index, diagram in enumerate(diagrams):
            source = Path(scratch) / f"diagram-{index}.mmd"
            source.write_text("\n".join(diagram.lines) + "\n", encoding="utf-8")
            try:
                result = subprocess.run(
                    [*command, "--quiet", "-i", str(source), "-o", str(source.with_suffix(".svg"))],
                    capture_output=True,
                    text=True,
                    timeout=300,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as failure:
                unknown.append(type(failure).__name__)
                continue
            if result.returncode == 0:
                rendered += 1
            elif _PARSE_FAILURE.search(result.stdout + result.stderr):
                problems.append(Problem(diagram.path, diagram.line, "mermaid: does not parse"))
            else:
                # The renderer itself failed, most often because it has no browser to
                # draw with. That says nothing about the diagram.
                unknown.append(f"exit {result.returncode}")
    summary = f"{rendered} of {len(diagrams)} mermaid diagram(s) rendered"
    if unknown:
        summary += f"; {len(unknown)} not rendered: the renderer failed ({unknown[0]})"
    return problems, summary


# --- the API description ---------------------------------------------------------------------


def openapi_document() -> str:
    """What the application describes itself as, as text: sorted keys, two-space indent."""
    sys.path.insert(0, str(ROOT / "src"))
    from corridor.api.app import create_app
    from corridor.platform.config import Settings

    # Describing the routes opens no connection, so the two addresses are never used. They
    # are given here so that the description does not depend on the caller's environment.
    settings = Settings(
        _env_file=None,
        environment="development",
        log_level="WARNING",
        database_url="postgresql+asyncpg://unused@localhost/unused",
        redis_url="redis://localhost/0",
    )
    described = create_app(settings).openapi()
    return json.dumps(described, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def check_openapi(docs: dict[Path, Document]) -> Iterator[Problem]:
    if not OPENAPI_PATH.is_file():
        yield Problem(OPENAPI_PATH, 1, "missing: write it with --write-openapi")
        return
    current = openapi_document()
    if OPENAPI_PATH.read_text(encoding="utf-8") != current:
        yield Problem(
            OPENAPI_PATH,
            1,
            "out of date: run `uv run python scripts/lint_docs.py --write-openapi`"
            " and bring docs/api.md in line with what changed",
        )
    guide = docs.get(API_GUIDE_PATH)
    if guide is None:
        yield Problem(API_GUIDE_PATH, 1, "missing")
        return
    for path, operations in sorted(json.loads(current)["paths"].items()):
        for method in sorted(operations):
            named = f"`{method.upper()} {path}`"
            if named not in guide.text:
                yield Problem(API_GUIDE_PATH, 1, f"does not describe {named}")


# --- the deny list ---------------------------------------------------------------------------


def denylist() -> list[str]:
    raw = os.environ.get(DENYLIST_VARIABLE, "")
    return [entry.strip().casefold() for entry in re.split(r"[;\n]", raw) if entry.strip()]


def check_denylist(docs: dict[Path, Document], entries: list[str]) -> Iterator[Problem]:
    for document in docs.values():
        for number, line in enumerate(document.text.splitlines(), start=1):
            folded = line.casefold()
            for index, entry in enumerate(entries, start=1):
                if entry in folded:
                    yield Problem(document.path, number, f"contains deny-list entry {index}")


# --- the command -----------------------------------------------------------------------------


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Check the documentation.")
    parser.add_argument(
        "--render", action="store_true", help="also render every diagram with mermaid-cli"
    )
    parser.add_argument(
        "--write-openapi", action="store_true", help="rewrite docs/openapi.json and stop"
    )
    options = parser.parse_args(arguments)

    if options.write_openapi:
        OPENAPI_PATH.write_text(openapi_document(), encoding="utf-8", newline="\n")
        print(f"Wrote {OPENAPI_PATH.relative_to(ROOT).as_posix()}.")
        return 0

    docs = {path: read(path) for path in documents()}
    diagrams = [diagram for document in docs.values() for diagram in document.diagrams]
    entries = denylist()

    problems = list(check_links(docs))
    for diagram in diagrams:
        problems.extend(check_diagram(diagram))
    problems.extend(check_openapi(docs))
    problems.extend(check_denylist(docs, entries))

    if options.render:
        failed, rendering = render_diagrams(diagrams)
        problems.extend(failed)
    else:
        rendering = (
            f"{len(diagrams)} mermaid diagram(s) checked for structure, not rendered"
            " (pass --render to render them)"
        )

    for problem in problems:
        print(problem)
    print(f"{len(docs)} document(s) checked.")
    print(rendering + ".")
    print(
        f"Deny list: {len(entries)} entr{'y' if len(entries) == 1 else 'ies'} checked."
        if entries
        else f"Deny list: not checked ({DENYLIST_VARIABLE} is not set)."
    )
    if problems:
        print(f"lint-docs FAILED: {len(problems)} problem(s).")
        return 1
    print("lint-docs passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
