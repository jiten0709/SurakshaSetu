"""Shared by the output-rail unit tests: the real lexicon pack, a context with evidence and engine
handles, registry sets with real hashes from content/seed, a gateway whose guard and verify routes
answer through httpx.MockTransport (no network), and a recorder in place of audit.chain.append."""

import json
from collections.abc import Callable
from dataclasses import replace
from functools import cache
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
import yaml
from compose_support import ROP, TERM, bundle, chunk, needs, option, product, ranking, suitability

from surakshasetu.compose.citations import EngineFact, TurnHandles, issue
from surakshasetu.compose.composer import Rendered, compose
from surakshasetu.config import Settings
from surakshasetu.crypto.jcs import sha256_hex
from surakshasetu.domain.models import DisclosureItem, DisclosureSet
from surakshasetu.gateway import Gateway, Route
from surakshasetu.kb.payload import content_sha256
from surakshasetu.rails import output
from surakshasetu.rails.output import LexiconPack, Numbers, OutputContext, load_pack
from surakshasetu.retrieval.service import EvidenceChunk

SAVER = "999N010V01"
SESSION = UUID("0190a0c4-0000-7000-8000-00000000c0de")
SUBJECT = UUID("0190a0c4-0000-7000-8000-00000000beef")
TURN = UUID("0190a0c4-0000-7000-8000-000000000042")
SEED = Path(__file__).resolve().parents[3] / "content" / "seed" / "catalog" / "disclosures.yaml"


def evidence() -> list[EvidenceChunk]:
    """E1 an exclusion and E2 a guaranteed-benefit clause (product), E3 tax, E4 regulatory."""

    def at(c: EvidenceChunk, **update: Any) -> EvidenceChunk:
        return c.model_copy(update=update)

    return [
        at(
            chunk("E1"),
            text="DUMMY: Death by suicide within twelve months of the risk commencement date is"
            " excluded, and 80% of the premiums paid is refunded.",
            section_path=["Suraksha Term Shield (999N001V02)", "Policy Wording v2", "3.1 Suicide"],
        ),
        at(
            chunk("E2"),
            text="DUMMY: The insurer pays the guaranteed maturity benefit plus the accrued"
            " guaranteed additions.",
            section_path=[
                "Suraksha Saver Guarantee (999N010V01)",
                "Policy Wording v2",
                "2.2 Maturity benefit and income option",
            ],
        ),
        at(
            chunk("E3", domain="tax"),
            text="DUMMY: Premiums may qualify for a deduction under section 123 within ₹1.5 lakh"
            " under the old regime. Maturity proceeds are tax-free under section 10(10D) when the"
            " conditions are met.",
            doc_type="explainer",
            section_path=["Tax counsel note", "2. Deductions"],
        ),
        at(
            chunk("E4", domain="regulatory"),
            text="DUMMY: Under section 45 of the Insurance Act, a policy cannot be called in"
            " question after three years from the date of issue.",
            doc_type="act",
            section_path=["Insurance Act, 1938", "Section 45"],
        ),
    ]


def facts() -> list[EngineFact]:
    return [
        EngineFact("RANK-01", "Ranked option 1", {"uin": TERM, "rank": 1, "codes": ["RANK-FIT"]}),
        EngineFact("SUIT-TERM-04", "Cover to age", {"cover_to_age": 60}),
    ]


def handles() -> TurnHandles:
    return issue(evidence(), facts())


@cache
def pack() -> LexiconPack:
    return load_pack("2026.09.1")


def ctx(**update: Any) -> OutputContext:
    base = OutputContext(
        session_id=SESSION,
        turn_id=TURN,
        subject_ref=SUBJECT,
        fsm_state="S3",
        pins={"prompt_bundle": "pb-2026.10.1"},
        key_ref="key-ref",
        locale="en-IN",
        route=Route.GEN_RECOMMEND,
        handles=handles(),
        customer_text="Which plan suits me?",
        products={
            TERM: "Suraksha Term Shield",
            ROP: "Suraksha Term Shield ROP",
            SAVER: "Suraksha Saver Guarantee",
        },
    )
    return replace(base, **update)


def numbers() -> Numbers:
    return Numbers(ranking(option(1, TERM)), suitability())


def settings(**update: Any) -> Settings:
    return Settings(_env_file=None, gateway_base_url="http://gateway.test/v1", **update)


def seed_set(uin: str = TERM, language: str = "en-IN", channel: str = "web") -> DisclosureSet:
    """The seed registry set, with bodies and hashes as the registry computes them."""
    seed = yaml.safe_load(SEED.read_text(encoding="utf-8"))
    bodies = {d["disclosure_id"]: d["bodies"][language] for d in seed["disclosures"]}
    row = next(s for s in seed["disclosure_sets"] if s["uin"] == uin)
    items = [
        DisclosureItem(disclosure_id=i, body=bodies[i], body_sha256=content_sha256(bodies[i]))
        for i in row["disclosure_ids"]
    ]
    payload = {
        "registry_version": row["registry_version"],
        "uin": uin,
        "channel": channel,
        "language": language,
        "items": [{"disclosure_id": i.disclosure_id, "body_sha256": i.body_sha256} for i in items],
    }
    return DisclosureSet(
        uin=uin,
        channel=channel,
        language=language,
        registry_version=row["registry_version"],
        items=items,
        set_sha256=sha256_hex(payload),
        is_dummy=True,
    )


def render_s3(
    context: OutputContext, sets: dict[str, DisclosureSet] | None = None
) -> Callable[[str | None], Rendered]:
    """The S3 composer, as the turn handler will pass it to output.release."""
    shown = sets if sets is not None else {TERM: seed_set()}

    def render(narrative: str | None) -> Rendered:
        return compose(
            bundle(),
            locale="en-IN",
            needs=needs(),
            suitability=suitability(),
            ranking=numbers().ranking,
            products={TERM: product(TERM)},
            disclosure_sets=shown,
            handles=context.handles,
            narrative=narrative,
        )

    return render


class Models:
    """guard-input and verify-claims behind httpx.MockTransport. verify says not_entailed for a
    claim containing UNSUPPORTED, as the stub does."""

    def __init__(
        self, *, safety: str = "safe", guard_down: bool = False, verify_down: bool = False
    ) -> None:
        self.safety, self.guard_down, self.verify_down = safety, guard_down, verify_down
        self.requests: list[tuple[str, list[dict[str, str]]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        route, messages = body["model"], body["messages"]
        self.requests.append((route, messages))
        if route == "guard-input":
            if self.guard_down:
                raise httpx.ConnectError("down")
            answer: dict[str, Any] = {"injection_score": 0.01, "safety": self.safety}
        elif route == "verify-claims":
            if self.verify_down:
                raise httpx.ConnectError("down")
            claim = messages[-1]["content"].split("<claim>", 1)[1]
            answer = {"verdict": "not_entailed" if "UNSUPPORTED" in claim else "entailed"}
        else:
            raise AssertionError(f"unexpected route {route}")
        completion = {
            "model": f"stub-{route}",
            "choices": [{"message": {"content": json.dumps(answer)}}],
        }
        return httpx.Response(200, json=completion)

    def calls(self, route: str) -> int:
        return sum(r == route for r, _ in self.requests)


def gateway(models: Models) -> Gateway:
    return Gateway(settings(), httpx.MockTransport(models))


class Recorder:
    """Stands in for audit.chain.append: the unit tests have no database."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[dict[str, Any]] = []
        monkeypatch.setattr(output.audit_chain, "append", self.append)

    def append(self, conn: Any, keys: Any, **event: Any) -> None:
        self.events.append(event)

    def rules(self) -> list[tuple[str, str, str]]:
        return [(e["header"].rail, e["header"].rule_id, e["header"].action) for e in self.events]
