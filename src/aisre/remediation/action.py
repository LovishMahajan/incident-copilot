"""Artifact 11.6–11.8: immutable actions, the approval record and its signature, and the
authorization matrix.

An action is created once, from a tested patch, and never changes; its digest binds patch,
source revision, exact target, policy version, test result and recovery predicates. An approval
signs that digest — not the patch content, and not anything the caller supplies. A rollback is
its own action, and its approval can be signed with the fix's as a conditional row (ADR-48).
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Literal, Optional, get_args

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


ActionKind = Literal["config_revert", "commit_revert", "release_rollback", "rollback"]


@dataclass(frozen=True)
class ImmutableAction:
    action_id: str                         # stable identity; also the execution key (Q11.70, ADR-47)
    incident_id: str
    diagnosis_id: str
    kind: ActionKind
    patch_sha256: Optional[str]            # None for a release rollback without files, and for a rollback
    base_revision: str                     # the revision the change was generated and tested against (Q11.56)
    expected_after_revision: str           # what the target should run afterwards; required for every kind
    target: Target
    policy_version: str                    # edit policy + approval policy in force at creation (Q11.57)
    test_bundle_digest: str                # the passing harness result for this exact patch
    recovery_predicates_digest: str        # what "recovered" means, fixed before execution
    high_impact: bool                      # can only add to is_high_impact, never lower it (ADR-49)
    hold_seconds: int = 600                # the hold period, from the service catalogue (ADR-66)
    parent_action_id: Optional[str] = None # rollback -> the action it reverses (Q11.91)
    created_at: str = ""

    def __post_init__(self):
        if self.kind not in get_args(ActionKind):
            raise ValueError(f"unknown action kind {self.kind}")
        if not self.expected_after_revision:
            raise ValueError("every action names the revision expected afterwards (C17)")
        if (self.kind == "rollback") != (self.parent_action_id is not None):
            raise ValueError("a rollback, and only a rollback, names its parent action")

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical(asdict(self))).hexdigest()


@dataclass(frozen=True)
class ImpactFacts:
    """What the high-impact rule reads beyond the action, from the incident record and the service
    catalogue. Loaded from trusted storage, never from a caller."""

    severity: Optional[str]                # the confirmed severity; None until a person confirms one
    critical: bool = False                 # the service is marked `critical` in the catalogue
    deployments: int = 1                   # how many deployments this same change is proposed for
    parent_kind: Optional[str] = None      # a rollback: the kind of the action it reverses


def is_high_impact(action: ImmutableAction, impact: ImpactFacts) -> bool:
    """ADR-49. Derived from the action, target and severity, so a False flag changes nothing. A True
    flag stands: a rollback copies its parent's. Severity not yet confirmed counts as high-impact."""
    return (action.high_impact or "release_rollback" in (action.kind, impact.parent_kind)
            or impact.severity in (None, "sev1") or impact.critical or impact.deployments > 1)


# ---------------------------------------------------------------------------
# 11.7 Authorization matrix
# ---------------------------------------------------------------------------
class Role(str, Enum):
    VIEWER = "viewer"
    RESPONDER = "responder"
    APPROVER = "approver"
    INCIDENT_LEAD = "incident_lead"
    MONITORING = "monitoring"              # opens incidents from alerts; nothing else
    PLATFORM_ADMIN = "platform_admin"      # runs the platform; cannot approve changes
    SECURITY_LEAD = "security_lead"        # owns policies, keys and sandbox configuration; cannot approve changes
    SYSTEM_WORKFLOW = "system:workflow"    # the platform's own identities
    SYSTEM_DISPATCHER = "system:dispatcher"


class Perm(str, Enum):
    READ = "incident:read"
    CREATE = "incident:create"
    NOTE = "incident:note"
    CANCEL = "incident:cancel"
    REINVESTIGATE = "incident:reinvestigate"
    CONFIRM_SEVERITY = "incident:confirm_severity"
    CLOSE = "incident:close"
    APPROVE = "action:approve"
    REJECT = "action:reject"
    REVOKE_OWN = "approval:revoke_own"
    RESOLVE_OUTCOME = "action:resolve_outcome"
    EXECUTE = "action:execute"
    AUDIT_EXPORT = "audit:export"
    SET_FENCE = "fence:set"
    OPERATE = "platform:operate"           # lift a fence, run a restore
    EDIT_POLICY = "policy:edit"
    MANAGE_SANDBOX = "sandbox:configure"
    MANAGE_KEYS = "keys:manage"


_RESPONDER = {Perm.READ, Perm.CREATE, Perm.NOTE, Perm.CANCEL, Perm.REJECT, Perm.REINVESTIGATE}
_APPROVER = _RESPONDER | {Perm.APPROVE, Perm.REVOKE_OWN}

MATRIX: dict[Role, set[Perm]] = {                                   # ADR-50, Artifact 11.7
    Role.VIEWER: {Perm.READ},
    Role.RESPONDER: _RESPONDER,
    Role.APPROVER: _APPROVER,
    Role.INCIDENT_LEAD: _APPROVER | {Perm.CONFIRM_SEVERITY, Perm.CLOSE, Perm.AUDIT_EXPORT, Perm.RESOLVE_OUTCOME,
                                     Perm.SET_FENCE},
    Role.MONITORING: {Perm.CREATE},
    Role.PLATFORM_ADMIN: {Perm.READ, Perm.SET_FENCE, Perm.OPERATE},
    Role.SECURITY_LEAD: {Perm.EDIT_POLICY, Perm.MANAGE_KEYS, Perm.MANAGE_SANDBOX},
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
    edit_version: str = "edit-policy@1"                # the EditPolicy in force (patch.py)
    valid_for: timedelta = timedelta(minutes=15)       # execute within this after approval
    conditional_margin: timedelta = timedelta(minutes=15)   # ADR-48: time to deploy the fix
    escalates: timedelta = timedelta(minutes=15)       # workflow timer on an unanswered request
    request_expires: timedelta = timedelta(minutes=60)
    max_auth_age: timedelta = timedelta(minutes=15)    # recent sign-in
    approvers_normal: int = 1
    approvers_high_impact: int = 2

    @property
    def pair(self) -> str:
        """Both policies, in the form actions and decisions record them (C19)."""
        return f"{self.edit_version}|{self.version}"

    def approvers_needed(self, action: ImmutableAction, impact: ImpactFacts) -> int:
        return self.approvers_high_impact if is_high_impact(action, impact) else self.approvers_normal


@dataclass(frozen=True)
class Decision:
    approval_id: str
    action_id: str
    action_digest: str                 # binds to the exact immutable action (Q11.54)
    decision: Literal["approved", "rejected", "revoked"]
    subject: str
    role: str
    auth_time: str
    mfa: bool
    policy_version: str                # edit-policy@N|approval-policy@N
    decided_at: str
    valid_until: Optional[str]
    reason: Optional[str]
    revokes: Optional[str] = None      # approval_id being withdrawn (Q11.63)
    conditional: bool = False          # a rollback's approval, signed with the fix's (ADR-48)
    conditional_on_approval_id: Optional[str] = None   # the same person's approval of the fix
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
        k.verify(base64.b64decode(d.signature, validate=True), d.payload())
        return True
    except (InvalidSignature, ValueError):             # bad base64 is binascii.Error, a ValueError
        return False


class ApprovalError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code = code


SECURITY_ALERT_CODES = {"reauth_required", "forbidden", "self_approval", "not_own_approval", "not_found",
                        "digest_mismatch"}                 # identity, rights and binding; retries don't alert (11.6)


def record_decision(*, action: ImmutableAction, subject: str, decision: str, auth_time: datetime, mfa: bool,
                    now: datetime, directory: Directory, policy: ApprovalPolicy, signer: Signer,
                    approval_id: str, presented_digest: str, proposer_subjects: set[str],
                    action_status: str, request_open: bool, intent_recorded: bool, events: list[dict],
                    impact: ImpactFacts, change_author: Optional[str] = None, reason: Optional[str] = None,
                    revokes: Optional[str] = None, prior: list[Decision] = (),
                    rollback: Optional[ImmutableAction] = None, rollback_digest: Optional[str] = None,
                    rollback_approval_id: Optional[str] = None, rollback_prior: list[Decision] = ()) -> list[Decision]:
    """The Approval API's core. Accepts only the action ID and the digest the approver saw; never
    patch content or targets (Q11.61). Returns the signed rows, to write in one transaction: the
    decision, then its conditional row if any. `rollback_digest` is the rollback the approver saw,
    and leaving it out gives no conditional approval. `rollback_approval_id` names that row, or the
    revocation of it, found in `rollback_prior`. A refusal writes no row: it appends its
    aisre.approval.refused.v1 event to `events`, with the flag that raises the security alert."""
    def refuse(code: str, detail: str = ""):
        events.append({"type": "aisre.approval.refused.v1",
                       "data": {"action_id": action.action_id, "decision": decision, "code": code,
                                "security_alert": code in SECURITY_ALERT_CODES}})
        raise ApprovalError(code, detail)

    service = action.target.service
    perm = {"approved": Perm.APPROVE, "rejected": Perm.REJECT, "revoked": Perm.REVOKE_OWN}[decision]
    if decision != "rejected" and (now - auth_time > policy.max_auth_age or not mfa):
        refuse("reauth_required", "sign in again with two-factor")                     # Q11.50, ADR-49
    if decision == "approved" and (subject in proposer_subjects or subject.startswith("system:")):
        refuse("self_approval", "proposers and platform identities cannot approve")    # Q11.49
    if subject.startswith("system:"):
        refuse("forbidden", "platform identities cannot decide")
    if not directory.can(subject, service, Perm.READ):
        refuse("not_found", "no such action")                                          # ADR-58: existence not revealed
    if not directory.can(subject, service, perm):
        refuse("forbidden", f"{subject} may not record {decision} for {service}")      # Q11.51
    if decision == "approved" and subject == change_author and policy.approvers_needed(action, impact) < 2:
        refuse("self_approval", "you can't be the sole approver of a revert of your own change")   # ADR-49
    if decision == "revoked" and not any(p.approval_id == revokes and p.subject == subject
                                         and p.decision == "approved" for p in prior):
        refuse("not_own_approval", "you can only withdraw your own approval")
    if decision != "approved" and not reason:
        refuse("reason_required")
    if presented_digest != action.digest:
        refuse("digest_mismatch", "the action changed since you reviewed it")
    if rollback_digest is not None and (rollback is None or rollback_digest != rollback.digest
                                        or rollback.parent_action_id != action.action_id):
        refuse("digest_mismatch", "the rollback changed since you reviewed it")
    if decision == "approved" and any(p.subject == subject and p.decision == "approved" for p in prior):
        refuse("duplicate", "already approved by this person")
    if decision == "revoked" and any(p.decision == "revoked" and p.revokes == revokes for p in prior):
        refuse("duplicate", "already revoked")
    if decision != "approved" and intent_recorded:
        refuse("execution_started", "undoing the change is a rollback action")
    if decision == "approved":
        is_open = action_status == "awaiting_approval" and request_open                # ST-12
    else:
        is_open = action_status in ("awaiting_approval", "approved") or (action_status == "proposed"
                                                                         and action.kind == "rollback")
    if not is_open:
        refuse("action_not_open", f"the action is {action_status}")

    roles = sorted(r.value for r in directory.grants.get((subject, service), set()))

    def signed(**fields) -> Decision:
        return signer.sign(Decision(subject=subject, role=",".join(roles), auth_time=auth_time.isoformat(), mfa=mfa,
                                    policy_version=policy.pair, decided_at=now.isoformat(), reason=reason, **fields))

    valid_until = now + policy.valid_for
    rows = [signed(approval_id=approval_id, action_id=action.action_id, action_digest=action.digest, decision=decision,
                   valid_until=valid_until.isoformat() if decision == "approved" else None, revokes=revokes)]
    if decision == "approved" and rollback_digest is not None:
        lapses = valid_until + timedelta(seconds=action.hold_seconds) + policy.conditional_margin    # ADR-48
        rows.append(signed(approval_id=rollback_approval_id, action_id=rollback.action_id,
                           action_digest=rollback.digest, decision="approved", valid_until=lapses.isoformat(),
                           revokes=None, conditional=True, conditional_on_approval_id=approval_id))
    if decision == "revoked":
        gone = {p.revokes for p in rollback_prior if p.decision == "revoked"}
        rows += [signed(approval_id=rollback_approval_id, action_id=c.action_id, action_digest=c.action_digest,
                        decision="revoked", valid_until=None, revokes=c.approval_id) for c in rollback_prior
                 if c.conditional_on_approval_id == revokes and c.approval_id not in gone]
    return rows


class Standing(str, Enum):
    APPROVED = "approved"
    PENDING = "pending"
    REJECTED = "rejected"
    EXPIRED = "expired"


def evaluate(action: ImmutableAction, decisions: list[Decision], *, now: datetime, keys: dict[str, Ed25519PublicKey],
             directory: Directory, policy: ApprovalPolicy, requested_at: datetime, impact: ImpactFacts,
             parent_verdict: Optional[str] = None) -> tuple[Standing, list[str], Optional[str]]:
    """Q11.57–Q11.63. Re-run at execution time by the dispatcher, against the CURRENT directory and policy.
    `impact` is the trusted input to the high-impact rule. `parent_verdict` is the recorded verification
    verdict of the action a rollback reverses. Returns the
    standing, notes, and when approved the approval_id that completed the count, for the attempt row."""
    notes: list[str] = []
    valid = [d for d in decisions if verify_signature(d, keys) and d.action_digest == action.digest
             and d.action_id == action.action_id]
    if len(valid) != len(decisions):
        notes.append(f"{len(decisions) - len(valid)} decision(s) ignored: bad signature or digest")
    if any(d.decision == "rejected" for d in valid):
        return Standing.REJECTED, notes + ["a rejection always wins (Q11.60)"], None
    revoked = {d.revokes for d in valid if d.decision == "revoked"}
    live = []
    for d in valid:
        if d.decision != "approved" or d.approval_id in revoked:
            continue
        if d.conditional and parent_verdict != "failed":
            notes.append(f"{d.approval_id}: conditional, and the parent has no failed verdict (ADR-48)")
            continue
        if now > datetime.fromisoformat(d.valid_until):
            notes.append(f"{d.approval_id} expired")
            continue
        if not directory.can(d.subject, action.target.service, Perm.APPROVE):
            notes.append(f"{d.approval_id}: {d.subject} no longer holds approval rights (Q11.59)")
            continue
        if d.policy_version != policy.pair:
            notes.append(f"{d.approval_id}: given under {d.policy_version}, current is {policy.pair}; re-approval needed")
            continue
        live.append(d)
    subjects: set[str] = set()
    for d in sorted(live, key=lambda d: d.decided_at):
        subjects.add(d.subject)
        if len(subjects) >= policy.approvers_needed(action, impact):
            return Standing.APPROVED, notes, d.approval_id
    if now - requested_at > policy.request_expires:
        return Standing.EXPIRED, notes, None
    return Standing.PENDING, notes, None
