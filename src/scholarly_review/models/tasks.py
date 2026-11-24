"""Task contracts (App. A4) and the model-facing submission schemas.

Workers never mint record IDs: they use local ``key`` values that the kernel maps to
UUIDs, and they never set dispositions, validity or report eligibility (spec §5.3).
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field, model_validator

from .common import (
    AccessLevel, AmountBasis, AssessmentOutcome, ClaimType, ContextIndependence, Impact,
    Importance, Strict, VerificationResult,
)

TaskType = Literal["understand", "check", "numeric_extract", "literature", "verify", "reproduce_plan", "report"]


class InputArtifactRef(Strict):
    artifact_id: str
    sha256: str
    read_scope: str


class TaskRequest(Strict):
    workspace_id: str
    paper_id: str
    paper_version_id: str
    run_id: str
    task_id: str
    attempt_id: str
    lease_generation: int
    task_token: str
    task_type: TaskType
    check_type: Optional[str]
    execution_role: Literal["worker"] = "worker"
    parent_run_id: str
    input_artifacts: list[InputArtifactRef]
    prompt_version: str
    output_schema_id: str
    allowed_paths: list[str]
    permitted_tools: list[str]
    policy_id: str
    budget_allocation: dict
    deadline: Optional[str]
    context_independence: ContextIndependence
    output_staging_directory: str


class Usage(Strict):
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    reported_total_tokens: Optional[int] = None
    cost_amount: Optional[str] = None
    currency: Optional[str] = None
    basis: AmountBasis = "unknown"
    provider_call_ids: list[str] = Field(default_factory=list)


class TaskResult(Strict):
    task_id: str
    attempt_id: str
    lease_generation: int
    input_fingerprint: str
    execution_status: Literal["completed", "partial", "failed", "cancelled"]
    result_artifact_id: Optional[str]
    result_sha256: Optional[str]
    evidence_ids: list[str]
    usage: Usage
    error: Optional[dict]
    warnings: list[str]
    incomplete_reasons: list[str]


# ======================= submission payloads =======================
class QuoteRef(Strict):
    """A verbatim quote from one DocumentMap block; the kernel resolves match_status."""

    block_id: str
    quote: str = Field(min_length=3)


class PaperSummary(Strict):
    title: Optional[str] = None
    research_problem: str
    task: str
    method: str
    contributions: list[str]
    assumptions: list[str]
    data: list[str]
    main_results: list[str]
    paper_type: Literal["empirical", "systems", "theory", "mixed", "survey", "position"]
    detected_domain: Literal["ml", "systems", "security", "other"]
    ai_subfields: list[Literal["llm_nlp", "vision_multimodal", "rl", "ml_theory", "agents", "graph", "other_ml"]] = Field(default_factory=list)


class SubmitClaim(Strict):
    key: str
    original_text: str = Field(min_length=1)
    normalized_text: str = Field(min_length=1)
    scope: str
    origin: Literal["explicit", "paraphrase"]
    importance: Importance
    claim_types: list[ClaimType] = Field(min_length=1)
    source_refs: list[QuoteRef] = Field(min_length=1)


class SubmitHypothesis(Strict):
    key: str
    question: str
    motivation: str
    related_claims: list[str]  # claim keys from this submission
    source_refs: list[QuoteRef] = Field(default_factory=list)


class UnderstandOutput(Strict):
    summary: PaperSummary
    claims: list[SubmitClaim]
    hypotheses: list[SubmitHypothesis] = Field(default_factory=list)
    analyzed_block_ids: list[str]
    uncovered_notes: list[str] = Field(default_factory=list)


class SubmitEvidence(Strict):
    key: str
    kind: Literal["manuscript", "supplement", "literature", "code", "user_assertion"]
    content_summary: str = Field(min_length=1)
    source_refs: list[QuoteRef] = Field(default_factory=list)
    literature_source_id: Optional[str] = None
    section_hint: Optional[str] = None
    method: str
    assumptions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _anchors(self):
        if self.kind in ("manuscript", "supplement") and not self.source_refs:
            raise ValueError("manuscript/supplement evidence requires at least one quote")
        if self.kind == "literature" and not self.literature_source_id:
            raise ValueError("literature evidence requires literature_source_id from `sreview lit search`")
        return self


class SubmitAssessment(Strict):
    claim_id: str
    outcome: AssessmentOutcome
    evidence_keys: list[str]
    scope_checked: str
    limitations: list[str] = Field(default_factory=list)


ActionType = Literal["correct_fact", "add_evidence", "clarify_scope", "improve_writing"]


class SubmitFinding(Strict):
    key: str
    kind: Literal["strength", "concern", "clarification"]
    proposition: str = Field(min_length=1)
    claim_ids: list[str]
    hypothesis_ids: list[str] = Field(default_factory=list)
    evidence_keys: list[str] = Field(min_length=1)
    potential_impact: Impact
    impact_rationale: str
    confidence_basis: str
    recommended_action: Optional[str] = None
    action_type: Optional[ActionType] = None
    neutral_question: Optional[str] = None
    issue_type: Optional[Literal[
        "missing_experiment", "novelty", "numeric", "theory", "attribution", "statistics",
        "leakage", "baseline", "reproducibility", "consistency", "citation", "presentation", "scope", "other",
    ]] = None

    @model_validator(mode="after")
    def _concern_question(self):
        if self.kind == "concern" and not self.neutral_question:
            raise ValueError("concerns require a neutral_question for blinded verification")
        return self


class CheckOutput(Strict):
    check_type: str
    evidence: list[SubmitEvidence]
    assessments: list[SubmitAssessment]
    findings: list[SubmitFinding]
    analyzed_block_ids: list[str]
    not_checked: list[str] = Field(default_factory=list)


class NumericCheckSpec(Strict):
    key: str
    op: Literal[
        "confusion_metrics", "normalized_confusion", "change", "mean", "sum",
        "weighted_mean", "rounding_compatible", "mean_difference", "product", "ratio",
    ]
    inputs: dict
    claimed: dict[str, str] = Field(default_factory=dict)
    claim_ids: list[str] = Field(default_factory=list)
    source_refs: list[QuoteRef] = Field(min_length=1)
    description: str


class NumericExtractOutput(Strict):
    checks: list[NumericCheckSpec]
    analyzed_block_ids: list[str]
    not_extractable: list[str] = Field(default_factory=list)


class SubmitVerification(Strict):
    finding_id: str
    method: str
    result: VerificationResult
    rationale: str
    supporting_evidence_keys: list[str] = Field(default_factory=list)
    counterevidence_keys: list[str] = Field(default_factory=list)
    confirmed_impact: Optional[Impact] = None
    limitations: list[str] = Field(default_factory=list)
    inputs_rechecked: bool = False  # numeric findings: source values were re-read from the original


class VerifyOutput(Strict):
    context_independence: ContextIndependence
    blinded: bool
    evidence: list[SubmitEvidence] = Field(default_factory=list)
    verifications: list[SubmitVerification]


class MetricRule(Strict):
    name: str
    definition: str
    unit: Optional[str]
    reported_value: Optional[str]
    comparison: Literal["exact", "rounded_interval", "absolute", "relative", "statistical", "descriptive"]
    absolute_tolerance: Optional[str] = None
    relative_tolerance: Optional[str] = None
    randomness_protocol: Optional[str] = None


class ExperimentPlanDraft(Strict):
    key: str
    claim_ids: list[str]
    run_variant: Literal["original", "environment_adapted", "patched"]
    entrypoint: str
    arguments: list[str]
    working_directory: str
    resources: dict
    metrics: list[MetricRule]
    timeout_seconds: int = Field(ge=1)
    rationale: str


class ReproducePlanOutput(Strict):
    plans: list[ExperimentPlanDraft]
    static_notes: list[str] = Field(default_factory=list)


OUTPUT_MODELS = {
    "understand": UnderstandOutput,
    "check": CheckOutput,
    "literature": CheckOutput,
    "numeric_extract": NumericExtractOutput,
    "verify": VerifyOutput,
    "reproduce_plan": ReproducePlanOutput,
}


class Submission(Strict):
    """File passed to `sreview task submit --result`."""

    output: dict
    usage: Usage = Field(default_factory=Usage)
    idempotency_key: Optional[str] = None


AccessLevelT = AccessLevel
