"""Property-based contract tests of the running domain tier (Schemathesis).

Every operation, fed data generated from contracts/openapi/domain-services.v1.yaml, must never
answer 5xx and must match its response schema; the stateful run follows the spec's links. Run with
`make up && make contract-test`.

The data is steered, in an in-memory copy of the spec, towards what the live service accepts, so
the run reaches the decision code and not only 404s and 409s:
- pins.rules is the live rules version;
- a Uin is a seeded product or rider, or one that doesn't exist;
- a consent record names the notice in force (these records land in the dev database).
The kill-switch operation is deleted from that copy: it would withdraw the seeded products for
good, and a filter isn't enough (schemathesis.pytest.from_fixture drops a filter applied to the
fixture's schema, which is how a first run withdrew them).
"""

import copy
from pathlib import Path
from typing import Any

import httpx
import pytest
import schemathesis
from hypothesis import HealthCheck, settings
from openapi_spec_validator.readers import read_from_filename
from schemathesis.pytest import from_fixture

from surakshasetu.config import Settings

pytestmark = pytest.mark.stack

SPEC = Path(__file__).parents[3] / "contracts" / "openapi" / "domain-services.v1.yaml"
CHECKS = ["not_a_server_error", "response_schema_conformance"]
# content/seed/catalog/products.yaml, plus a UIN that doesn't exist.
UINS = [
    "999N001V02",
    "999N002V01",
    "999N010V01",
    "999A007V01",
    "999A008V01",
    "999A009V01",
    "999N099V99",
]
SLOW = [HealthCheck.too_slow, HealthCheck.filter_too_much]
KILL_SWITCH = "/v1/catalog/products/{uin}/kill-switch"


@pytest.fixture(scope="module")
def domain_schema() -> Any:
    config = Settings(_env_file=None)
    headers = {"Authorization": f"Bearer {config.domain_token.get_secret_value()}"}
    with httpx.Client(base_url=config.domain_base_url, headers=headers, timeout=5) as client:
        rules = client.get("/v1/meta/versions").raise_for_status().json()["rules_version"]
        notice = (
            client.get("/v1/consent/notices/current", params={"language": "en-IN"})
            .raise_for_status()
            .json()
        )

    raw, _ = read_from_filename(str(SPEC))
    spec: dict[str, Any] = copy.deepcopy(dict(raw))
    del spec["paths"][KILL_SWITCH]
    schemas = spec["components"]["schemas"]
    schemas["Pins"]["properties"]["rules"] = {"type": "string", "const": rules}
    for operations in spec["paths"].values():
        for operation in operations.values():
            for parameter in operation.get("parameters", []):
                if parameter.get("name") == "pins.rules":
                    parameter["schema"] = {"type": "string", "const": rules}
    schemas["Uin"] = {"type": "string", "enum": UINS}
    consent = schemas["ConsentRecordCreate"]["properties"]
    consent["notice_version"] = {"type": "string", "const": notice["notice_version"]}
    consent["notice_sha256"] = {"type": "string", "const": notice["body_sha256"]}
    consent["language"] = {"type": "string", "const": notice["language"]}

    schema = schemathesis.openapi.from_dict(spec)
    schema.config.update(base_url=config.domain_base_url, headers=headers)
    schema.config.checks.update(included_check_names=CHECKS)
    return schema


schema = from_fixture("domain_schema")


@schema.parametrize()
@settings(max_examples=50, deadline=None, suppress_health_check=SLOW)
def test_every_operation_honours_the_contract(case: schemathesis.Case) -> None:
    assert "kill-switch" not in case.path  # never withdraw the dev database's products
    case.call_and_validate()


def test_linked_calls_honour_the_contract(domain_schema: Any) -> None:
    domain_schema.as_state_machine().run(
        settings=settings(
            max_examples=50, stateful_step_count=6, deadline=None, suppress_health_check=SLOW
        )
    )
