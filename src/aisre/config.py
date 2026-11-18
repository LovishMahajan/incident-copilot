"""Artifact 13.1 (Q13.11): typed settings, read from the environment.

Configuration lives in the repository and is reviewed; values come from the process
environment. Locally that is a `.env` copied from `.env.example` (fake values only), loaded
by Compose or by `uv run --env-file .env ...`; this module reads `os.environ` and nothing
else. Production credentials come from a secrets manager, injected per process, and only the
process named in Artifact 6 gets each one.

Database URLs carry passwords, so they are `SecretStr`: they don't appear in a repr or a log.
Each process reads only the URL for its own role (ADR-51): API and worker `aisre_app`, the
dispatcher `aisre_dispatcher`, the migration job `aisre_owner`.

Task queue names are not settings. Workflow code routes activities by them, so changing one
is a code change in `aisre.workflow.activities`; they are repeated here, without importing
`temporalio`, because `aisre.api` reads this module and must never reach `temporalio`
(Artifact 13.1). A unit test keeps the two copies equal.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

Environment = Literal["development", "test", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

# Settings field -> environment variable.
ENV_VARS: dict[str, str] = {
    "env": "AISRE_ENV",
    "database_url": "AISRE_DATABASE_URL",
    "dispatcher_database_url": "AISRE_DISPATCHER_DATABASE_URL",
    "migration_database_url": "AISRE_MIGRATION_DATABASE_URL",
    "temporal_address": "AISRE_TEMPORAL_ADDRESS",
    "temporal_namespace": "AISRE_TEMPORAL_NAMESPACE",
    "log_level": "AISRE_LOG_LEVEL",
    "scripted_skills": "AISRE_SCRIPTED_SKILLS",
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
    temporal_address: str = Field(default="127.0.0.1:7233", min_length=1)
    temporal_namespace: str = Field(default="default", min_length=1)
    log_level: LogLevel = "INFO"
    # M0 (T0.11): the worker answers the skill activities with aisre.workflow.scripted's fixed
    # results, so an incident runs end to end with no model. Refused in production.
    scripted_skills: bool = False

    # Fixed: equal to aisre.workflow.activities.INVESTIGATION_QUEUE and DISPATCHER_QUEUE.
    investigation_task_queue: ClassVar[str] = "aisre-investigation"
    dispatcher_task_queue: ClassVar[str] = "aisre-dispatcher"

    @model_validator(mode="after")
    def no_scripts_in_production(self) -> Settings:
        if self.scripted_skills and self.env == "production":
            raise ValueError("AISRE_SCRIPTED_SKILLS is for development and test, never production")
        return self

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Build from `environ` (default `os.environ`). An empty variable counts as unset."""
        source = os.environ if environ is None else environ
        values = {field: source[var] for field, var in ENV_VARS.items() if source.get(var)}
        if "log_level" in values:
            values["log_level"] = values["log_level"].upper()
        return cls.model_validate(values)
