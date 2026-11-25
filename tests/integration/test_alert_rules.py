"""T4.6 (Artifact 12.3, WR-25): every rule in ops/alerts.yml fires on the series it reads.

`promtool test rules`, from the Prometheus image Compose runs, over synthetic series: one case
per alert, past its hold, with the labels and annotations it must carry (route labels
included, which Alertmanager routes on). This proves each rule's expression and hold, not that
a component publishes the metric: tests/fault/test_alert_faults.py fires the ones this stack
can produce from a real fault, and ADR-74 lists the rest. Needs Docker; skips without it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
RULES = ROOT / "ops/alerts.yml"
PROMETHEUS_IMAGE = "prom/prometheus:v3.16.0"  # compose.yaml's


def hist(name: str, below: str, above: str, labels: str = "") -> list[dict[str, str]]:
    """A histogram whose observations all land between `below` and `above`."""
    sep = "," if labels else ""
    return [
        {"series": f'{name}_bucket{{{labels}{sep}le="{below}"}}', "values": "0x90"},
        {"series": f'{name}_bucket{{{labels}{sep}le="{above}"}}', "values": "0+10x90"},
        {"series": f'{name}_bucket{{{labels}{sep}le="+Inf"}}', "values": "0+10x90"},
    ]


def flat(series: str, value: str, n: int = 90) -> dict[str, str]:
    return {"series": series, "values": f"{value}x{n}"}


def rising(series: str, step: str = "1", n: int = 90) -> dict[str, str]:
    return {"series": series, "values": f"0+{step}x{n}"}


INTAKE = 'aisre_http_requests_total{route="/v1/incidents",method="POST",status_class="%s"}'
CALLS = 'aisre_model_calls_total{task="diagnose",status="%s"}'
VERDICT = 'aisre_verification_outcomes_total{action_kind="config_revert",category="c",verdict="%s"}'

# alertname -> (input series, eval time, labels beyond alertname and the rule's own)
CASES: dict[str, tuple[list[dict[str, str]], str, dict[str, str]]] = {
    "AisreIntakeErrorRate": (
        [rising(INTAKE % "5xx", "10"), rising(INTAKE % "2xx", "10")],
        "10m",
        {},
    ),
    "AisreIntakeLatencyHigh": (
        hist("aisre_http_request_seconds", "0.3", "1", 'route="/v1/incidents",method="POST"'),
        "20m",
        {},
    ),  # fmt: skip
    "AisreIntakeLatencyP99High": (
        hist("aisre_http_request_seconds", "1", "2.5", 'route="/v1/incidents",method="POST"'),
        "20m",
        {},
    ),  # fmt: skip
    "AisreIntakeAvailabilityMonthly": (
        [rising(INTAKE % "5xx"), rising(INTAKE % "2xx", "10")],
        "30m",
        {},
    ),  # fmt: skip
    "AisrePostgresDown": (
        [flat('aisre_dependency_up{dependency="postgresql",job="api"}', "0")],
        "3m",
        {},
    ),  # fmt: skip
    "AisreOutboxStuck": ([flat("aisre_outbox_oldest_age_seconds", "200")], "1m", {}),
    "AisreEventDeliverySlow": (hist("aisre_event_available_seconds", "1", "5"), "20m", {}),
    "AisreQueueBacklog": (
        [flat('aisre_incident_queue_oldest_age_seconds{severity="sev1"}', "400")],
        "1m",
        {},
    ),  # fmt: skip
    "AisreQueueDepthHigh": ([flat('aisre_incident_queue_depth{severity="sev3"}', "60")], "1m", {}),
    "AisreFirstSummarySlow": (hist("aisre_time_to_first_summary_seconds", "300", "600"), "70m", {}),
    "AisreFirstNotificationSlow": (
        hist("aisre_time_to_first_notification_seconds", "60", "120"),
        "50m",
        {},
    ),  # fmt: skip
    "AisreWorkflowStalled": (
        [flat('aisre_workflows_stalled{state="investigating"}', "1")],
        "8m",
        {},
    ),
    "AisreCrashResumeSlow": (
        [
            rising("aisre_worker_resume_seconds_count"),
            flat('aisre_worker_resume_seconds_bucket{le="120.0"}', "0"),
        ],
        "10m",
        {},
    ),  # fmt: skip
    "AisreCancellationSlow": (hist("aisre_cancellation_seconds", "30", "60"), "50m", {}),
    "AisreModelAuthErrors": ([rising(CALLS % "auth_error")], "5m", {}),
    "AisreModelErrorRate": (
        [rising(CALLS % "provider_error", "10"), rising(CALLS % "ok", "10")],
        "30m",
        {},
    ),  # fmt: skip
    "AisreModelLatencyHigh": (
        hist("aisre_model_call_seconds", "45", "60", 'task="diagnose"'),
        "40m",
        {"task": "diagnose"},
    ),  # fmt: skip
    "AisreCircuitOpen": (
        [flat('aisre_circuit_open{dependency="model_provider"}', "1")],
        "8m",
        {"dependency": "model_provider"},
    ),  # fmt: skip
    "AisreUnreconciledUsage": ([flat("aisre_budget_pending_reconcile", "30")], "70m", {}),
    "AisreModelOutputSecret": (
        [rising('aisre_model_output_unsafe_total{task="diagnose",kind="secret"}')],
        "5m",
        {},
    ),  # fmt: skip
    "AisreActionOutcomeUnknown": ([flat("aisre_actions_outcome_unknown", "1")], "3m", {}),
    "AisreApprovalBacklog": ([flat("aisre_approval_oldest_age_seconds", "1000")], "1m", {}),
    "AisreApprovalRefused": (
        [rising('aisre_approval_refused_total{reason="self_approval"}')],
        "5m",
        {},
    ),  # fmt: skip
    "AisreReleaseChangeUnmatched": ([flat("aisre_release_changes_unmatched", "1")], "1m", {}),
    "AisreVerificationFailureRate": (
        [rising(VERDICT % "failed"), rising(VERDICT % "recovered")],
        "20m",
        {},
    ),  # fmt: skip
    "AisreSandboxLeak": ([flat("aisre_sandboxes_leftover", "1")], "1m", {}),
    "AisreWritesFenced": ([flat('aisre_dispatch_fenced{reason="manual"}', "1")], "20m", {}),
    "AisreAuditChainBroken": ([flat("aisre_audit_chain_faults", "1")], "1m", {}),
    "AisreTelemetryDropping": ([rising('aisre_telemetry_dropped_total{kind="span"}')], "40m", {}),
}
# Spend: one set of series, at the ceiling, fires every threshold at or below it.
SPEND = [
    flat("aisre_budget_daily_spent_micros", "100"),
    flat("aisre_budget_daily_limit_micros", "100"),
    flat("aisre_budget_monthly_spent_micros", "100"),
    flat("aisre_budget_monthly_limit_micros", "100"),
]
for _name in (
    "AisreDailySpend80",
    "AisreDailySpend100",
    "AisreMonthlySpend50",
    "AisreMonthlySpend80",
    "AisreMonthlySpend100",
):
    CASES[_name] = (SPEND, "1m", {})


def rules() -> dict[str, dict]:
    doc = yaml.safe_load(RULES.read_text())
    return {r["alert"]: r for g in doc["groups"] for r in g["rules"]}


def render(text: str, labels: dict[str, str]) -> str:
    return re.sub(r"\{\{\s*\$labels\.(\w+)\s*\}\}", lambda m: labels.get(m.group(1), ""), text)


def test_every_alert_has_a_case():
    assert set(CASES) == set(rules())


def test_every_alert_fires_under_promtool(tmp_path):
    try:
        subprocess.run(["docker", "image", "inspect", PROMETHEUS_IMAGE], check=True, capture_output=True, timeout=20)  # fmt: skip
    except (OSError, subprocess.SubprocessError) as e:
        pytest.skip(f"Docker or {PROMETHEUS_IMAGE} unavailable: {type(e).__name__}")
    tests = []
    for name, rule in sorted(rules().items()):
        series, at, extra = CASES[name]
        labels = {**rule.get("labels", {}), **extra}
        annotations = {k: render(v, labels) for k, v in rule.get("annotations", {}).items()}
        tests.append(
            {
                "interval": "1m",
                "input_series": series,
                "alert_rule_test": [
                    {
                        "eval_time": at,
                        "alertname": name,
                        "exp_alerts": [{"exp_labels": labels, "exp_annotations": annotations}],
                    }
                ],
            }
        )
    (tmp_path / "alerts.yml").write_text(RULES.read_text())
    (tmp_path / "alerts.test.yml").write_text(
        yaml.safe_dump({"rule_files": ["alerts.yml"], "evaluation_interval": "15s", "tests": tests})
    )
    tmp_path.chmod(0o755)
    for f in tmp_path.iterdir():
        f.chmod(0o644)
    out = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "-v",
            f"{tmp_path}:/t:ro",
            "--workdir",
            "/t",
            "--entrypoint",
            "promtool",
            PROMETHEUS_IMAGE,
            "test",
            "rules",
            "alerts.test.yml",
        ],  # fmt: skip
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stdout[-4000:] + out.stderr[-4000:]
    assert "SUCCESS" in out.stdout + out.stderr
