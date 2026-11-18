"""ADR-70: the demo viewer stays read-only and renders untrusted text as text.

Reads demo/nginx.conf as text (no nginx needed): which location each request lands in, under
nginx's matching rules for the forms the file uses (exact, prefix, regex, one nested regex),
and which methods that location lets through. Then the security headers, the Compose service,
and demo/app.js for HTML sinks and eval.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
DEMO = ROOT / "demo"
CONF = (DEMO / "nginx.conf").read_text()

CSP = (
    "default-src 'self'; connect-src 'self'; style-src 'self'; script-src 'self'; "
    "img-src 'self' data:; frame-ancestors 'none'"
)


@dataclass
class Location:
    modifier: str  # "=", "~", "^~" or "" (prefix)
    pattern: str
    body: str
    children: list[Location] = field(default_factory=list)

    def own_body(self) -> str:
        """The block's text without its nested locations."""
        text = self.body
        for c in self.children:
            text = text.replace(c.body, "")
        return text

    def allowed(self) -> set[str]:
        """Methods the block's `if ($request_method ...) { return 405; }` lets through."""
        body = self.own_body()
        m = re.search(r"if \(\$request_method !~ \^\(([A-Z|]+)\)\$\) \{ return 405; \}", body)
        if m:
            return set(m.group(1).split("|"))
        m = re.search(r"if \(\$request_method != ([A-Z]+)\) \{ return 405; \}", body)
        assert m, f"location {self.pattern} has no method rule"
        return {m.group(1)}

    def upstream(self) -> str | None:
        m = re.search(r"proxy_pass (\$\w+);", self.own_body())
        return m.group(1) if m else None

    def matches(self, path: str) -> bool:
        if self.modifier == "=":
            return path == self.pattern
        if self.modifier == "~":
            return re.search(self.pattern, path) is not None
        return path.startswith(self.pattern)


def parse(text: str) -> list[Location]:
    out: list[Location] = []
    head = re.compile(r"location\s+(=|~|\^~)?\s*(\S+)\s*\{")
    pos = 0
    while (m := head.search(text, pos)) is not None:
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        body = text[m.end() : i - 1]
        out.append(Location(m.group(1) or "", m.group(2), body, parse(body)))
        pos = i
    return out


SERVER = parse(CONF)


def route(path: str) -> Location:
    """nginx: an exact match wins; else the longest prefix, unless a regex (first in file
    order, nested ones of that prefix first) matches."""
    for loc in SERVER:
        if loc.modifier == "=" and loc.matches(path):
            return loc
    prefixes = [x for x in SERVER if x.modifier in ("", "^~") and x.matches(path)]
    best = max(prefixes, key=lambda x: len(x.pattern)) if prefixes else None
    if best is not None:
        for child in best.children:
            if child.matches(path):
                return child
        if best.modifier == "^~":
            return best
    for loc in SERVER:
        if loc.modifier == "~" and loc.matches(path):
            return loc
    assert best is not None, path
    return best


def outcome(method: str, path: str) -> str:
    """'405', 'static', '404' or the upstream variable the request is proxied to."""
    loc = route(path)
    if method not in loc.allowed():
        return "405"
    if (up := loc.upstream()) is not None:
        return up
    return "404" if "return 404" in loc.own_body() else "static"


INCIDENT = "/v1/incidents/01a1ee90-ae44-7289-aa2b-4d9ef6e077ab"


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("GET", "/", "static"),
        ("GET", "/app.js", "static"),
        ("HEAD", "/styles.css", "static"),
        ("GET", "/reference-alert.json", "static"),
        ("GET", "/nginx.conf", "404"),
        ("POST", "/", "405"),
        ("POST", "/index.html", "405"),
        ("POST", "/token", "$issuer_upstream"),
        ("GET", "/token", "405"),
        ("GET", "/jwks", "404"),
        ("POST", "/v1/incidents", "$api_upstream"),
        ("PUT", "/v1/incidents", "405"),
        ("DELETE", "/v1/incidents", "405"),
        ("GET", INCIDENT, "$api_upstream"),
        ("HEAD", INCIDENT, "$api_upstream"),
        ("GET", f"{INCIDENT}/evidence", "$api_upstream"),
        ("GET", f"{INCIDENT}/diagnoses/current", "$api_upstream"),
        ("GET", f"{INCIDENT}/timeline/export", "$api_upstream"),
        ("GET", f"{INCIDENT}/timeline/stream", "$api_upstream"),
        ("GET", "/v1/actions/01a1ee90-0000-7000-8000-000000000000", "$api_upstream"),
        ("POST", f"{INCIDENT}/timeline/stream", "405"),
        ("POST", "/v1/incidents/", "405"),
        ("POST", f"{INCIDENT}/cancellation", "405"),
        ("POST", f"{INCIDENT}/closure", "405"),
        ("POST", f"{INCIDENT}/notes", "405"),
        ("POST", f"{INCIDENT}/severity-confirmation", "405"),
        ("POST", f"{INCIDENT}/reinvestigations", "405"),
        ("POST", "/v1/actions/x/approvals", "405"),
        ("PATCH", INCIDENT, "405"),
        ("DELETE", INCIDENT, "405"),
        ("PUT", f"{INCIDENT}/notes", "405"),
        ("GET", "/icons.js", "static"),
        ("GET", "/readyz", "$api_upstream"),
        ("HEAD", "/readyz", "$api_upstream"),
        ("GET", "/livez", "$api_upstream"),
        ("POST", "/livez", "405"),
        ("POST", "/readyz", "405"),
        ("DELETE", "/readyz", "405"),
        ("GET", "/readyz/x", "404"),
    ],
)
def test_proxy_enforces_read_only_methods(method: str, path: str, expected: str) -> None:
    assert outcome(method, path) == expected


def test_only_two_writes_reach_an_upstream() -> None:
    writes = {
        (loc.modifier, loc.pattern)
        for top in SERVER
        for loc in [top, *top.children]
        if loc.upstream() and loc.allowed() - {"GET", "HEAD"}
    }
    assert writes == {("=", "/v1/incidents"), ("=", "/token")}
    assert route("/v1/incidents").allowed() == {"GET", "HEAD", "POST"}
    assert route("/token").allowed() == {"POST"}


def test_the_stream_is_not_buffered() -> None:
    stream = route(f"{INCIDENT}/timeline/stream")
    assert stream.modifier == "~"
    body = stream.own_body()
    assert "proxy_buffering off;" in body
    m = re.search(r"proxy_read_timeout (\d+)([smh]);", body)
    assert m and int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)] >= 300


def test_security_headers_on_every_response() -> None:
    headers = dict(re.findall(r'add_header ([\w-]+) "([^"]*)" always;', CONF))
    assert headers["Content-Security-Policy"] == CSP
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    # A location's own add_header would replace the server's, dropping the CSP there.
    for top in SERVER:
        for loc in [top, *top.children]:
            assert "add_header" not in loc.own_body(), loc.pattern


def test_development_caller_headers_are_stripped() -> None:
    for name in ("X-Aisre-Dev-Caller", "X-Aisre-Dev-Org", "X-Aisre-Dev-Groups"):
        assert f'proxy_set_header {name} "";' in CONF


def test_compose_runs_the_viewer_pinned_and_local() -> None:
    svc = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"]["demo-viewer"]
    assert re.fullmatch(r"nginx:\d+\.\d+\.\d+-alpine", svc["image"])
    assert svc["ports"] == ["127.0.0.1:${AISRE_DEMO_VIEWER_PORT:-58080}:8080"]
    assert set(svc["depends_on"]) == {"api", "token-issuer"}
    assert "./demo/nginx.conf:/etc/nginx/conf.d/default.conf:ro" in svc["volumes"]
    assert all(v.endswith(":ro") for v in svc["volumes"])


def test_page_loads_only_its_own_files() -> None:
    html = (DEMO / "index.html").read_text()
    assert "<script" in html and "<style" not in html and " style=" not in html
    for ref in re.findall(r'(?:src|href)="([^"]+)"', html):
        assert not re.match(r"^[a-z]+:|^//", ref), ref
    assert "on" + "click=" not in html.lower()


def test_health_probes_are_exact_and_read_only() -> None:
    for path in ("/readyz", "/livez"):
        loc = route(path)
        assert (loc.modifier, loc.pattern) == ("=", path)
        assert loc.allowed() == {"GET", "HEAD"}
        assert loc.upstream() == "$api_upstream"


SCRIPTS = sorted(DEMO.glob("*.js"))


def test_every_script_is_served() -> None:
    assert {p.name for p in SCRIPTS} >= {"app.js", "icons.js"}
    for p in SCRIPTS:
        assert outcome("GET", f"/{p.name}") == "static", p.name


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
@pytest.mark.parametrize(
    "sink",
    ["innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function", "document.write"],
)
def test_scripts_have_no_html_sink_or_eval(script: Path, sink: str) -> None:
    assert sink not in script.read_text()


@pytest.mark.parametrize("name", ["index.html", *(p.name for p in SCRIPTS)])
def test_no_approve_or_apply_control(name: str) -> None:
    """Advisory release (ADR-44) and a read-only viewer (ADR-70): nothing offers to act."""
    text = (DEMO / name).read_text()
    for word in (
        "Approve",
        "Apply fix",
        "Apply Fix",
        "Reject",
        "Cancel incident",
        "Close incident",
    ):
        assert word not in text, word
