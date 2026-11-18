"""Artifact 13.5 T1.1: the local fakes in ops/fakes, each run in-process on a free port.

The Compose stack runs the same files (compose.yaml); `docker compose up --wait` checks they
answer /healthz. These tests check what each one says.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import urllib.error
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parents[2]
FAKES = ROOT / "ops/fakes"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"fake_{name}", FAKES / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


token_issuer, model_provider, monitoring = (
    load(n) for n in ("token_issuer", "model_provider", "monitoring")
)


def call(
    url: str, body: Any = None, headers: dict[str, str] | None = None
) -> tuple[int, Any, dict[str, str]]:
    data = (
        None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    )
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read()), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), dict(e.headers)


@pytest.fixture(scope="module")
def issuer_url():
    issuer = token_issuer.Issuer("http://issuer.test", "aisre-api")
    server = token_issuer.serve(issuer)
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="module")
def provider_url():
    server = model_provider.serve(
        model_provider.Provider(model_provider.load_scripts(model_provider.SCRIPTS))
    )
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="module")
def monitoring_url():
    server = monitoring.serve()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def segment(token: str, i: int) -> dict[str, Any]:
    part = token.split(".")[i]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


# ---------------------------------------------------------------- token issuer
def test_issuer_advertises_its_keys(issuer_url):
    status, conf, _ = call(issuer_url + "/.well-known/openid-configuration")
    assert status == 200 and conf["issuer"] == "http://issuer.test"
    assert conf["jwks_uri"] == "http://issuer.test/jwks"
    _, jwks, _ = call(issuer_url + "/jwks")
    (key,) = jwks["keys"]
    assert key["kty"] == "RSA" and key["alg"] == "RS256" and key["kid"]
    assert "d" not in key  # public part only


def test_issuer_mints_a_short_lived_token_with_the_claims_the_api_reads(issuer_url):
    status, body, headers = call(
        issuer_url + "/token",
        {"sub": "alice", "groups": ["aisre:responder"], "mfa": False, "auth_age_seconds": 60},
    )
    assert status == 200 and body["expires_in"] == token_issuer.DEFAULT_TTL
    assert headers["Cache-Control"] == "no-store"
    claims = segment(body["access_token"], 1)
    assert claims["iss"] == "http://issuer.test" and claims["aud"] == "aisre-api"
    assert claims["exp"] - claims["iat"] == token_issuer.DEFAULT_TTL
    assert claims["iat"] - claims["auth_time"] == 60
    assert claims["groups"] == ["aisre:responder"] and claims["amr"] == ["pwd"]
    assert claims["org_id"] == token_issuer.DEV_ORG_ID
    assert segment(body["access_token"], 0)["alg"] == "RS256"


@pytest.mark.parametrize(
    "request_body",
    [{}, {"sub": ""}, {"sub": "a", "groups": "x"}, {"sub": "a", "ttl_seconds": 3600}],
)
def test_issuer_refuses_a_bad_token_request(issuer_url, request_body):
    assert call(issuer_url + "/token", request_body)[0] == 400


def test_issuer_refuses_to_start_in_production(monkeypatch):
    monkeypatch.setenv("AISRE_ENV", "production")
    with pytest.raises(SystemExit, match="production"):
        token_issuer.main()


# ---------------------------------------------------------------- model provider
MESSAGE = {
    "model": "claude-test",
    "max_tokens": 50,
    "messages": [{"role": "user", "content": "hi"}],
}
API_HEADERS = {"x-api-key": "fake-key", "anthropic-version": "2023-06-01"}


def test_provider_answers_in_the_messages_api_shape(provider_url):
    status, msg, headers = call(provider_url + "/v1/messages", MESSAGE, API_HEADERS)
    assert status == 200
    assert {
        "id",
        "type",
        "role",
        "model",
        "content",
        "stop_reason",
        "stop_sequence",
        "usage",
    } <= set(msg)
    assert (msg["type"], msg["role"], msg["model"]) == ("message", "assistant", "claude-test")
    assert msg["content"][0]["type"] == "text" and msg["stop_reason"] == "end_turn"
    assert msg["usage"]["input_tokens"] > 0 and headers["request-id"].startswith("req_fake_")


def test_provider_scripts_match_on_request_text(provider_url):
    def ask(text: str) -> tuple[int, Any, dict[str, str]]:
        body = MESSAGE | {"messages": [{"role": "user", "content": text}]}
        return call(provider_url + "/v1/messages", body, API_HEADERS)

    status, err, headers = ask("please FAKE_RATE_LIMIT now")
    assert status == 429 and err["type"] == "error"
    assert err["error"]["type"] == "rate_limit_error" and headers["retry-after"] == "1"
    assert ask("FAKE_OVERLOADED")[0] == 529
    assert ask("FAKE_MAX_TOKENS")[1]["stop_reason"] == "max_tokens"


@pytest.mark.parametrize(
    "headers,body,status,kind",
    [
        ({"anthropic-version": "2023-06-01"}, MESSAGE, 401, "authentication_error"),
        ({"x-api-key": "k"}, MESSAGE, 400, "invalid_request_error"),
        (API_HEADERS, {"model": "m"}, 400, "invalid_request_error"),
        (API_HEADERS, b"not json", 400, "invalid_request_error"),
    ],
)
def test_provider_refuses_what_the_real_api_refuses(provider_url, headers, body, status, kind):
    got, err, _ = call(provider_url + "/v1/messages", body, headers)
    assert got == status and err["error"]["type"] == kind


def test_provider_answers_cite_the_data_blocks_they_were_sent(tmp_path):
    (tmp_path / "a.json").write_text(
        json.dumps(
            {
                "match": ["STEP", "both"],
                "answer": {"cites": ["{{ref:(?s)^\\[log .*ValueError}}"], "n": 1},
            }
        )
    )
    p = model_provider.Provider(model_provider.load_scripts(tmp_path))
    log = "11111111-1111-4111-8111-111111111111"
    text = (
        'STEP both\n<data ref="evidence:22222222-2222-4222-8222-222222222222" kind="evidence">\n'
        '[metric from m] ValueError\n</data>\n<data ref="evidence:' + log + '" kind="evidence">\n'
        "[log from l] 3 lines\nValueError: x\n</data>"
    )
    body = MESSAGE | {"messages": [{"role": "user", "content": text}]}
    status, msg, _ = p.answer(API_HEADERS, json.dumps(body).encode())
    assert status == 200 and json.loads(msg["content"][0]["text"]) == {"cites": [log], "n": 1}
    body = MESSAGE | {"messages": [{"role": "user", "content": "STEP both, no data"}]}
    status, err, _ = p.answer(API_HEADERS, json.dumps(body).encode())  # falls through: no default
    assert status == 500 and "no script matched" in err["error"]["message"]
    body = MESSAGE | {"messages": [{"role": "user", "content": "STEP only"}]}
    assert p.answer(API_HEADERS, json.dumps(body).encode())[0] == 500


def test_provider_takes_the_key_as_a_bearer_token_too(provider_url):
    bearer = {"authorization": "Bearer fake-key", "anthropic-version": "2023-06-01"}
    assert call(provider_url + "/v1/messages", MESSAGE, bearer)[0] == 200
    empty = {"authorization": "Bearer ", "anthropic-version": "2023-06-01"}
    assert call(provider_url + "/v1/messages", MESSAGE, empty)[0] == 401


def test_provider_keeps_requests_but_never_the_key(provider_url):
    call(provider_url + "/v1/messages", MESSAGE | {"system": "CAPTURED"}, API_HEADERS)
    _, seen, _ = call(provider_url + "/_fake/requests")
    assert seen[-1]["system"] == "CAPTURED"
    assert "fake-key" not in json.dumps(seen)


# ---------------------------------------------------------------- monitoring
def test_monitoring_withholds_files_for_the_honesty_variant(monitoring_url):
    ref = monitoring_url + "/v1/scenarios/reference-incident:without=release.json,config-diff.txt"
    assert call(ref + "/changes")[0] == 404
    assert call(ref + "/alert")[0] == 200 and call(ref + "/logs")[0] == 200
    only_diff = monitoring_url + "/v1/scenarios/reference-incident:without=config-diff.txt"
    assert call(only_diff + "/changes")[1]["config_diff"] is None
    assert call(monitoring_url + "/v1/scenarios/reference-incident:other=x/alert")[0] == 404


def test_monitoring_serves_the_reference_incident(monitoring_url):
    base = monitoring_url + "/v1/scenarios"
    assert call(base)[1] == [  # with the evaluation seed's cases, by case ID (T1.22)
        "dev-3f9c2a71",
        "dev-8b14e6d0",
        "reference-incident",
        "seed-0001",
        "seed-0002",
    ]
    ref = base + "/reference-incident"
    assert call(ref + "/alert")[1]["incident_create"]["service"] == "orders-api"
    assert call(ref + "/traces")[1]["trace_id"]
    assert call(ref + "/catalog")[1]["service"] == "orders-api"
    changes = call(ref + "/changes")[1]
    assert changes["version"] == "2.14.1" and "production.env" in changes["config_diff"]


def test_monitoring_filters_by_time(monitoring_url):
    ref = monitoring_url + "/v1/scenarios/reference-incident"
    rows = call(ref + "/metrics?start=2026-10-20T14:00:00Z&end=2026-10-20T14:02:00Z")[1]
    assert [r["minute_utc"] for r in rows] == [
        "2026-10-20T14:00:00Z",
        "2026-10-20T14:01:00Z",
        "2026-10-20T14:02:00Z",
    ]
    logs = call(ref + "/logs?end=2026-10-20T13:41:00Z")[1]
    assert logs and all(r["ts"] <= "2026-10-20T13:41:00Z" for r in logs)


@pytest.mark.parametrize(
    "path",
    [
        "reference-incident/answer-key",
        "..%2F..%2Fsrc/alert",
        "nope/alert",
        "keys-dev-seed-1/alert",
        "dev-seed-1/alert",
    ],
)
def test_monitoring_never_serves_the_answer_key_or_other_files(monitoring_url, path):
    assert call(f"{monitoring_url}/v1/scenarios/{path}")[0] == 404


# ---------------------------------------------------------------- Compose wiring
def test_compose_runs_every_fake_and_the_telemetry_stack():
    services = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]
    for name in (
        "minio",
        "minio-bucket",
        "otel-collector",
        "prometheus",
        "token-issuer",
        "fake-provider",
        "fake-monitoring",
        "demo-viewer",  # ADR-70
    ):
        assert name in services, name
    for name, svc in services.items():
        for port in svc.get("ports", []):
            assert port.startswith("127.0.0.1:"), (name, port)
        image = svc.get("image", "")
        assert ":" in image and not image.endswith(":latest"), (name, image)
    prom = services["prometheus"]["volumes"]
    assert "./ops/alerts.yml:/etc/prometheus/alerts.yml:ro" in prom
    rules = yaml.safe_load((ROOT / "ops/compose/prometheus.yml").read_text())["rule_files"]
    assert rules == ["/etc/prometheus/alerts.yml"]
