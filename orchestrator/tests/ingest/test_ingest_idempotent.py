"""Ingestion against the running stack (Qdrant, tei-embed, surakshasetu_test) in collections of its
own: a re-run adds no point, the review gate holds, a released snapshot is immutable, and a new
snapshot supersedes the old one while its points stay searchable."""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from dagster import materialize
from qdrant_client import QdrantClient, models

from surakshasetu_ingest import assets

pytestmark = pytest.mark.stack

DOC = """---
breadcrumb: ["Test Product (999N001V02)", "Policy Wording v9"]
citation_prefix: Test_999N001V02_PolicyWording
doc_type: policy_wording
product_uin: 999N001V02
product_types: [term]
---
# Policy Wording

## 1. Free-look

DUMMY: The policyholder may return the policy within thirty days of receiving it.

## 2. Grace period

DUMMY: Premiums paid within thirty days of the due date keep the policy in force.
"""
PENDING = DOC.replace("Free-look", "Draft clause").replace("return the policy", "not return it")


def manifest(snapshot_id: str) -> str:
    rows = [("t:pw", "pw.md", "approved"), ("t:draft", "draft.md", "pending")]
    docs = "".join(
        f"""  - {{doc_id: "{d}", collection: product, path: product/{p}, version: v9,
     effective_from: 2026-09-01, effective_to: null, status: {s}, by: DUMMY-test,
     at: 2026-09-02}}
"""
        for d, p, s in rows
    )
    return (
        f"snapshots:\n  product: {{snapshot_id: {snapshot_id}, approved_by: DUMMY-test}}\n"
        f"documents:\n{docs}"
    )


@pytest.fixture
def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    dsn = os.environ.get("SS_TEST_PG_DSN_CATALOG_LOADER")
    admin = os.environ.get("SS_TEST_PG_DSN_ADMIN")
    if not dsn or not admin:
        pytest.fail("SS_TEST_PG_DSN_CATALOG_LOADER is not set; run `make up && make check-ingest`")
    monkeypatch.setenv("SS_PG_DSN_CATALOG_LOADER", dsn)
    tag = uuid.uuid4().hex[:8]
    (tmp_path / "product").mkdir()
    (tmp_path / "product" / "pw.md").write_text(DOC)
    (tmp_path / "product" / "draft.md").write_text(PENDING)
    kb = assets.Kb(kb_dir=str(tmp_path), uri_base="test", collection_prefix=f"test_{tag}_")
    client = QdrantClient(url=assets.IngestSettings().qdrant_url)
    yield {"kb": kb, "tag": tag, "dir": tmp_path, "client": client, "admin": admin}
    name = kb.collection("product")
    if client.collection_exists(name):
        for snapshot in client.list_snapshots(name):
            client.delete_snapshot(name, snapshot.name, wait=True)
        client.delete_collection(name)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(
            "DELETE FROM catalog.corpus_snapshot WHERE snapshot_id LIKE %s", (f"product-{tag}-%",)
        )


def run(stack: dict[str, Any], snapshot_id: str) -> bool:
    (stack["dir"] / "review-manifest.yaml").write_text(manifest(snapshot_id))
    return materialize(assets.ASSETS, resources={"kb": stack["kb"]}, raise_on_error=False).success


def points(stack: dict[str, Any], snapshot_id: str) -> list[models.Record]:
    only = models.Filter(
        must=[models.FieldCondition(key="snapshot_id", match=models.MatchValue(value=snapshot_id))]
    )
    records, _ = stack["client"].scroll(
        stack["kb"].collection("product"), scroll_filter=only, limit=100
    )
    return list(records)


def rows(stack: dict[str, Any]) -> list[tuple[str, str, int]]:
    with psycopg.connect(stack["admin"]) as conn:
        return conn.execute(
            "SELECT snapshot_id, status, chunk_count FROM catalog.corpus_snapshot"
            " WHERE snapshot_id LIKE %s ORDER BY snapshot_id",
            (f"product-{stack['tag']}-%",),
        ).fetchall()


def test_ingestion_is_idempotent_gated_and_immutable(stack: dict[str, Any]) -> None:
    first = f"product-{stack['tag']}-a"
    assert run(stack, first)
    indexed = points(stack, first)
    assert len(indexed) == 2
    assert {p.payload["doc_id"] for p in indexed if p.payload} == {"t:pw"}  # draft: pending
    assert rows(stack) == [(first, "active", 2)]

    assert run(stack, first)  # the same snapshot again: nothing new
    assert sorted(str(p.id) for p in points(stack, first)) == sorted(str(p.id) for p in indexed)
    assert stack["client"].count(stack["kb"].collection("product"), exact=True).count == 2
    assert rows(stack) == [(first, "active", 2)]

    (stack["dir"] / "product" / "pw.md").write_text(DOC.replace("thirty days of the", "the"))
    assert not run(stack, first)  # changed content under a released snapshot id
    assert rows(stack) == [(first, "active", 2)]

    second = f"product-{stack['tag']}-b"
    assert run(stack, second)
    assert rows(stack) == [(first, "superseded", 2), (second, "active", 2)]
    assert len(points(stack, first)) == 2  # still searchable for sessions that pinned it
    assert len(points(stack, second)) == 2
