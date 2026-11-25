"""Artifact 9.4, 13.5 (M5, ADR-75): Amazon CloudWatch for metrics and logs, read-only.

Two documented AWS APIs, through boto3 (the optional `aws` extra: `uv sync --extra aws`; the
default install doesn't need it, and configuring this adapter without it fails at start):

- `get_metric`: CloudWatch GetMetricData (API version 2010-08-01,
  https://docs.aws.amazon.com/AmazonCloudWatch/latest/APIReference/API_GetMetricData.html),
  one metric math expression per query, one point a `period_s`;
- `get_logs`: CloudWatch Logs FilterLogEvents (API version 2014-03-28,
  https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_FilterLogEvents.html),
  synchronous and paged, rather than StartQuery and polling GetQueryResults.

Everything else raises SourceUnavailable: a gap (ADR-54).

Credentials come from the standard AWS chain (environment, shared files, web identity, container
or instance role: https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html),
optionally a named `profile`; none is ever in the configuration. The role needs only
`cloudwatch:GetMetricData` and `logs:FilterLogEvents` on the service's log groups.

Expressions, the log group and the filter pattern are configuration with `$service` and
`$environment`, put in only if they are plain values (letters, digits, `_ - . /`), else the call
is a gap. `error_rate_by_version` is one expression returning a series per version (a SEARCH),
each labelled with its version through a dynamic label on the `version_dimension`. A log message
is parsed as JSON when it is JSON; the level and `contains` are matched here, and any field naming
another service or environment drops the line (FR-44).

Bounds (Artifact 9.4): the call's window, `limit` rows (FilterLogEvents' own `limit`, one page,
partial when there is a next token; GetMetricData's MaxDatapoints), botocore's connect and read
timeouts at `timeout_s` with its own retries off (the collector retries and holds the breaker).
Tested against responses stubbed with botocore's Stubber, which checks each request and response
against the service model (tests/unit/test_vendor_sources.py); never against a real account.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from aisre.evidence.sources import (
    DEFAULT_TIMEOUT_S,
    Record,
    SourceResult,
    SourceUnavailable,
    log_record,
    metric_queries,
    metric_records,
    substitute,
    utc_text,
    wanted_levels,
)

_PLAIN = re.compile(r"^[A-Za-z0-9_./-]{1,200}$")


def quote_plain(value: str) -> str:
    if not _PLAIN.match(value):
        raise SourceUnavailable("a value that is not plain can't go in a CloudWatch query")
    return value


class CloudWatchSource:
    """`get_metric` and `get_logs` from one AWS account and region."""

    def __init__(
        self,
        region: str,
        metrics: Mapping[str, Any] | None = None,
        log_group: str | None = None,
        filter_pattern: str | None = None,
        version_dimension: str = "version",
        profile: str | None = None,
        period_s: int = 60,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        name: str = "cloudwatch",
    ) -> None:
        if importlib.util.find_spec("boto3") is None:
            raise ValueError(f"{name}: needs boto3, the `aws` extra (uv sync --extra aws)")
        if not re.match(r"^[a-z]{2}(-[a-z]+)+-\d$", region):
            raise ValueError(f"{name}: region {region!r} is not an AWS region name")
        if log_group is not None and "$service" not in log_group and filter_pattern is None:
            raise ValueError(f"{name}: select the service with $service in log_group or filter")
        self.name, self.region, self.profile = name, region, profile
        self.metrics = metric_queries(metrics or {}, name)
        self.log_group, self.filter_pattern = log_group, filter_pattern
        self.version_dimension, self.period_s, self.timeout_s = (
            version_dimension,
            period_s,
            timeout_s,
        )
        self.credentials = ()  # the AWS chain: nothing by reference here
        self._clients: dict[str, Any] = {}

    def client(self, service: str) -> Any:
        """A boto3 client, built on first use: building one reaches nothing."""
        if service not in self._clients:
            boto3 = importlib.import_module("boto3")
            config = importlib.import_module("botocore.config").Config(
                connect_timeout=self.timeout_s,
                read_timeout=self.timeout_s,
                retries={"total_max_attempts": 1},
            )
            session = boto3.Session(profile_name=self.profile, region_name=self.region)
            self._clients[service] = session.client(service, config=config)
        return self._clients[service]

    def _aws(self, service: str, op: str, **kwargs: Any) -> Any:
        exceptions = importlib.import_module("botocore.exceptions")
        try:
            return getattr(self.client(service), op)(**kwargs)
        except exceptions.ClientError as e:
            code = (getattr(e, "response", None) or {}).get("Error", {}).get("Code", "error")
            raise SourceUnavailable(f"{self.name} {op}: {code}") from None
        except (exceptions.BotoCoreError, OSError) as e:
            raise SourceUnavailable(f"{self.name} {op}: {type(e).__name__}") from None

    def fetch(self, tool: str, params: BaseModel, environment: str) -> SourceResult:
        p: Any = params
        if tool == "get_metric":
            return self._metric(p, environment)
        if tool == "get_logs" and self.log_group is not None:
            return self._logs(p, environment)
        raise SourceUnavailable(f"{self.name} has no {tool}")

    def _link(self, kind: str) -> str:
        return f"https://{self.region}.console.aws.amazon.com/cloudwatch/home?region={self.region}#{kind}"

    def _metric(self, p: Any, environment: str) -> SourceResult:
        spec = self.metrics.get(p.metric)
        if spec is None:
            raise SourceUnavailable(f"{self.name}: no query configured for {p.metric}")
        by_version = p.metric == "error_rate_by_version"
        queries = []
        for qid, expr in sorted(spec.items()):
            q: dict[str, Any] = {
                "Id": qid,
                "Expression": substitute(
                    expr, quote_plain, service=p.service, environment=environment
                ),
                "Period": self.period_s,
                "ReturnData": True,
            }
            if by_version:
                q["Label"] = "${PROP('Dim." + self.version_dimension + "')}"
            queries.append(q)
        found = self._aws(
            "cloudwatch",
            "get_metric_data",
            MetricDataQueries=queries,
            StartTime=p.start,
            EndTime=p.end,
            ScanBy="TimestampAscending",
            MaxDatapoints=max(p.limit * 50, 1000),
        )
        series: dict[str, dict[str, dict[str, float | None]]] = {"value": {}, "requests": {}}
        partial = bool(found.get("NextToken"))
        for r in found.get("MetricDataResults") or []:
            partial |= r.get("StatusCode") != "Complete"
            key = str(r.get("Label") or "") if by_version else ""
            if by_version and not key:
                continue
            points = series.setdefault(str(r.get("Id")), {}).setdefault(key, {})
            for t, v in zip(r.get("Timestamps") or [], r.get("Values") or [], strict=False):
                points[utc_text(t)] = float(v)
        records = metric_records(p.metric, series["value"], series["requests"])
        return SourceResult(
            f"{self.name}:metric",
            self._link("metricsV2"),
            records[: p.limit],
            partial=partial or len(records) > p.limit,
        )

    def _logs(self, p: Any, environment: str) -> SourceResult:
        assert self.log_group is not None
        args: dict[str, Any] = {
            "logGroupName": substitute(
                self.log_group, quote_plain, service=p.service, environment=environment
            ),
            "startTime": int(p.start.timestamp() * 1000),
            "endTime": int(p.end.timestamp() * 1000),
            "limit": p.limit,
        }
        if self.filter_pattern:
            args["filterPattern"] = substitute(
                self.filter_pattern, quote_plain, service=p.service, environment=environment
            )
        found = self._aws("logs", "filter_log_events", **args)
        levels = wanted_levels(p.level)
        records: list[Record] = []
        for e in found.get("events") or []:
            line = str(e.get("message") or "")
            try:
                parsed = json.loads(line)
            except ValueError:
                parsed = None
            fields = parsed if isinstance(parsed, dict) else {}
            if (
                fields.get("service", p.service) != p.service
                or fields.get("environment", environment) != environment
            ):
                continue
            ts = utc_text(datetime.fromtimestamp(int(e.get("timestamp") or 0) / 1000, UTC))
            rec = log_record({"instance": e.get("logStreamName"), **fields}, ts, line)
            if rec["level"] not in levels:
                continue
            if p.contains and p.contains.lower() not in line.lower():
                continue
            records.append(rec)
        return SourceResult(
            f"{self.name}:logs",
            self._link("logsV2:log-groups"),
            records[: p.limit],
            partial=bool(found.get("nextToken")) or len(records) > p.limit,
        )
