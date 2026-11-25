"""Artifact 9.4, 13.5 (M5, ADR-75): Amazon CloudWatch, X-Ray and CloudWatch Logs, read-only.

Documented AWS APIs, through boto3 (the optional `aws` extra: `uv sync --extra aws`; the default
install doesn't need it, and configuring this adapter without it fails at start):

- `get_metric`: CloudWatch GetMetricData (API version 2010-08-01,
  https://docs.aws.amazon.com/AmazonCloudWatch/latest/APIReference/API_GetMetricData.html),
  one metric math expression per query, one point a `period_s`;
- `get_logs`: CloudWatch Logs FilterLogEvents (API version 2014-03-28,
  https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_FilterLogEvents.html),
  synchronous and paged, rather than StartQuery and polling GetQueryResults;
- `get_alert`: CloudWatch DescribeAlarms (2010-08-01,
  https://docs.aws.amazon.com/AmazonCloudWatch/latest/APIReference/API_DescribeAlarms.html),
  metric and composite alarms in ALARM whose dimensions (`alarm_dimensions`, on the alarm or a
  metric it reads) name the service, or whose name starts with `alarm_name_prefix`, and, if
  `alarm_tags` is set, whose tags match (ListTagsForResource, one call per alarm, at most
  MAX_ALERTS). The value is the latest of the alarm's `recentDatapoints`, the breach start its
  earliest evaluated datapoint (StateReasonData), fired at the state transition;
- `get_traces`: X-Ray GetTraceSummaries and BatchGetTraces (API version 2016-04-12,
  https://docs.aws.amazon.com/xray/latest/api/API_GetTraceSummaries.html,
  https://docs.aws.amazon.com/xray/latest/api/API_BatchGetTraces.html): the faulted traces
  `traces_filter` selects (default `service("$service") { fault = true }`), up to `max_traces`
  (5, one BatchGetTraces call); each segment of the service and its subsegments become spans
  (https://docs.aws.amazon.com/xray/latest/devguide/xray-api-segmentdocuments.html), a fault is
  ERROR, a cause's exceptions are the span's and its first frame's path is `code.filepath`;
- `get_error_groups`: CloudWatch Logs Insights, StartQuery then GetQueryResults (2014-03-28,
  https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_StartQuery.html,
  https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_GetQueryResults.html),
  polled every `poll_s` until complete or `timeout_s` (a gap). Derived: `error_groups_query` is
  the owners' `stats count(*) as count, min(@timestamp) as first_seen, max(@timestamp) as
  last_seen, latest(...) as stack by type, message, version`, merged here by type and message;
- `get_dependency_health`: GetMetricData (above), each configured dependency's `error_rate` and
  `latency_p95` expression over `dependency_lookback_s` up to the call's `at`, latest point
  against its limits. Derived: CloudWatch has no notion of a dependency.

`get_changes` raises SourceUnavailable: CloudWatch holds no change history. A gap (ADR-54).

Credentials come from the standard AWS chain (environment, shared files, web identity, container
or instance role: https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html),
optionally a named `profile`; none is ever in the configuration. The role needs only
`cloudwatch:GetMetricData`, `cloudwatch:DescribeAlarms` (and `cloudwatch:ListTagsForResource`
with `alarm_tags`), `logs:FilterLogEvents`, `logs:StartQuery` and `logs:GetQueryResults` on the
service's log groups, `xray:GetTraceSummaries` and `xray:BatchGetTraces`.

Expressions, the log group, filters and queries are configuration with `$service`,
`$environment` and, for dependencies, `$dependency`, put in only if they are plain values
(letters, digits, `_ - . /`), else the call is a gap. `error_rate_by_version` is one expression
returning a series per version (a SEARCH), each labelled with its version through a dynamic
label on the `version_dimension`. A log message is parsed as JSON when it is JSON; the level and
`contains` are matched here, and any field naming another service or environment drops the line
(FR-44); so does an alarm dimension or a segment's environment annotation naming another.

Bounds (Artifact 9.4): the call's window, `limit` rows (FilterLogEvents' own `limit`, one page,
partial when there is a next token; GetMetricData's MaxDatapoints; MAX_ALERTS alarms;
`max_traces` traces), botocore's connect and read timeouts at `timeout_s` with its own retries
off (the collector retries and holds the breaker). Tested against responses stubbed with
botocore's Stubber, which checks each request and response against the service model
(tests/unit/test_vendor_sources.py and test_vendor_gaps.py); never against a real account.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import re
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel

from aisre.evidence.sources import (
    DEFAULT_TIMEOUT_S,
    MAX_ALERTS,
    MAX_TRACES,
    Record,
    SourceResult,
    SourceUnavailable,
    dependency_record,
    dependency_specs,
    error_group_records,
    frames_from,
    iso_ms,
    log_record,
    metric_queries,
    metric_records,
    parse_time,
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
        alarm_dimensions: Mapping[str, str] | None = None,
        alarm_name_prefix: str | None = None,
        alarm_tags: Mapping[str, str] | None = None,
        traces_filter: str | None = None,
        environment_annotation: str = "environment",
        max_traces: int = MAX_TRACES,
        error_groups_query: str | None = None,
        dependencies: Mapping[str, Any] | None = None,
        dependency_lookback_s: int = 300,
        poll_s: float = 1.0,
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
        dims = {"Service": "$service"} if alarm_dimensions is None else dict(alarm_dimensions)
        for what, m in (("alarm_dimensions", dims), ("alarm_tags", alarm_tags or {})):
            if not all(isinstance(k, str) and isinstance(v, str) for k, v in m.items()):
                raise ValueError(f"{name}: {what} is {{name: value}}")
        if not any("$service" in v for v in (*dims.values(), *(alarm_tags or {}).values())) and (
            alarm_name_prefix is None or "$service" not in alarm_name_prefix
        ):
            raise ValueError(f"{name}: select the service's alarms with $service")
        if traces_filter is not None and "$service" not in traces_filter:
            raise ValueError(f"{name}: traces_filter must select the service with $service")
        if error_groups_query is not None and (
            "stats" not in error_groups_query or log_group is None
        ):
            raise ValueError(f"{name}: error_groups_query is a `stats ... by` query on log_group")
        if not 1 <= max_traces <= 5:
            raise ValueError(f"{name}: max_traces is 1 to 5 (one BatchGetTraces call)")
        if not 60 <= dependency_lookback_s <= 3600:
            raise ValueError(f"{name}: dependency_lookback_s is 60 to 3600")
        self.alarm_dimensions, self.alarm_name_prefix = dims, alarm_name_prefix
        self.alarm_tags = dict(alarm_tags) if alarm_tags else None
        self.traces_filter = traces_filter or 'service("$service") { fault = true }'
        self.environment_annotation, self.max_traces = environment_annotation, max_traces
        self.error_groups_query = error_groups_query
        self.dependencies = (
            dependency_specs(dependencies, name) if dependencies is not None else None
        )
        self.dependency_lookback_s, self.poll_s = dependency_lookback_s, poll_s
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
        if tool == "get_alert":
            return self._alerts(p, environment)
        if tool == "get_traces":
            return self._traces(p, environment)
        if tool == "get_error_groups" and self.error_groups_query is not None:
            return self._error_groups(p, environment)
        if tool == "get_dependency_health" and self.dependencies is not None:
            return self._dependencies(p, environment)
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

    # -- get_alert: DescribeAlarms ------------------------------------------------------------
    def _alerts(self, p: Any, environment: str) -> SourceResult:
        """Alarms in ALARM for the service: by its dimensions (on the alarm or any metric it
        reads), or by name prefix, and if `alarm_tags` is set, by tags too (one
        ListTagsForResource each). An alarm with a configured dimension naming another
        service or environment is dropped (FR-44)."""

        def sub(v: str) -> str:
            return substitute(v, quote_plain, service=p.service, environment=environment)

        wanted_dims = {k: sub(v) for k, v in self.alarm_dimensions.items()}
        args: dict[str, Any] = {
            "StateValue": "ALARM",
            "AlarmTypes": ["MetricAlarm", "CompositeAlarm"],
            "MaxRecords": 100,
        }
        if self.alarm_name_prefix:
            args["AlarmNamePrefix"] = sub(self.alarm_name_prefix)
        found = self._aws("cloudwatch", "describe_alarms", **args)
        partial = bool(found.get("NextToken"))
        candidates: list[Record] = []
        for a in [*(found.get("MetricAlarms") or []), *(found.get("CompositeAlarms") or [])]:
            dims: dict[str, set[str]] = {}
            for d in [
                *(a.get("Dimensions") or []),
                *(
                    d
                    for m in a.get("Metrics") or []
                    for d in ((m.get("MetricStat") or {}).get("Metric") or {}).get("Dimensions")
                    or []
                ),
            ]:
                dims.setdefault(d["Name"], set()).add(d["Value"])
            if any(dims.get(k, {v}) != {v} for k, v in wanted_dims.items()):
                continue  # names another service or environment
            by_dims = all(dims.get(k) == {v} for k, v in wanted_dims.items())
            if not (by_dims or self.alarm_name_prefix):
                continue
            if p.alert_id is not None and p.alert_id not in (a.get("AlarmArn"), a.get("AlarmName")):
                continue
            candidates.append(a)
        if self.alarm_tags is not None:
            wanted_tags = {k: sub(v) for k, v in self.alarm_tags.items()}
            partial |= len(candidates) > MAX_ALERTS
            tagged = []
            for a in candidates[:MAX_ALERTS]:
                tags = self._aws("cloudwatch", "list_tags_for_resource", ResourceARN=a["AlarmArn"])
                have = {t["Key"]: t["Value"] for t in tags.get("Tags") or []}
                if all(have.get(k) == v for k, v in wanted_tags.items()):
                    tagged.append(a)
            candidates = tagged
        records = [self._alarm_record(a, p.service, environment) for a in candidates]
        records.sort(key=lambda r: str(r["fired_at"] or ""), reverse=True)
        return SourceResult(
            f"{self.name}:alert",
            self._link("alarmsV2:"),
            records[:MAX_ALERTS],
            partial=partial or len(records) > MAX_ALERTS,
        )

    def _alarm_record(self, a: Record, service: str, environment: str) -> Record:
        try:
            reason = json.loads(a.get("StateReasonData") or "{}")
        except ValueError:
            reason = {}
        recent = reason.get("recentDatapoints") or []
        evaluated = [
            parse_time(_aws_time(d["timestamp"]))
            for d in reason.get("evaluatedDatapoints") or []
            if d.get("timestamp")
        ]
        fired = a.get("StateTransitionedTimestamp") or a.get("StateUpdatedTimestamp")
        if "AlarmRule" in a:
            rule = f"composite: {a['AlarmRule']}"
        elif a.get("MetricName"):
            rule = (
                f"{a.get('Statistic') or a.get('ExtendedStatistic')} of {a.get('Namespace')} "
                f"{a['MetricName']} {a.get('ComparisonOperator')} {a.get('Threshold')} for "
                f"{a.get('EvaluationPeriods')} x {a.get('Period')} s"
            )
        else:
            exprs = [m.get("Expression") for m in a.get("Metrics") or [] if m.get("ReturnData")]
            rule = f"{'; '.join(str(e) for e in exprs)} {a.get('ComparisonOperator')} {a.get('Threshold')}"
        return {
            "alert_name": a.get("AlarmName"),
            "alert_id": a.get("AlarmArn"),
            "state": "firing",
            "service": service,
            "environment": environment,
            "rule": rule,
            "value": float(recent[-1]) if recent and recent[-1] is not None else None,
            "threshold": a.get("Threshold"),
            "breach_started_at": utc_text(min(evaluated)) if evaluated else None,
            "fired_at": utc_text(fired) if isinstance(fired, datetime) else None,
        }

    # -- get_traces: X-Ray ------------------------------------------------------------------
    def _traces(self, p: Any, environment: str) -> SourceResult:
        """GetTraceSummaries for the service's faulted traces in the window, then
        BatchGetTraces for up to `max_traces`; each segment and subsegment of the service is a
        span, a fault is ERROR, and a cause's exceptions are the span's."""
        flt = substitute(
            self.traces_filter, quote_plain, service=p.service, environment=environment
        )
        found = self._aws(
            "xray",
            "get_trace_summaries",
            StartTime=p.start,
            EndTime=p.end,
            Sampling=False,
            FilterExpression=flt,
        )
        ids = [t["Id"] for t in found.get("TraceSummaries") or [] if t.get("Id")]
        partial = bool(found.get("NextToken")) or len(ids) > self.max_traces
        records: list[Record] = []
        if ids:
            batch = self._aws("xray", "batch_get_traces", TraceIds=ids[: self.max_traces])
            partial |= bool(batch.get("UnprocessedTraceIds"))
            for t in batch.get("Traces") or []:
                spans: list[Record] = []
                for seg in t.get("Segments") or []:
                    try:
                        doc = json.loads(seg.get("Document") or "{}")
                    except ValueError:
                        continue
                    note = (doc.get("annotations") or {}).get(self.environment_annotation)
                    if doc.get("name") != p.service or note not in (None, environment):
                        continue  # another service's segment (FR-44)
                    spans += _xray_spans(doc, top=True)
                spans.sort(key=lambda s: str(s["start"]))
                failing = any(s["status"] == "ERROR" for s in spans)
                on_path = not p.path or any(p.path in str(s["name"]) for s in spans)
                if failing and on_path:
                    records.append({"trace_id": t.get("Id"), "spans": spans})
        return SourceResult(
            f"{self.name}:traces",
            f"https://{self.region}.console.aws.amazon.com/xray/home?region={self.region}#/traces",
            records,
            partial=partial,
        )

    # -- get_error_groups: Logs Insights ------------------------------------------------------
    def _error_groups(self, p: Any, environment: str) -> SourceResult:
        """StartQuery with the configured `stats ... by` query, then GetQueryResults until it
        completes or `timeout_s` passes (a gap). Derived: rows give `type`, `message`, `count`,
        `first_seen`, `last_seen`, optionally `version` and a `stack` (as `latest(...)`), whose
        Python frames become the group's."""
        assert self.log_group is not None and self.error_groups_query is not None
        cap = min(10_000, p.limit * 10)
        started = self._aws(
            "logs",
            "start_query",
            logGroupNames=[
                substitute(self.log_group, quote_plain, service=p.service, environment=environment)
            ],
            startTime=int(p.start.timestamp()),
            endTime=int(p.end.timestamp()),
            queryString=substitute(
                self.error_groups_query, quote_plain, service=p.service, environment=environment
            ),
            limit=cap,
        )
        deadline = time.monotonic() + self.timeout_s
        while True:
            found = self._aws("logs", "get_query_results", queryId=started["queryId"])
            status = found.get("status")
            if status == "Complete":
                break
            if status not in ("Scheduled", "Running"):
                raise SourceUnavailable(f"{self.name} Logs Insights query: {status}")
            if time.monotonic() >= deadline:
                raise SourceUnavailable(
                    f"{self.name} Logs Insights query: still {status} after {self.timeout_s:g} s"
                )
            time.sleep(self.poll_s)
        rows: list[Record] = []
        for row in found.get("results") or []:
            f = {c.get("field"): c.get("value") for c in row}
            if not (f.get("first_seen") and f.get("last_seen")):
                continue
            rows.append(
                {
                    "type": f.get("type"),
                    "message": f.get("message"),
                    "version": f.get("version"),
                    "count": int(float(f.get("count") or 0)),
                    "first_seen": utc_text(parse_time(f["first_seen"])),
                    "last_seen": utc_text(parse_time(f["last_seen"])),
                    "frames": frames_from(f.get("stack")),
                }
            )
        groups = error_group_records(rows)
        if p.release:
            groups = [g for g in groups if p.release in g["versions"]]
        return SourceResult(
            f"{self.name}:error_groups",
            self._link("logsV2:logs-insights"),
            groups[: p.limit],
            partial=len(rows) >= cap or len(groups) > p.limit,
        )

    # -- get_dependency_health: GetMetricData per dependency ----------------------------------
    def _dependencies(self, p: Any, environment: str) -> SourceResult:
        assert self.dependencies is not None
        queries, keys = [], {}
        for i, (dep, spec) in enumerate(self.dependencies.items()):
            for measure, expr in spec["queries"].items():
                qid = f"d{i}_{measure}"
                keys[qid] = (dep, measure)
                queries.append(
                    {
                        "Id": qid,
                        "Expression": substitute(
                            expr,
                            quote_plain,
                            service=p.service,
                            environment=environment,
                            dependency=dep,
                        ),
                        "Period": self.period_s,
                        "ReturnData": True,
                    }
                )
        found = self._aws(
            "cloudwatch",
            "get_metric_data",
            MetricDataQueries=queries,
            StartTime=p.at - timedelta(seconds=self.dependency_lookback_s),
            EndTime=p.at,
            ScanBy="TimestampDescending",
        )
        measured: dict[str, dict[str, float | None]] = {d: {} for d in self.dependencies}
        seen: set[str] = set()
        for r in found.get("MetricDataResults") or []:
            if r.get("Id") not in keys:
                continue
            dep, measure = keys[r["Id"]]
            if r["Id"] in seen:
                raise SourceUnavailable(
                    f"{self.name}: {dep} gave more than one series; aggregate it"
                )
            seen.add(r["Id"])
            values = r.get("Values") or []
            measured[dep][measure] = float(values[0]) if values else None  # the latest
        how = f"latest point in the {self.dependency_lookback_s} s to {utc_text(p.at)}, {self.name}"
        records = [
            dependency_record(dep, spec, measured[dep], p.at, how)
            for dep, spec in self.dependencies.items()
        ]
        return SourceResult(
            f"{self.name}:dependencies",
            self._link("metricsV2"),
            records,
            partial=bool(found.get("NextToken")) or any(r["status"] == "unknown" for r in records),
        )


def _aws_time(value: str) -> str:
    """CloudWatch's `2026-11-19T10:03:00.000+0000` as ISO 8601 with a colon in the offset."""
    return re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", value)


def _xray_spans(doc: Mapping[str, Any], top: bool = False) -> list[Record]:
    """A segment document (https://docs.aws.amazon.com/xray/latest/devguide/xray-api-segmentdocuments.html)
    and its subsegments as spans."""
    http = doc.get("http") or {}
    request, response = http.get("request") or {}, http.get("response") or {}
    name = doc.get("name")
    if top and request.get("method") and request.get("url"):
        name = f"{request['method']} {urlsplit(str(request['url'])).path}"
    cause = doc.get("cause")
    exceptions = (cause.get("exceptions") or []) if isinstance(cause, Mapping) else []
    attributes: dict[str, Any] = dict(doc.get("annotations") or {})
    if response.get("status") is not None:
        attributes["http.response.status_code"] = response["status"]
    stack = next((e.get("stack") for e in exceptions if e.get("stack")), None)
    if stack and stack[0].get("path"):
        attributes["code.filepath"] = stack[0]["path"]
    start, end = doc.get("start_time"), doc.get("end_time")
    span: Record = {
        "name": name,
        "status": "ERROR" if doc.get("fault") else "OK",
        "start": iso_ms(datetime.fromtimestamp(float(start), UTC)) if start else None,
        "end": iso_ms(datetime.fromtimestamp(float(end), UTC)) if end else None,
        "attributes": attributes,
        "exceptions": [{"type": e.get("type"), "message": e.get("message")} for e in exceptions],
    }
    return [span, *(s for sub in doc.get("subsegments") or [] for s in _xray_spans(sub))]
