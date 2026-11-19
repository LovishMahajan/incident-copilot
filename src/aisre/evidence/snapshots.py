"""Artifact 7.2 (ADR-57), 13.5 T1.10: evidence snapshots in the object store.

Each evidence record's raw result, redacted and size-capped, is kept as one write-once object;
the `artifacts` row points at it and the evidence row at the artifact, never at the key
(Artifact 7: the storage key is internal). Snapshots go after 30 days unless a diagnosis cites
them (ADR-57); the retention job deletes them.

`S3Snapshots` speaks the S3 API (MinIO locally, T1.1) with AWS Signature Version 4, from the
standard library, so no SDK is needed for one PUT. Keys are `evidence/<incident>/<evidence>.json`.
`get` reads an object back: the Test Runner reads a patch's canonical bytes this way (M2, T2.6).
"""

from __future__ import annotations

import hashlib
import hmac
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree  # noqa: S405 (the configured object store's own answer)

from pydantic import SecretStr


class SnapshotFailed(Exception):
    """The object store refused or didn't answer."""


class SnapshotStore(Protocol):
    def put(self, key: str, body: bytes, media_type: str) -> None: ...


class MemorySnapshots:
    """For tests: keeps what was put, in this process."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, key: str, body: bytes, media_type: str) -> None:
        self.objects[key] = body

    def get(self, key: str) -> bytes:
        try:
            return self.objects[key]
        except KeyError:
            raise SnapshotFailed(f"no object {key}") from None


def snapshot_key(incident_id: str, evidence_id: str) -> str:
    return f"evidence/{incident_id}/{evidence_id}.json"


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


class S3Snapshots:
    """Path-style PUT to `endpoint/bucket/key`, signed with SigV4."""

    def __init__(
        self,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: SecretStr,
        region: str = "us-east-1",
        timeout_s: float = 10.0,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.timeout_s = timeout_s

    def signed_headers(
        self,
        method: str,
        path: str,
        body: bytes,
        media_type: str,
        now: datetime,
        query: str = "",
    ) -> dict[str, str]:
        amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
        payload = hashlib.sha256(body).hexdigest()
        headers = {
            "content-type": media_type,
            "host": urlsplit(self.endpoint).netloc,
            "x-amz-content-sha256": payload,
            "x-amz-date": amz_date,
        }
        names = ";".join(sorted(headers))
        canonical = "\n".join(
            [
                method,
                path,
                query,
                *(f"{k}:{headers[k]}" for k in sorted(headers)),
                "",
                names,
                payload,
            ]
        )
        scope = f"{day}/{self.region}/s3/aws4_request"
        to_sign = "\n".join(
            ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()]
        )
        key = ("AWS4" + self.secret_key.get_secret_value()).encode()
        for part in (day, self.region, "s3", "aws4_request"):
            key = _hmac(key, part)
        signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
        headers["authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={names}, Signature={signature}"
        )
        return headers

    def put(self, key: str, body: bytes, media_type: str) -> None:
        path = "/" + quote(f"{self.bucket}/{key}", safe="/-_.~")
        headers = self.signed_headers("PUT", path, body, media_type, datetime.now(UTC))
        headers.pop("host")  # urllib sets it from the URL, to the same value
        req = urllib.request.Request(self.endpoint + path, data=body, method="PUT", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s):
                return
        except urllib.error.HTTPError as e:
            raise SnapshotFailed(f"object store refused the snapshot: HTTP {e.code}") from None
        except (urllib.error.URLError, OSError) as e:
            raise SnapshotFailed(f"object store unreachable: {type(e).__name__}") from None

    def get(self, key: str) -> bytes:
        path = "/" + quote(f"{self.bucket}/{key}", safe="/-_.~")
        headers = self.signed_headers(
            "GET", path, b"", "application/octet-stream", datetime.now(UTC)
        )
        headers.pop("host")
        req = urllib.request.Request(self.endpoint + path, method="GET", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return bytes(resp.read())
        except urllib.error.HTTPError as e:
            raise SnapshotFailed(f"object store refused the read: HTTP {e.code}") from None
        except (urllib.error.URLError, OSError) as e:
            raise SnapshotFailed(f"object store unreachable: {type(e).__name__}") from None

    def _call(self, method: str, path: str, query: dict[str, str]) -> bytes:
        q = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in sorted(query.items()))
        headers = self.signed_headers(
            method, path, b"", "application/octet-stream", datetime.now(UTC), q
        )
        headers.pop("host")
        url = self.endpoint + path + (f"?{q}" if q else "")
        req = urllib.request.Request(url, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return bytes(resp.read())
        except urllib.error.HTTPError as e:
            raise SnapshotFailed(f"object store refused the {method}: HTTP {e.code}") from None
        except (urllib.error.URLError, OSError) as e:
            raise SnapshotFailed(f"object store unreachable: {type(e).__name__}") from None

    def delete(self, key: str) -> int:
        """T4.8 (12.7): deletes the object and every version of it, so nothing is left in a
        versioned bucket; returns how many versions went. Absent is not an error."""
        bucket = "/" + quote(self.bucket, safe="/-_.~")
        listing = self._call("GET", bucket, {"versions": "", "prefix": key})
        ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
        versions = [
            v.findtext("s3:VersionId", default="", namespaces=ns)
            for tag in ("s3:Version", "s3:DeleteMarker")
            for v in ElementTree.fromstring(listing).findall(tag, ns)
            if v.findtext("s3:Key", default="", namespaces=ns) == key
        ]
        path = "/" + quote(f"{self.bucket}/{key}", safe="/-_.~")
        for version in versions:
            self._call("DELETE", path, {"versionId": version} if version else {})
        return len(versions)
