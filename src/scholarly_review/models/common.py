"""Shared primitives: strict base model, ids, time, enumerations (spec §9, App. A)."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

SCHEMA_VERSION = "1.0.0"


class Strict(BaseModel):
    """All external input rejects unknown fields (spec §5.2)."""

    model_config = ConfigDict(extra="forbid")


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(obj: Any) -> str:
    return sha256_bytes(canonical_json(obj).encode("utf-8"))


# ---- enumerations (spec §9.3, §9.4, App. A3) ----
Mode = Literal["author", "reviewer"]
Effort = Literal["quick", "standard", "deep", "reproduction", "custom"]
ExecutionMode = Literal["interactive", "managed"]

TaskState = Literal[
    "planned", "ready", "running", "completed", "partial", "blocked",
    "failed", "cancelled", "skipped", "stale",
]
BlockedReason = Literal["policy", "budget", "missing_input", "missing_capability", "dependency"]
RunState = Literal[
    "created", "planned", "running", "pausing", "paused", "finalizing",
    "completed", "partial", "blocked", "failed", "cancelled",
]
AssessmentOutcome = Literal[
    "supported", "contradicted", "insufficient_evidence", "unresolved", "not_applicable", "not_checked",
]
FindingDisposition = Literal["candidate", "confirmed", "refuted", "unresolved", "dismissed"]
FindingKind = Literal["strength", "concern", "clarification"]
Impact = Literal["critical", "major", "minor", "informational", "not_assessed"]
Validity = Literal["current", "stale", "corrupt"]
EvidenceKind = Literal[
    "manuscript", "supplement", "literature", "code", "calculation", "execution", "user_assertion",
]
AccessLevel = Literal[
    "metadata", "abstract", "partial_fulltext", "fulltext", "supplied_primary", "local_computation",
]
MatchStatus = Literal["exact", "fuzzy", "ambiguous", "unresolved"]
ContextIndependence = Literal["fresh", "inherited", "same_context", "human"]
VerificationResult = Literal["validated", "invalidated", "inconclusive"]
ClaimType = Literal["empirical", "theoretical", "comparative", "efficiency", "generality", "reproducibility"]
Importance = Literal["core", "supporting", "peripheral"]
Audience = Literal["author_private", "reviewer_private", "authors", "editor", "public"]
Readiness = Literal["blocked", "conditional", "no_blocker_found", "not_assessed"]
BillingState = Literal["reserved", "in_flight", "settled", "pending_reconciliation", "released"]
AmountBasis = Literal["observed", "estimated", "unknown"]
ActorType = Literal["program", "model", "human"]

TERMINAL_TASK_STATES = {"completed", "partial", "blocked", "failed", "cancelled", "skipped", "stale"}
SUCCESS_TASK_STATES = {"completed", "partial"}
TERMINAL_RUN_STATES = {"completed", "partial", "blocked", "failed", "cancelled"}
IMPACT_RANK = {"critical": 4, "major": 3, "minor": 2, "informational": 1, "not_assessed": 0}


class Producer(Strict):
    component: str
    component_version: str
    actor_type: ActorType
    actor_id: str
