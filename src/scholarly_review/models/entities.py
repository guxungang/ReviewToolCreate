"""Canonical evidence-layer entities with the exact keys of spec Appendix A3."""
from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import Field, field_validator

from .common import (
    SCHEMA_VERSION, AccessLevel, AssessmentOutcome, Audience, ClaimType, ContextIndependence,
    EvidenceKind, FindingDisposition, FindingKind, Impact, Importance, MatchStatus, Producer,
    Strict, Validity, VerificationResult,
)


# ---------------- locators (App. A3, §9.3) ----------------
class DocumentLocator(Strict):
    type: Literal["document"] = "document"
    block_id: Optional[str]
    page_index: Optional[int] = Field(default=None, ge=1)
    page_label: Optional[str] = None
    bbox: Optional[list[float]] = None  # normalised [x0,y0,x1,y1] or null, never a zero rectangle
    text_span: Optional[list[int]] = None  # [start, end) in code points of the normalised block text
    match_status: MatchStatus

    @field_validator("bbox")
    @classmethod
    def _bbox(cls, v):
        if v is None:
            return v
        if len(v) != 4 or not all(0.0 <= x <= 1.0 for x in v) or v[0] >= v[2] or v[1] >= v[3]:
            raise ValueError("bbox must be normalised [x0,y0,x1,y1] with x0<x1, y0<y1")
        return v


class CodeLocator(Strict):
    type: Literal["code"] = "code"
    repo_commit: Optional[str]
    relative_path: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    snippet_sha256: Optional[str]


class ExternalLocator(Strict):
    type: Literal["external"] = "external"
    url: Optional[str]
    source_id: str
    version_id: Optional[str]
    section_hint: Optional[str]
    access_level: AccessLevel


class ArtifactLocator(Strict):
    type: Literal["artifact"] = "artifact"
    artifact_sha256: str
    json_pointer: Optional[str]


Locator = Annotated[
    Union[DocumentLocator, CodeLocator, ExternalLocator, ArtifactLocator], Field(discriminator="type")
]


class SourceRef(Strict):
    artifact_id: str
    locator: Locator
    quote: Optional[str]


class Disclosure(Strict):
    classification: Literal["local_only", "restricted", "public"]
    allowed_audiences: list[Audience]
    authorization_record_ids: list[str]


# ---------------- persisted record base (App. A2) ----------------
class RecordMeta(Strict):
    record_id: str
    schema_version: str = SCHEMA_VERSION
    created_at: str
    producer: Producer
    validity: Validity = "current"
    workspace_id: str
    paper_id: str
    paper_version_id: str
    run_id: Optional[str]


class Claim(RecordMeta):
    claim_id: str
    original_text: str = Field(min_length=1)
    normalized_text: str = Field(min_length=1)
    scope: str
    origin: Literal["explicit", "paraphrase"]
    importance: Importance
    source_refs: list[SourceRef] = Field(min_length=1)  # author claims need an anchor
    claim_types: list[ClaimType]
    supersedes: Optional[str] = None


class Hypothesis(RecordMeta):
    hypothesis_id: str
    question: str = Field(min_length=1)
    motivation: str = Field(min_length=1)
    related_claim_ids: list[str]
    source_refs: list[SourceRef]


class Evidence(RecordMeta):
    evidence_id: str
    kind: EvidenceKind
    content_summary: str = Field(min_length=1)
    payload_artifact_id: Optional[str]
    source_refs: list[SourceRef]
    method: str
    assumptions: list[str]
    limitations: list[str]
    retrieved_at: Optional[str]
    access_level: AccessLevel
    disclosure: Disclosure


class Assessment(RecordMeta):
    assessment_id: str
    claim_id: str
    check_type: str
    outcome: AssessmentOutcome
    evidence_ids: list[str]
    scope_checked: str
    limitations: list[str]


class ReportEligibility(Strict):
    audience: Audience
    admitted: bool
    category: Literal["strength", "confirmed_concern", "question", "excluded"]
    reason_codes: list[str]
    checked_at: str


class Finding(RecordMeta):
    finding_id: str
    kind: FindingKind
    proposition: str = Field(min_length=1)
    claim_ids: list[str]
    hypothesis_ids: list[str]
    evidence_ids: list[str]
    disposition: FindingDisposition
    impact: Impact
    potential_impact: Impact
    impact_rationale: str
    confidence_basis: str
    verification_ids: list[str]
    duplicate_of: Optional[str] = None
    recommended_action: Optional[str] = None
    report_eligibility: list[ReportEligibility] = Field(default_factory=list)  # kernel-only
    # Not in A3; namespaced extension kept separate from the business keys (spec §5.2).
    extensions: dict = Field(default_factory=dict)


class Verification(RecordMeta):
    verification_id: str
    finding_id: str
    question: str
    input_evidence_ids: list[str]
    input_artifact_ids: list[str]
    method: str
    context_independence: ContextIndependence
    blinded: bool
    supporting_evidence_ids: list[str]
    counterevidence_ids: list[str]
    result: VerificationResult
    rationale: str
    limitations: list[str]


# ---------------- coverage (App. A3) ----------------
class MaterialCoverage(Strict):
    artifact_id: str
    total_blocks: Optional[int]
    readable_blocks: int
    analyzed_blocks: int
    missing_reason: Optional[str]


class ClaimCoverage(Strict):
    claim_id: str
    importance: Importance
    applicable_checks: list[str]
    completed_checks: list[str]
    unresolved_checks: list[str]


class CheckCoverage(Strict):
    check_type: str
    expected_items: Optional[int]
    processed_items: int
    scope_label: str
    remaining_reasons: list[str]


class Coverage(Strict):
    material_items: list[MaterialCoverage]
    claim_items: list[ClaimCoverage]
    check_items: list[CheckCoverage]
    unknown_denominators: list[str]


# ---------------- literature (App. A4) ----------------
class LiteratureRecord(Strict):
    source_id: str
    title: Optional[str]
    authors: Optional[list[str]]
    first_public_date: Optional[str]
    version_public_date: Optional[str]
    venue: Optional[str]
    identifiers: dict
    links: list[str]
    access_level: AccessLevel
    abstract: Optional[str] = None


class SearchResult(Strict):
    records: list[LiteratureRecord]
    provider_status: Literal["success", "partial", "failed"]
    error_reason: Optional[str]
    complete: bool
    truncation: Optional[str]
    next_cursor: Optional[str]
    queries: list[str]
    retrieved_at: str
