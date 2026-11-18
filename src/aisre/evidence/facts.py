"""Artifact 9.7 (ADR-53), 13.5 T1.11: the observations behind each record's strength tier.

`validation.assign_tier` maps an `ObservationFacts` to a tier; this module computes those facts
from the records the collector gathered, in code, never from the model. A fact compares a record
with the change history (FR-46): what changed (keys, files, versions), when it started, how far
it rolled out, and whether it was rolled back. With no change history there is nothing to link
to, so no record gets a tier, and the diagnosis can't pass low (ADR-54). The rules, and every
threshold below, are provisional (9.7):

| Tier | Fact | From |
| --- | --- | --- |
| 2 | only_new_version_failing | error_rate_by_version: in at least 3 minutes with traffic on the new version, the new version fails at FAILING_RATE or more in nearly all, and the previous version never does |
| 3 | changed_key_on_failing_path | an ERROR log line, or an ERROR trace span, names a changed configuration key (stack, message or attributes), at or after the change |
| 4 | frames_in_changed_file | a trace span's file, or an error group's frame, is a file the change touched |
| 5 | first_seen_after_change | an error group first seen at or after the change |
| 6 | failures_grow_with_rollout | http_5xx_rate's mean rises with each rollout step's traffic share, from above the pre-change baseline |
| 7 | rollback_and_recovery | the change history shows a rollback, and http_5xx_rate is back within RECOVERED of the baseline afterwards |
| 8 | linked_by_time | a failure record whose data runs past the change's start, and nothing stronger |

Tier 1, reproduction, is the Test Runner's (M2). Each record keeps the facts that held, as its
reasons, in `attributes.facts`.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
from typing import Any

from aisre.agent.agent_contracts import EvidenceKind
from aisre.agent.validation import ObservationFacts, assign_tier
from aisre.evidence.sources import Record, parse_time

FAILING_RATE = 0.05  # an error rate counted as failing; the reference alert's threshold
FAILING_SHARE = 0.8  # of the new version's minutes
MIN_POINTS = 3
SETTLE = timedelta(minutes=5)  # after a rollback, before recovery is judged
RECOVERED = 2.0  # times the pre-change median, with a floor of 0.002 (README predicate)
RECOVERED_FLOOR = 0.002
ROLLBACK_STEP = re.compile(r"(?i)rollback|rolled_back|revert")


@dataclass(frozen=True)
class ChangeInfo:
    """What the fact rules need from one change record. Stored on the change's evidence as
    `attributes.change`, so a later round can use it."""

    version: str | None
    previous_version: str | None
    keys: tuple[str, ...]
    files: tuple[str, ...]
    changed_at: datetime | None  # the rollout's start, else the last stage before it
    rollout: tuple[tuple[float, datetime], ...]  # (traffic %, from)
    rollback_at: datetime | None

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["changed_at"] = self.changed_at.isoformat() if self.changed_at else None
        d["rollback_at"] = self.rollback_at.isoformat() if self.rollback_at else None
        d["rollout"] = [[pct, at.isoformat()] for pct, at in self.rollout]
        return d

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> ChangeInfo:
        def t(v: str | None) -> datetime | None:
            return parse_time(v) if v else None

        return cls(
            d.get("version"),
            d.get("previous_version"),
            tuple(d.get("keys", ())),
            tuple(d.get("files", ())),
            t(d.get("changed_at")),
            tuple((float(p), parse_time(a)) for p, a in d.get("rollout", ())),
            t(d.get("rollback_at")),
        )


def change_info(record: Record) -> ChangeInfo:
    stages = sorted(
        ((s["step"], parse_time(s["at"])) for s in record.get("stages") or [] if s.get("at")),
        key=lambda s: s[1],
    )
    rollout = tuple(
        sorted(
            (float(r["traffic_pct"]), parse_time(r["at"]))
            for r in record.get("rollout") or []
            if r.get("at") and r.get("traffic_pct") is not None
        )
    )
    rollback = [at for step, at in stages if ROLLBACK_STEP.search(step)]
    forward = [at for step, at in stages if not ROLLBACK_STEP.search(step)]
    if rollout:
        changed_at: datetime | None = min(at for _, at in rollout)
    else:
        changed_at = forward[-1] if forward else None
    return ChangeInfo(
        record.get("version"),
        record.get("previous_version"),
        tuple(c["key"] for c in record.get("config_changes") or [] if c.get("key")),
        tuple(record.get("files") or ()),
        changed_at,
        tuple(sorted(rollout, key=lambda r: r[1])),
        min(rollback) if rollback else None,
    )


@dataclass(frozen=True)
class Observed:
    """One record as the rules see it: its kind, its tool's parameters and its records."""

    kind: EvidenceKind
    params: Mapping[str, Any]
    records: Sequence[Record]


def _mentions(text: str, key: str) -> bool:
    return bool(re.search(rf"(?i)(?<![A-Za-z0-9]){re.escape(key)}(?![A-Za-z0-9])", text))


def _after(stamp: str | None, t: datetime | None) -> bool:
    return t is None or (stamp is not None and parse_time(stamp) >= t)


def _same_file(frame: str, changed: str) -> bool:
    a, b = frame.lstrip("/"), changed.lstrip("/")
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def _rate_points(o: Observed) -> list[tuple[datetime, float]]:
    return [
        (parse_time(p["at"]), float(p["value"])) for p in o.records if p.get("value") is not None
    ]


def _baseline(points: list[tuple[datetime, float]], c: ChangeInfo) -> float | None:
    before = [v for at, v in points if c.changed_at is not None and at < c.changed_at]
    return statistics.median(before) if before else None


def _version_split(o: Observed, c: ChangeInfo) -> bool:
    if not (c.version and c.previous_version):
        return False
    new_rates, old_failing = [], False
    for p in o.records:
        versions = p.get("versions") or {}
        new, old = versions.get(c.version) or {}, versions.get(c.previous_version) or {}
        if new.get("requests") and new.get("rate") is not None:
            new_rates.append(float(new["rate"]))
            if old.get("requests") and (old.get("rate") or 0) >= FAILING_RATE:
                old_failing = True
    failing = sum(r >= FAILING_RATE for r in new_rates)
    return (
        len(new_rates) >= MIN_POINTS
        and failing >= FAILING_SHARE * len(new_rates)
        and not old_failing
    )


def _grows_with_rollout(o: Observed, c: ChangeInfo) -> bool:
    points = _rate_points(o)
    base = _baseline(points, c)
    if base is None or len(c.rollout) < 2:
        return False
    means = []
    bounds = [at for _, at in c.rollout[1:]] + [datetime.max.replace(tzinfo=c.rollout[0][1].tzinfo)]
    for (pct, start), end in zip(c.rollout, bounds, strict=True):
        if c.rollback_at is not None and start >= c.rollback_at:
            break
        step = [v for at, v in points if start <= at < end]
        if step:
            means.append((pct, statistics.mean(step)))
    pcts, values = [p for p, _ in means], [v for _, v in means]
    return (
        len(means) >= 2
        and pcts == sorted(set(pcts))
        and all(b > a for a, b in zip(values, values[1:], strict=False))
        and values[0] > max(base * RECOVERED, RECOVERED_FLOOR)
    )


def _recovered(o: Observed, c: ChangeInfo) -> bool:
    points = _rate_points(o)
    base = _baseline(points, c)
    if c.rollback_at is None or base is None:
        return False
    after = [v for at, v in points if at >= c.rollback_at + SETTLE]
    limit = max(base * RECOVERED, RECOVERED_FLOOR)
    return len(after) >= MIN_POINTS and all(v <= limit for v in after)


def _failing_after(o: Observed, c: ChangeInfo) -> bool:
    """The record shows a failure from the change's start on: the only link is time."""
    if c.changed_at is None:
        return False
    if o.kind == EvidenceKind.alert:
        return any(_after(r.get("fired_at"), c.changed_at) for r in o.records)
    if o.kind == EvidenceKind.log:
        return any(
            r.get("level") == "ERROR" and _after(r.get("ts"), c.changed_at) for r in o.records
        )
    if o.kind == EvidenceKind.error_group:
        return any(_after(r.get("last_seen"), c.changed_at) for r in o.records)
    if o.kind == EvidenceKind.trace:
        return any(
            s.get("status") == "ERROR" and _after(s.get("start"), c.changed_at)
            for t in o.records
            for s in t.get("spans", [])
        )
    if o.kind == EvidenceKind.metric:
        return any(
            at >= c.changed_at and v >= FAILING_RATE for at, v in _rate_points(o)
        ) or _version_split(o, c)
    return False


def facts_for(
    o: Observed, changes: Iterable[ChangeInfo], context: Sequence[Observed] = ()
) -> ObservationFacts:
    """The facts that hold for `o` against any of `changes`. `context` is the rest of the round,
    for the rule that needs a metric beside the change record (tier 7)."""
    found: dict[str, bool] = {}
    for c in changes:
        f = found.setdefault
        if o.kind == EvidenceKind.metric:
            metric = o.params.get("metric")
            if metric == "error_rate_by_version" and _version_split(o, c):
                f("only_new_version_failing", True)
            if metric == "http_5xx_rate":
                if _grows_with_rollout(o, c):
                    f("failures_grow_with_rollout", True)
                if _recovered(o, c):
                    f("rollback_and_recovery", True)
        if o.kind == EvidenceKind.log:
            for r in o.records:
                text = " ".join(str(r.get(k) or "") for k in ("message", "error_message", "stack"))
                if r.get("level") == "ERROR" and _after(r.get("ts"), c.changed_at):
                    if any(_mentions(text, k) for k in c.keys):
                        f("changed_key_on_failing_path", True)
        if o.kind == EvidenceKind.trace:
            for t in o.records:
                for s in t.get("spans", []):
                    if s.get("status") != "ERROR" or not _after(s.get("start"), c.changed_at):
                        continue
                    attrs = s.get("attributes") or {}
                    text = " ".join(
                        [*map(str, attrs.values())]
                        + [str(e.get("message") or "") for e in s.get("exceptions", [])]
                    )
                    if any(_mentions(text, k) for k in c.keys):
                        f("changed_key_on_failing_path", True)
                    path = attrs.get("code.filepath")
                    if path and any(_same_file(str(path), ch) for ch in c.files):
                        f("frames_in_changed_file", True)
        if o.kind == EvidenceKind.error_group:
            for g in o.records:
                frames = [str(fr[0]) for fr in g.get("frames") or []]
                if any(_same_file(fr, ch) for fr in frames for ch in c.files):
                    f("frames_in_changed_file", True)
                if c.changed_at is not None and _after(g.get("first_seen"), c.changed_at):
                    f("first_seen_after_change", True)
        if o.kind == EvidenceKind.change and c.rollback_at is not None:
            rates = [
                m
                for m in context
                if m.kind == EvidenceKind.metric and m.params.get("metric") == "http_5xx_rate"
            ]
            if any(_recovered(m, c) for m in rates):
                f("rollback_and_recovery", True)
        if _failing_after(o, c):
            f("linked_by_time", True)
    return ObservationFacts(**found)


def tier_and_reasons(
    o: Observed, changes: Iterable[ChangeInfo], context: Sequence[Observed] = ()
) -> tuple[int | None, list[str]]:
    """The record's strength tier (validation.assign_tier) and the facts that held."""
    facts = facts_for(o, list(changes), context)
    held = [f.name for f in fields(facts) if getattr(facts, f.name)]
    return assign_tier(o.kind, facts), held
