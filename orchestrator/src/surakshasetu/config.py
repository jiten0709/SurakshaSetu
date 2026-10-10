"""Runtime settings, read from SS_* environment variables and an optional .env file."""

import re
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError

ENV_PREFIX = "SS_"
# Settings with no safe default outside a developer machine: pilot and prod must set them.
REQUIRED_OUTSIDE_DEV = (
    "pg_dsn_app",
    "redis_url",
    "domain_token",
    "domain_internal_token",
    "ops_api_key",
    "compliance_api_key",
    "advisor_api_key",
    "pg_dsn_erasure",
    "pg_dsn_compliance",
    "kek_b64",
    "pg_dsn_keyvault",
    "minio_endpoint",
    "minio_access_key",
    "minio_secret_key",
    "tsa_key_path",
    "anchor_retention_days",
    "gateway_base_url",
    "gateway_api_key",
    "tei_embed_url",
    "tei_rerank_url",
    "qdrant_url",
    "intake_signing_key_b64",
    "journey_url",
)


class ConfigError(RuntimeError):
    """The environment does not form valid Settings. The message never contains values."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
        hide_input_in_errors=True,
    )

    env: Literal["dev", "test", "pilot", "prod"] = "dev"
    pg_dsn_app: SecretStr | None = None
    redis_url: SecretStr | None = None
    domain_base_url: str = "http://127.0.0.1:8080"
    domain_token: SecretStr = SecretStr("surakshasetu-dev-domain-token")
    # The domain tier's internal scope (Step 16): only the ops kill-switch endpoint sends it, as
    # X-Internal-Token next to the service token.
    domain_internal_token: SecretStr = SecretStr("surakshasetu-dev-domain-internal-token")
    # Internal endpoints (Step 16): one API key per role, sent as a bearer. SSO in prod.
    ops_api_key: SecretStr = SecretStr("surakshasetu-dev-ops-key")
    compliance_api_key: SecretStr = SecretStr("surakshasetu-dev-compliance-key")
    advisor_api_key: SecretStr = SecretStr("surakshasetu-dev-advisor-key")  # Step 17: hand-offs
    # Subject keys (Step 4). The dev KEK is 32 public bytes; the DSN is the compose dummy.
    kek_b64: SecretStr = SecretStr("c3VyYWtzaGFzZXR1LWRldi1rZWstbm90LXNlY3JldCE=")
    pg_dsn_keyvault: SecretStr = SecretStr(
        "postgresql://keyvault_rw:surakshasetu-dev-keyvault-rw@127.0.0.1:5432/surakshasetu"
    )
    # Audit anchoring (Step 4): compose's MinIO dummies. With no TSA key file, dev and test sign
    # with a key derived from a public constant.
    minio_endpoint: str = "http://127.0.0.1:9000"
    minio_access_key: SecretStr = SecretStr("surakshasetu")
    minio_secret_key: SecretStr = SecretStr("surakshasetu-dev-minio")
    tsa_key_path: Path | None = None
    anchor_retention_days: int = Field(default=1, ge=1)
    # Model path (Steps 8-9). Chat routes go to the gateway: compose's hardened OmniRoute
    # (`make gateway-up`), which fronts the stubs locally. embed and rerank go straight to the
    # self-hosted TEI services, never through the gateway.
    gateway_base_url: str = "http://127.0.0.1:20130/v1"
    gateway_api_key: SecretStr = SecretStr("surakshasetu-dev-gateway-key")
    tei_embed_url: str = "http://127.0.0.1:8081"
    tei_rerank_url: str = "http://127.0.0.1:8082"
    # Bound to the embedding model in infra/compose.yaml: its vector size, and the instruction an
    # instruction-tuned model needs in front of queries (never documents). Empty for a symmetric
    # model.
    embed_dim: int = Field(default=1024, ge=1)
    embed_query_prefix: str = ""
    # Dev and test only: multiplies the embed and rerank budgets, because CPU TEI misses the TDD's
    # GPU budgets (a 40-candidate rerank takes 20-40 s on CPU). Pilot and prod require exactly 1.
    tei_timeout_scale: float = Field(default=1.0, ge=1)
    # Knowledge base (Step 11): the three collections, written by ingestion, read by retrieval.
    qdrant_url: str = "http://127.0.0.1:6333"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # Opt-in local extras: per-subsystem JSON files, and a readable console instead of JSON.
    log_dir: Path | None = None
    log_format: Literal["json", "text"] = "json"
    # Input rails (Step 10). The TDD/guide's global confidence bands: >=0.8 accept a slot outright,
    # 0.6-0.8 read it back to the customer, below 0.6 escalates after repeated misses.
    injection_score_threshold: float = Field(default=0.5, ge=0, le=1)
    normalise_token_cap: int = Field(default=500, ge=1)
    confidence_accept: float = Field(default=0.8, ge=0, le=1)
    confidence_readback_floor: float = Field(default=0.6, ge=0, le=1)
    # Output rails (Step 14). The lexicon pack under content/lexicon/ (picked at deploy time, not a
    # session pin; each lexicon verdict records it), and the share of gen-converse (S1-S2) turns
    # whose cited sentences go to verify-claims. gen-recommend (S3, side-queries) always does.
    output_lexicon: str = Field(default="2026.10.1", pattern=r"^\d{4}\.\d{2}\.\d+$")
    verify_sample_rate: float = Field(default=0.1, ge=0, le=1)
    # Transition rows (Step 15; surakshasetu.fsm.rows.Thresholds): the S2 -> S3 sufficiency gate
    # (TDD §3.7), the S3 -> S2 re-discovery loops before Human Escalation (TDD §3.8), and the
    # consecutive low-confidence turns that escalate (CC2). Deploy-time config, not session pins.
    profile_sufficiency_min: float = Field(default=0.7, ge=0, le=1)
    rediscovery_loop_limit: int = Field(default=2, ge=1)
    low_confidence_streak_limit: int = Field(default=2, ge=1)
    # Runtime (Step 16). The prompt bundle new sessions pin (deploy-time, like the lexicon pack);
    # session TTL (TDD §4.4, D7 open); the single-writer lock and idempotency TTLs (TDD §7.2); turn
    # rate limits per subject as {window seconds: max turns}; the side-query stack depth (TDD §2.6);
    # injection hits before HE_INJECTION (TDD §3.9); the app_rw pool size per process.
    prompt_bundle: str = Field(default="pb-2026.10.8", pattern=r"^pb-\d{4}\.\d{2}\.\d+$")
    session_ttl_days: int = Field(default=30, ge=1)
    session_lock_ttl_s: int = Field(default=30, ge=1)
    idempotency_ttl_s: int = Field(default=86_400, ge=1)
    rate_limits: dict[int, int] = Field(default_factory=lambda: {60: 20, 3600: 200})
    side_query_max_stack: int = Field(default=2, ge=0)
    injection_hit_limit: int = Field(default=3, ge=1)
    pg_pool_max: int = Field(default=20, ge=1)
    # State-1 (Step 19): answers to one question the system could not use before the advisor offer
    # (TDD §3.9's "invalid input" row).
    invalid_input_limit: int = Field(default=3, ge=1)
    # Cross-cutting completion (Step 22). Consecutive side queries before the offer to continue or
    # hand off (TDD §2.6); the same objection's count at which the handler stops answering and
    # offers Pause or Exit (TDD §3.9: the second); inactivity per state before CC3 pauses a
    # post-consent session (seconds; TDD §3.10 names only S4's 10 minutes, so 10 minutes each until
    # the specification owner sets them), and how often the timer job looks (jobs/timers.py).
    side_query_offer_after: int = Field(default=5, ge=1)
    objection_repeat_limit: int = Field(default=2, ge=2)
    inactivity_timeouts: dict[str, int] = Field(
        default_factory=lambda: dict.fromkeys(("S0", "S1", "QUOTE_ONLY", "S2", "S3"), 600)
    )
    timer_interval_s: int = Field(default=60, ge=1)
    # Evaluation (Step 23): each chat route's primary model, filled in when D2 provisions routes. A
    # call served by another model is a fallback (TDD §5.2 fallback <= 2%); empty, the live gate
    # reports the rate as unmeasured and fails. Eval only, never read by a turn.
    eval_primary_models: dict[str, str] = Field(default_factory=dict)
    # Cross-cutting handlers (Step 17). Erasure hard-deletes live conv and checkpoint rows as
    # erasure_rw, never app_rw. A subject key is destroyed when the longest applicable retention
    # ends (TDD §4.4): the audit hot store's proposed 13 months, as days that always cover 13
    # calendar months (D7 open). A closed advisor queue still queues the hand-off, and the customer
    # gets contact options instead of the hand-off script.
    pg_dsn_erasure: SecretStr = SecretStr(
        "postgresql://erasure_rw:surakshasetu-dev-erasure-rw@127.0.0.1:5432/surakshasetu"
    )
    key_retention_days: int = Field(default=397, ge=1)
    advisor_queue_open: bool = True
    # The session dossier (Step 24, TDD §4.3) reads the audit and consent hot store as
    # compliance_ro; payloads come through the key service (pg_dsn_keyvault and the KEK).
    pg_dsn_compliance: SecretStr = SecretStr(
        "postgresql://compliance_ro:surakshasetu-dev-compliance-ro@127.0.0.1:5432/surakshasetu"
    )
    # The hand-off adapter (Step 21, TDD §7.1): the Ed25519 key that signs the intake payload (KMS
    # in prod; the dev default is 32 public bytes, whose public key the stub journey holds), and the
    # application journey's intake endpoint (locally the stubs' receiver).
    intake_signing_key_b64: SecretStr = SecretStr("c3VyYWtzaGFzZXR1LWRldi1pbnRha2Utc2lnbmluZyE=")
    journey_url: str = "http://127.0.0.1:8090/journey/intake"

    @model_validator(mode="after")
    def _require_dependencies_outside_dev(self) -> Self:
        if self.env in ("pilot", "prod"):
            missing = [
                f"{ENV_PREFIX}{name.upper()}"
                for name in REQUIRED_OUTSIDE_DEV
                if name not in self.model_fields_set or getattr(self, name) is None
            ]
            if missing:
                raise ValueError(f"env={self.env} requires {', '.join(missing)}")
            if self.tei_timeout_scale != 1:
                raise ValueError(f"env={self.env} requires {ENV_PREFIX}TEI_TIMEOUT_SCALE=1")
        if unknown := set(self.inactivity_timeouts) - {"S0", "S1", "QUOTE_ONLY", "S2", "S3"}:
            raise ValueError(f"inactivity_timeouts names states no timer pauses: {sorted(unknown)}")
        if any(seconds < 60 for seconds in self.inactivity_timeouts.values()):
            raise ValueError("an inactivity timeout is at least 60 seconds")
        return self


def load_settings() -> Settings:
    """Load settings, failing fast with one readable line per problem."""
    try:
        return Settings()
    except ValidationError as exc:
        problems = "\n".join(
            f"  {_variable(err['loc'])}: {err['msg']}"
            for err in exc.errors(include_url=False, include_input=False)
        )
        raise ConfigError(f"invalid configuration:\n{problems}") from None
    except SettingsError as exc:  # a complex value (dict, list) that is not JSON
        field = re.search(r'field "(\w+)"', str(exc))
        name = _variable((field.group(1),)) if field else "settings"
        raise ConfigError(f"invalid configuration:\n  {name}: not valid JSON") from None


def _variable(loc: tuple[int | str, ...]) -> str:
    """Map a pydantic error location to the environment variable a human would set."""
    return f"{ENV_PREFIX}{str(loc[0]).upper()}" if loc else "settings"
