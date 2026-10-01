"""Retrieval explains and cites; it is never the source of a product number or disclosure text (TDD
§2.1). Enforced by module boundaries: the premium and disclosure paths never import retrieval, and
retrieval never imports the domain client that carries premiums and disclosure sets."""

import ast
import re
from pathlib import Path

import surakshasetu

PACKAGE = Path(surakshasetu.__file__).parent
# Modules that carry premiums, quotes, disclosure text or number placeholders, now or later.
NUMBER_OR_DISCLOSURE_PATH = re.compile(r"disclosure|premium|placeholder|quote")


def imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found |= {f"{node.module}.{alias.name}" for alias in node.names}
    return found


def module(path: Path) -> str:
    return ".".join(path.relative_to(PACKAGE.parent).with_suffix("").parts)


def test_retrieval_never_imports_the_domain_tier() -> None:
    offenders = [
        f"{module(p)} imports {name}"
        for p in sorted((PACKAGE / "retrieval").rglob("*.py"))
        for name in imports(p)
        if name.startswith("surakshasetu.domain")
    ]

    assert not offenders, offenders


def test_premium_and_disclosure_paths_never_import_retrieval() -> None:
    guarded = [
        p
        for p in sorted(PACKAGE.rglob("*.py"))
        if p.is_relative_to(PACKAGE / "domain") or NUMBER_OR_DISCLOSURE_PATH.search(p.stem)
    ]
    offenders = [
        f"{module(p)} imports {name}"
        for p in guarded
        for name in imports(p)
        if name.startswith("surakshasetu.retrieval")
    ]

    assert guarded  # the domain client at least
    assert not offenders, offenders


def test_the_scan_sees_both_import_forms(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text("import surakshasetu.retrieval\nfrom surakshasetu.retrieval import service\n")

    assert {"surakshasetu.retrieval", "surakshasetu.retrieval.service"} <= imports(probe)
