"""Artifact 11.6–11.8: immutable actions, the approval record and its signature, and the
authorization matrix.

An action is created once, from a tested patch, and never changes; its digest binds patch,
source revision, exact target, policy version, test result and recovery predicates. An approval
signs that digest — not the patch content, and not anything the caller supplies.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Literal, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


# ---------------------------------------------------------------------------
# 11.8 Immutable action schema
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Target:
    service: str
    environment: str                       # exactly one environment per action (Q11.55)
    deployment: str                        # the release-process target name, e.g. "checkout-prod-eu1"


@dataclass(frozen=True)
class ImmutableAction:
    action_id: str                         # stable identity; also the execution key (Q11.70)
    incident_id: str
    diagnosis_id: str
    kind: Literal["apply_patch", "release_rollback", "rollback"]
    patch_digest: Optional[str]            # None only for a release rollback without files
    base_revision: str                     # the revision the change was generated and tested against (Q11.56)
    expected_after_revision: Optional[str] # what the target should run afterwards
    target: Target
    policy_version: str                    # edit policy + approval policy in force at creation (Q11.57)
    test_bundle_digest: str                # the passing harness result for this exact patch
    recovery_predicates_digest: str        # what "recovered" means, fixed before execution
    high_impact: bool
    parent_action_id: Optional[str] = None # rollback -> the action it reverses (Q11.91)
    created_at: str = ""

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical(asdict(self))).hexdigest()


# ---------------------------------------------------------------------------
# 11.7 Authorization matrix
# ---------------------------------------------------------------------------
class Role(str, Enum):
    VIEWER = "viewer"
    RESPONDER = "responder"
    APPROVER = "approver"
    INCIDENT_LEAD = "incident_lead"
    PLATFORM_ADMIN = "platform_admin"      # runs the platform; cannot approve changes
    SECURITY_ADMIN = "security_admin"      # owns policies and keys; cannot approve changes
    SYSTEM_WORKFLOW = "system:workflow"    # the platform's own identities
    SYSTEM_DISPATCHER = "system:dispatcher"


class Perm(str, Enum):
    READ = "incident:read"
    CREATE = "incident:create"
    CANCEL = "incident:cancel"
    APPROVE = "action:approve"
    REJECT = "action:reject"
    REVOKE_OWN = "approval:revoke_own"
    CLOSE = "incident:close"
    EXECUTE = "action:execute"
    EDIT_POLICY = "policy:edit"
    MANAGE_SANDBOX = "sandbox:configure"
    MANAGE_KEYS = "keys:manage"


MATRIX: dict[Role, set[Perm]] = {
    Role.VIEWER: {Perm.READ},
    Role.RESPONDER: {Perm.READ, Perm.CREATE, Perm.CANCEL, Perm.REJECT},
    Role.APPROVER: {Perm.READ, Perm.CREATE, Perm.CANCEL, Perm.APPROVE, Perm.REJECT, Perm.REVOKE_OWN},
    Role.INCIDENT_LEAD: {Perm.READ, Perm.CREATE, Perm.CANCEL, Perm.APPROVE, Perm.REJECT, Perm.REVOKE_OWN, Perm.CLOSE},
    Role.PLATFORM_ADMIN: {Perm.READ, Perm.MANAGE_SANDBOX},
    Role.SECURITY_ADMIN: {Perm.READ, Perm.EDIT_POLICY, Perm.MANAGE_KEYS},
    Role.SYSTEM_WORKFLOW: {Perm.READ, Perm.CREATE},                 # proposes; can never approve
    Role.SYSTEM_DISPATCHER: {Perm.READ, Perm.EXECUTE},              # executes; can never approve
}


@dataclass
class Directory:
    """Who holds which role on which service, now. Backed by the identity system."""

    grants: dict[tuple[str, str], set[Role]] = field(default_factory=dict)   # (subject, service) -> roles

    def can(self, subject: str, service: str, perm: Perm) -> bool:
        return any(perm in MATRIX[r] for r in self.grants.get((subject, service), set()))


# ---------------------------------------------------------------------------
# Approval records and signatures (Q11.54–Q11.64)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ApprovalPolicy:
    version: str = "approval-policy@1"
    valid_for: timedelta = timedelta(minutes=15)       # execute within this after approval
    request_expires: timedelta = timedelta(minutes=60)
    max_auth_age: timedelta = timedelta(minutes=15)    # recent sign-in
    approvers_normal: int = 1
    approvers_high_impact: int = 2


@dataclass(frozen=True)
class Decision:
    approval_id: str
    action_id: str
    action_digest: str                 # binds to the exact immutable action (Q11.54)
    decision: Literal["approve", "reject", "revoke"]
    subject: str
    role: str
    auth_time: str
    mfa: bool
    policy_version: str
    decided_at: str
    valid_until: Optional[str]
    reason: Optional[str]
    revokes: Optional[str] = None      # approval_id being withdrawn (Q11.63)
    key_id: str = ""
    signature: str = ""

    def payload(self) -> bytes:
        d = asdict(self)
        d.pop("signature")
        return canonical(d)


class Signer:
    """Held only by the Approval Service. The dispatcher holds only the public key (ADR-17)."""

    def __init__(self, key_id: str, private: Ed25519PrivateKey):
        self.key_id, self._k = key_id, private

    def sign(self, d: Decision) -> Decision:
        unsigned = Decision(**{**asdict(d), "key_id": self.key_id, "signature": ""})
        sig = base64.b64encode(self._k.sign(unsigned.payload())).decode()
        return Decision(**{**asdict(unsigned), "signature": sig})


def verify_signature(d: Decision, keys: dict[str, Ed25519PublicKey]) -> bool:
    k = keys.get(d.key_id)
    if not k or not d.signature:
        return False
    try:
        k.verify(base64.b64decode(d.signature), d.payload())
        return True
    except InvalidSignature:
        return False


class ApprovalError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code = code


def record_decision(*, action: ImmutableAction, subject: str, decision: str, auth_time: datetime, mfa: bool,
                    now: datetime, directory: Directory, policy: ApprovalPolicy, signer: Signer,
                    approval_id: str, presented_digest: str, proposer_subjects: set[str],
                    reason: Optional[str] = None, revokes: Optional[str] = None,
                    prior: list[Decision] = ()) -> Decision:
    """The Approval API's core. Accepts only the action ID and the digest the approver saw;
    never patch content or targets (Q11.61)."""
    if presented_digest != action.digest:
        raise ApprovalError("digest_mismatch", "the action changed since you reviewed it")
    if subject in proposer_subjects or subject.startswith("system:"):
        raise ApprovalError("self_approval", "proposers and platform identities cannot decide")   # Q11.49
    if now - auth_time > policy.max_auth_age or not mfa:
        raise ApprovalError("reauth_required", "sign in again with two-factor")                    # Q11.50
    perm = {"approve": Perm.APPROVE, "reject": Perm.REJECT, "revoke": Perm.REVOKE_OWN}[decision]
    if not directory.can(subject, action.target.service, perm):
        raise ApprovalError("forbidden", f"{subject} may not {decision} for {action.target.service}")   # Q11.51
    if decision == "reject" and not reason:
        raise ApprovalError("reason_required")
    if decision == "revoke":
        own = [p for p in prior if p.approval_id == revokes and p.subject == subject and p.decision == "approve"]
        if not own:
            raise ApprovalError("not_own_approval", "you can only withdraw your own approval")
    if decision == "approve" and any(p.subject == subject and p.decision == "approve" for p in prior):
        raise ApprovalError("duplicate", "already approved by this person")
    roles = sorted(r.value for r in directory.grants.get((subject, action.target.service), set()))
    d = Decision(approval_id=approval_id, action_id=action.action_id, action_digest=action.digest,
                 decision=decision, subject=subject, role=",".join(roles), auth_time=auth_time.isoformat(),
                 mfa=mfa, policy_version=policy.version, decided_at=now.isoformat(),
                 valid_until=(now + policy.valid_for).isoformat() if decision == "approve" else None,
                 reason=reason, revokes=revokes)
    return signer.sign(d)


class Standing(str, Enum):
    APPROVED = "approved"
    PENDING = "pending"
    REJECTED = "rejected"
    EXPIRED = "expired"


def evaluate(action: ImmutableAction, decisions: list[Decision], *, now: datetime, keys: dict[str, Ed25519PublicKey],
             directory: Directory, policy: ApprovalPolicy, requested_at: datetime) -> tuple[Standing, list[str]]:
    """Q11.57–Q11.63. Re-run at execution time by the dispatcher, against the CURRENT directory and policy."""
    notes: list[str] = []
    valid = [d for d in decisions if verify_signature(d, keys) and d.action_digest == action.digest
             and d.action_id == action.action_id]
    if len(valid) != len(decisions):
        notes.append(f"{len(decisions) - len(valid)} decision(s) ignored: bad signature or digest")
    if any(d.decision == "reject" for d in valid):
        return Standing.REJECTED, notes + ["a rejection always wins (Q11.60)"]
    revoked = {d.revokes for d in valid if d.decision == "revoke"}
    live = []
    for d in valid:
        if d.decision != "approve" or d.approval_id in revoked:
            continue
        if now > datetime.fromisoformat(d.valid_until):
            notes.append(f"{d.approval_id} expired")
            continue
        if not directory.can(d.subject, action.target.service, Perm.APPROVE):
            notes.append(f"{d.approval_id}: {d.subject} no longer holds approval rights (Q11.59)")
            continue
        if d.policy_version != policy.version:
            notes.append(f"{d.approval_id}: given under {d.policy_version}, current is {policy.version}; re-approval needed")
            continue
        live.append(d)
    need = policy.approvers_high_impact if action.high_impact else policy.approvers_normal
    if len({d.subject for d in live}) >= need:
        return Standing.APPROVED, notes
    if now - requested_at > policy.request_expires:
        return Standing.EXPIRED, notes
    return Standing.PENDING, notes
