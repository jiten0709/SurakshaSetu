"""The ingest pipeline without the stack: Docling's Markdown output as Blocks, a parse failure that
names its document, and an unavailable embed route that fails the run before anything is indexed."""

from io import BytesIO
from pathlib import Path

import pytest
from dagster import Failure, materialize
from docling.datamodel.base_models import DocumentStream, InputFormat
from docling.document_converter import DocumentConverter

from surakshasetu.kb.chunker import Block
from surakshasetu_ingest import assets

MARKDOWN = """# Policy Wording

## 5. Exclusions

DUMMY: The first paragraph, with **bold** words.

### 5.3 Suicide

DUMMY: Clause text.

- item one
- item two

| Year | Refund |
|------|--------|
| 1    | 80%    |
"""

FRONT = """---
breadcrumb: ["Test Product (999N001V02)", "Policy Wording v9"]
citation_prefix: Test_999N001V02_PolicyWording
doc_type: policy_wording
product_uin: 999N001V02
product_types: [term]
---
"""

MANIFEST = """snapshots:
  product: {snapshot_id: product-test-embed-down, approved_by: DUMMY-test}
documents:
  - {doc_id: "t:pw", collection: product, path: product/pw.md, version: v9,
     effective_from: 2026-09-01, effective_to: null, status: approved, by: DUMMY-test,
     at: 2026-09-02}
"""


def blocks(markdown: str) -> list[Block]:
    converter = DocumentConverter(allowed_formats=[InputFormat.MD])
    stream = DocumentStream(name="t.md", stream=BytesIO(markdown.encode()))
    return assets.to_blocks(converter.convert(stream).document)


def test_docling_markdown_becomes_headings_and_paragraphs() -> None:
    got = blocks(MARKDOWN)

    assert got[:5] == [
        Block(1, "Policy Wording"),
        Block(2, "5. Exclusions"),
        Block(None, "DUMMY: The first paragraph, with bold words."),
        Block(3, "5.3 Suicide"),
        Block(None, "DUMMY: Clause text."),
    ]
    assert got[5] == Block(None, "- item one\n- item two")
    assert got[6].level is None
    assert got[6].text.startswith("|") and "Year" in got[6].text and "80%" in got[6].text
    assert len(got) == 7


def kb(tmp_path: Path) -> assets.Kb:
    (tmp_path / "product").mkdir()
    (tmp_path / "product" / "pw.md").write_text(FRONT + MARKDOWN)
    (tmp_path / "review-manifest.yaml").write_text(MANIFEST)
    return assets.Kb(kb_dir=str(tmp_path), uri_base="test", collection_prefix="test_never_")


def test_a_parse_failure_names_the_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("parser crashed")

    monkeypatch.setattr(DocumentConverter, "convert", broken)
    sources = assets.source_docs(kb(tmp_path))

    with pytest.raises(Failure, match=r"Docling could not parse t:pw \(product/pw.md\)"):
        assets.parsed(sources)


def test_an_unavailable_embed_route_fails_before_indexing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SS_TEI_EMBED_URL", "http://127.0.0.1:9")  # nothing listens there

    result = materialize(assets.ASSETS, resources={"kb": kb(tmp_path)}, raise_on_error=False)

    done = {e.asset_key.path[-1] for e in result.get_asset_materialization_events()}
    assert not result.success
    assert "approved" in done
    assert {"vectors", "indexed_snapshot"}.isdisjoint(done)
