"""The layering in docs/architecture.md, asserted.

A map that is only prose rots the way PLAN.md section 3 did. These tests fail
the moment an import crosses a layer, so the map stays true without anyone
remembering to check it.

The rule: a module may import only from a layer strictly below its own. What it
buys is concrete -- adapters that cannot reach the database stay testable
against a saved fixture, and `policy` staying below `sender` is what makes "no
caller bypasses may_send" checkable by reading one file.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "jobhunter"

# Lower numbers may be imported by higher ones, never the reverse.
LAYERS: dict[str, int] = {
    "config": 0,
    "models": 0,
    "http": 1,
    "db": 1,
    "google_auth": 1,
    "sources.base": 2,
    "matching.scorer": 2,
    "matching.llm_scorer": 2,
    "contacts.scraper": 2,
    "contacts.patterns": 2,
    "contacts.verify": 2,
    "sources.greenhouse": 3,
    "sources.lever": 3,
    "sources.ashby": 3,
    "sources.workable": 3,
    "sources.freehire": 3,
    "sources.careers_page": 3,
    "contacts.finder": 3,
    "contacts.importer": 3,
    "outreach.drafter": 3,
    "sources.registry": 4,
    "outreach.policy": 4,
    "outreach.sender": 5,
    "harvest": 5,
    "export": 5,
    "pipeline": 6,
    "cli": 7,
}


def _modules() -> list[str]:
    return [
        ".".join(p.relative_to(PACKAGE).with_suffix("").parts)
        for p in sorted(PACKAGE.rglob("*.py"))
        if p.name != "__init__.py"
    ]


def _imports(module: str) -> set[str]:
    """Internal modules `module` imports, by dotted name relative to the package."""
    path = PACKAGE / (module.replace(".", "/") + ".py")
    package_parts = module.split(".")[:-1]
    found: set[str] = set()
    known = set(LAYERS)

    def record(head: str, names: list[str]) -> None:
        # `from .base import X` names a module; `from .base import parse_iso`
        # names a symbol inside it. Try the longer form first.
        for name in names:
            candidate = f"{head}.{name}" if head else name
            found.add(candidate if candidate in known else head)

    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = package_parts[: len(package_parts) - (node.level - 1)]
                head = ".".join(base + ([node.module] if node.module else []))
            elif node.module and node.module.startswith("jobhunter"):
                head = node.module.removeprefix("jobhunter").lstrip(".")
            else:
                continue
            record(head, [a.name for a in node.names])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("jobhunter"):
                    found.add(alias.name.removeprefix("jobhunter").lstrip("."))

    return {f for f in found if f in known}


def test_every_module_has_a_layer():
    """A new module must be placed deliberately, not default to the top."""
    unplaced = sorted(set(_modules()) - set(LAYERS))
    assert not unplaced, (
        f"{unplaced} has no layer. Add it to LAYERS here and to the table in "
        "docs/architecture.md, choosing the lowest layer it can live in."
    )


@pytest.mark.parametrize("module", sorted(LAYERS))
def test_imports_only_from_below(module):
    if not (PACKAGE / (module.replace(".", "/") + ".py")).is_file():
        pytest.skip(f"{module} has been removed")
    offenders = [
        f"{module} (L{LAYERS[module]}) -> {dep} (L{LAYERS[dep]})"
        for dep in sorted(_imports(module))
        if LAYERS[dep] >= LAYERS[module]
    ]
    assert not offenders, "layer violation: " + "; ".join(offenders)


def test_adapters_never_touch_the_database():
    """Adapters return RawJob and let db.upsert_job persist it -- CLAUDE.md."""
    for module in sorted(m for m in _modules() if m.startswith("sources.")):
        assert "db" not in _imports(module), f"{module} imports db"


def test_only_the_cli_prints():
    """Everything else logs. A library that prints is not reusable."""
    for module in _modules():
        if module == "cli":
            continue
        tree = ast.parse((PACKAGE / (module.replace(".", "/") + ".py")).read_text())
        printers = [
            n.lineno
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print"
        ]
        assert not printers, f"{module} calls print() at line(s) {printers}"
