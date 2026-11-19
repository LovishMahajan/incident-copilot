"""Artifact 13.1 (Q13.11): typed settings, read from the environment.

Configuration lives in the repository and is reviewed; values come from the process
environment. Locally that is a `.env` copied from `.env.example` (fake values only), loaded
by Compose or by `uv run --env-file .env ...`; this module reads `os.environ` and nothing
else. Production credentials come from a secrets manager, injected per process, and only the
process named in Artifact 6 gets each one.

Database URLs carry passwords, so they are `SecretStr`: they don't appear in a repr or a log.
Each process reads only the URL for its own role (ADR-51): API and worker `aisre_app`, the
dispatcher `aisre_dispatcher`, the migration job `aisre_owner`, the retention job
`aisre_retention` and the pilot reports `aisre_reporting` (read-only, ADR-75).

Every setting the processes read is here, under its environment variable's name, including
OpenTelemetry's own `OTEL_EXPORTER_OTLP_ENDPOINT`; the CLI's `AISRE_API_URL` and `AISRE_TOKEN`
belong to a person's shell, not a process, and stay in aisre.cli.

Task queue names are not settings. Workflow code routes activities by them, so changing one
is a code change in `aisre.workflow.activities`; they are repeated here, without importing
`temporalio`, because `aisre.api` reads this module and must never reach `temporalio`
(Artifact 13.1). A unit test keeps the two copies equal.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, Json, SecretStr, model_validator

Environment = Literal["development", "test", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

# Settings field -> environment variable.
ENV_VARS: dict[str, str] = {
    "env": "AISRE_ENV",
    "database_url": "AISRE_DATABASE_URL",
    "dispatcher_database_url": "AISRE_DISPATCHER_DATABASE_URL",
    "migration_database_url": "AISRE_MIGRATION_DATABASE_URL",
    "retention_database_url": "AISRE_RETENTION_DATABASE_URL",
    "reporting_database_url": "AISRE_REPORTING_DATABASE_URL",
    "temporal_address": "AISRE_TEMPORAL_ADDRESS",
    "temporal_namespace": "AISRE_TEMPORAL_NAMESPACE",
    "log_level": "AISRE_LOG_LEVEL",
    "scripted_skills": "AISRE_SCRIPTED_SKILLS",
    "scripted_step_seconds": "AISRE_SCRIPTED_STEP_SECONDS",
    "oidc_issuer": "AISRE_OIDC_ISSUER",
    "oidc_audience": "AISRE_OIDC_AUDIENCE",
    "oidc_jwks_url": "AISRE_OIDC_JWKS_URL",
    "evidence_sources": "AISRE_EVIDENCE_SOURCES",
    "object_store_url": "AISRE_OBJECT_STORE_URL",
    "object_store_bucket": "AISRE_OBJECT_STORE_BUCKET",
    "object_store_access_key": "AISRE_OBJECT_STORE_ACCESS_KEY",
    "object_store_secret_key": "AISRE_OBJECT_STORE_SECRET_KEY",
    "audit_store_url": "AISRE_AUDIT_STORE_URL",
    "audit_store_bucket": "AISRE_AUDIT_STORE_BUCKET",
    "audit_store_access_key": "AISRE_AUDIT_STORE_ACCESS_KEY",
    "audit_store_secret_key": "AISRE_AUDIT_STORE_SECRET_KEY",
    "audit_store_lock_days": "AISRE_AUDIT_STORE_LOCK_DAYS",
    "export_signing_key": "AISRE_EXPORT_SIGNING_KEY",
    "provider_url": "AISRE_PROVIDER_URL",
    "provider_api_key": "AISRE_PROVIDER_API_KEY",
    "prompts_dir": "AISRE_PROMPTS_DIR",
    "otlp_endpoint": "OTEL_EXPORTER_OTLP_ENDPOINT",
    "metrics_port": "AISRE_METRICS_PORT",
    "sandbox_runtime": "AISRE_SANDBOX_RUNTIME",
    "sandbox_image": "AISRE_SANDBOX_IMAGE",
    "sandbox_repos": "AISRE_SANDBOX_REPOS",
    "review_adapter": "AISRE_REVIEW_ADAPTER",
    "review_url": "AISRE_REVIEW_URL",
    "review_token": "AISRE_REVIEW_TOKEN",
    "release_adapter": "AISRE_RELEASE_ADAPTER",
    "release_url": "AISRE_RELEASE_URL",
    "release_token": "AISRE_RELEASE_TOKEN",
    "approval_signing_key": "AISRE_APPROVAL_SIGNING_KEY",
    "approval_verify_key": "AISRE_APPROVAL_VERIFY_KEY",
    "identity_directory_url": "AISRE_IDENTITY_DIRECTORY_URL",
    "verifier_monitoring_url": "AISRE_VERIFIER_MONITORING_URL",
    "post_change_watch_seconds": "AISRE_POST_CHANGE_WATCH_SECONDS",
}


class Settings(BaseModel):
    """One process's settings. Defaults suit a local development stack and hold no secret."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    env: Environment = "development"
    database_url: SecretStr = SecretStr("postgresql+psycopg://aisre_app@127.0.0.1:5432/aisre")
    dispatcher_database_url: SecretStr = SecretStr(
        "postgresql+psycopg://aisre_dispatcher@127.0.0.1:5432/aisre"
    )
    migration_database_url: SecretStr = SecretStr(
        "postgresql+psycopg://aisre_owner@127.0.0.1:5432/aisre"
    )
    retention_database_url: SecretStr | None = None  # T4.8: the retention job, as aisre_retention
    reporting_database_url: SecretStr | None = None  # M5: the pilot reports, as aisre_reporting
    temporal_address: str = Field(default="127.0.0.1:7233", min_length=1)
    temporal_namespace: str = Field(default="default", min_length=1)
    log_level: LogLevel = "INFO"
    # M0 (T0.11): the worker answers the skill activities with aisre.workflow.scripted's fixed
    # results, so an incident runs end to end with no model. Refused in production.
    scripted_skills: bool = False
    # Demo pacing for the scripted skill steps, so a worker kill lands mid-run (T0.12).
    scripted_step_seconds: float = Field(default=0.0, ge=0, le=10)
    # Bearer-token sign-in (T1.3, ADR-49): the identity system's `iss`, the API's audience and
    # the issuer's JWK set. All three or none. With none, the API takes the development caller
    # header, and only in development and test (aisre.api.auth).
    oidc_issuer: str | None = Field(default=None, min_length=1)
    oidc_audience: str | None = Field(default=None, min_length=1)
    oidc_jwks_url: str | None = Field(default=None, pattern=r"^https?://")
    # Evidence (T1.10, T1.17): JSON for aisre.evidence.sources.SourceSet.from_config, naming the
    # adapter answering each READ tool, platform-wide and per service or service and environment
    # (ADR-75). Unset: no real Evidence Collector or Knowledge Retriever.
    evidence_sources: Json[dict[str, Any]] | None = None
    # Evidence snapshots in the object store (ADR-57). All four or none.
    object_store_url: str | None = Field(default=None, pattern=r"^https?://")
    object_store_bucket: str | None = Field(default=None, min_length=1)
    object_store_access_key: str | None = Field(default=None, min_length=1)
    object_store_secret_key: SecretStr | None = None
    # The write-once audit copy (T1.20). With the URL set, bucket and both keys are required;
    # lock_days sets S3 Object Lock retention, COMPLIANCE mode, on every object.
    audit_store_url: str | None = Field(default=None, pattern=r"^https?://")
    audit_store_bucket: str | None = Field(default=None, min_length=1)
    audit_store_access_key: str | None = Field(default=None, min_length=1)
    audit_store_secret_key: SecretStr | None = None
    audit_store_lock_days: int | None = Field(default=None, ge=1)
    # Ed25519 seed, base64, that signs exports. Required in production (aisre.api.export).
    export_signing_key: SecretStr | None = None
    # The Model Gateway (T1.8, T1.18): the provider's key, and its base URL (unset: the
    # provider's own). With a key, the worker's hypothesise, diagnose and review_diagnosis are the
    # real skills, ahead of any script. Locally, the URL is Compose's fake provider.
    provider_url: str | None = Field(default=None, pattern=r"^https?://")
    provider_api_key: SecretStr | None = None
    # prompts/<task>/v<N>.md (ADR-63), relative to the working directory: /app in the image.
    prompts_dir: str = Field(default="prompts", min_length=1)
    # Telemetry (T1.19): the collector's OTLP/HTTP endpoint and the /metrics port. Unset: off.
    otlp_endpoint: str | None = Field(default=None, pattern=r"^https?://")
    metrics_port: int | None = Field(default=None, ge=0, le=65535)
    # The Test Runner (M2, ADR-71): the sandbox runtime, `runsc` (gVisor), or `runc` only in
    # development and test; the sandbox image by digest (ops/sandbox/build.sh); and JSON naming
    # each repository's local git mirror, which the checkout is read from.
    sandbox_runtime: Literal["runsc", "runc"] = "runsc"
    sandbox_image: str | None = Field(
        default=None, pattern=r"^([\w./:-]+@sha256:[0-9a-f]{64}|sha256:[0-9a-f]{64})$"
    )
    sandbox_repos: Json[dict[str, str]] | None = None
    # The review system (M2, T2.8, ADR-72): where draft pull requests are opened and where the
    # Patch Proposer reads the repository at its base revision, with the worker's
    # draft-pull-request credential (Artifact 11.7). `fake_review` is Compose's fake
    # (ops/fakes/review.py), refused in production like the other fakes. Unset: no real
    # draft_patch, and no pull request.
    review_adapter: Literal["fake_review"] | None = None
    review_url: str | None = Field(default=None, pattern=r"^https?://")
    review_token: SecretStr | None = None
    # The release process (M3, T3.1, ADR-73): `fake_release` is Compose's fake
    # (ops/fakes/release.py), the only adapter, refused in production: no production write
    # credential exists (ADR-44). On the worker the adapter alone turns on the approval path; the
    # URL and the token, the release credential, are the dispatcher process's only.
    release_adapter: Literal["fake_release"] | None = None
    release_url: str | None = Field(default=None, pattern=r"^https?://")
    release_token: SecretStr | None = None
    # Approvals (M3, T3.2, T3.3, ADR-73). No secrets manager is chosen, so the Approval Service
    # signs with a DEVELOPMENT Ed25519 key from settings (32-byte seed, base64; the API only),
    # and the dispatcher holds its public key (base64). Both refused outside development and
    # test. The identity directory is the fake issuer's (ops/fakes/token_issuer.py), which the
    # dispatcher asks for an approver's current groups (Q11.59); refused in production.
    approval_signing_key: SecretStr | None = None
    approval_verify_key: str | None = Field(default=None, min_length=1)
    identity_directory_url: str | None = Field(default=None, pattern=r"^https?://")
    # The Recovery Verifier (M3, T3.8, T3.17, ADR-66, ADR-73): the monitoring source it reads
    # directly, never the dispatcher's word (FR-31). Here, Compose's FAKE monitoring source,
    # whose live metrics follow what the fake release process runs; refused in production. The
    # post-change watch after the hold period is 60 minutes (provisional, 11.12); tests and the
    # demo shorten it.
    verifier_monitoring_url: str | None = Field(default=None, pattern=r"^https?://")
    post_change_watch_seconds: int = Field(default=3600, ge=60, le=86400)

    # Fixed: equal to aisre.workflow.activities.INVESTIGATION_QUEUE, DISPATCHER_QUEUE and
    # TEST_RUNNER_QUEUE.
    investigation_task_queue: ClassVar[str] = "aisre-investigation"
    dispatcher_task_queue: ClassVar[str] = "aisre-dispatcher"
    test_runner_task_queue: ClassVar[str] = "aisre-test-runner"

    @model_validator(mode="after")
    def no_scripts_in_production(self) -> Settings:
        if self.scripted_skills and self.env == "production":
            raise ValueError("AISRE_SCRIPTED_SKILLS is for development and test, never production")
        if self.sandbox_runtime == "runc" and self.env not in ("development", "test"):
            raise ValueError("AISRE_SANDBOX_RUNTIME=runc is for development and test (ADR-71)")
        if self.review_adapter == "fake_review" and self.env == "production":
            raise ValueError("AISRE_REVIEW_ADAPTER=fake_review is for development and test")
        if (self.review_adapter is None) != (self.review_url is None) or (
            self.review_adapter is not None and self.review_token is None
        ):
            raise ValueError("AISRE_REVIEW_ADAPTER, _URL and _TOKEN go together")
        if self.release_adapter is not None and self.env == "production":
            raise ValueError("AISRE_RELEASE_ADAPTER=fake_release is for development and test")
        if (self.release_url is None) != (self.release_token is None) or (
            self.release_url is not None and self.release_adapter is None
        ):
            raise ValueError("AISRE_RELEASE_URL and _TOKEN go together, with AISRE_RELEASE_ADAPTER")
        dev_keys = self.approval_signing_key is not None or self.approval_verify_key is not None
        if dev_keys and self.env not in ("development", "test"):
            raise ValueError(
                "AISRE_APPROVAL_SIGNING_KEY and _VERIFY_KEY are development keys (ADR-73): "
                "development and test only"
            )
        if self.identity_directory_url is not None and self.env == "production":
            raise ValueError("AISRE_IDENTITY_DIRECTORY_URL is the fake issuer's: not production")
        if self.verifier_monitoring_url is not None and self.env == "production":
            raise ValueError("AISRE_VERIFIER_MONITORING_URL is the fake monitoring source's")
        return self

    @model_validator(mode="after")
    def oidc_all_or_none(self) -> Settings:
        given = [self.oidc_issuer, self.oidc_audience, self.oidc_jwks_url]
        if any(given) and not all(given):
            raise ValueError("AISRE_OIDC_ISSUER, _AUDIENCE and _JWKS_URL go together")
        if self.env == "production" and (self.oidc_jwks_url or "").startswith("http://"):
            raise ValueError("AISRE_OIDC_JWKS_URL must be https in production")
        return self

    @model_validator(mode="after")
    def stores_complete(self) -> Settings:
        objects = [
            self.object_store_url,
            self.object_store_bucket,
            self.object_store_access_key,
            self.object_store_secret_key,
        ]
        if any(objects) and not all(objects):
            raise ValueError(
                "AISRE_OBJECT_STORE_URL, _BUCKET, _ACCESS_KEY and _SECRET_KEY go together"
            )
        if self.audit_store_url:
            missing = [
                ENV_VARS[f]
                for f in ("audit_store_bucket", "audit_store_access_key", "audit_store_secret_key")
                if not getattr(self, f)
            ]
            if missing:
                raise ValueError(f"audit store: set {', '.join(missing)}")
        return self

    @property
    def token_sign_in(self) -> bool:
        """True when a token verifier is configured (the dev caller header is then refused)."""
        return self.oidc_jwks_url is not None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Build from `environ` (default `os.environ`). An empty variable counts as unset."""
        source = os.environ if environ is None else environ
        values = {field: source[var] for field, var in ENV_VARS.items() if source.get(var)}
        if "log_level" in values:
            values["log_level"] = values["log_level"].upper()
        return cls.model_validate(values)
