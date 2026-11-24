"""RunConfig with the exact keys of spec Appendix A1."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field

from .common import Audience, Effort, ExecutionMode, Mode, Strict

DomainProfile = Literal["generic-v1", "ml-v1", "systems-v1", "security-v1"]


class CodeRepository(Strict):
    path: str
    revision: Optional[str] = None


class InputSpec(Strict):
    manuscript: str = Field(min_length=1)
    supplements: list[str] = Field(default_factory=list)
    source_files: list[str] = Field(default_factory=list)
    code_repositories: list[CodeRepository] = Field(default_factory=list)
    data_files: list[str] = Field(default_factory=list)


class HostSpec(Strict):
    name: Literal["codex", "claude", "api", "local"]
    model: str = "host_current"
    required_capabilities: list[str] = Field(default_factory=list)
    # Extension (spec §5.2): verification tasks run by a kernel-launched isolated worker even in
    # interactive mode; the host never claims them.
    verify_worker: Literal["none", "claude"] = "none"
    # Analysis tasks (understand, numeric, checks, reproduction plans) in short-lived workers as well:
    # each worker gets one task bundle inline, so the host context no longer grows task by task.
    analysis_worker: Literal["none", "claude"] = "none"
    worker_model: Optional[str] = None


class ScopeSpec(Strict):
    domain_profile: DomainProfile
    reviewer_expertise: Literal["general", "generalist", "specialist"] = "general"
    focus_claim_ids: list[str] = Field(default_factory=list)
    include_appendices: bool = True
    allow_new_experiments: bool = False


class Budget(Strict):
    max_active_seconds: Optional[int] = Field(ge=0)
    absolute_deadline: Optional[str]
    max_tasks: Optional[int] = Field(ge=0)
    max_model_requests: Optional[int] = Field(ge=0)
    max_input_tokens: Optional[int] = Field(ge=0)
    max_output_tokens: Optional[int] = Field(ge=0)
    max_total_tokens: Optional[int] = Field(ge=0)
    max_estimated_cost: Optional[str]  # decimal string, never float
    currency: str = "USD"
    max_search_requests: Optional[int] = Field(ge=0)
    max_download_bytes: Optional[int] = Field(ge=0)
    max_external_fulltexts: Optional[int] = Field(ge=0)
    max_cpu_seconds: Optional[int] = Field(ge=0)
    max_gpu_seconds: Optional[int] = Field(ge=0)
    max_concurrency: int = Field(ge=1)
    max_attempts: int = Field(ge=1)
    verification_reserve_ratio: float = Field(ge=0, le=0.9)
    finalization_reserve_ratio: float = Field(ge=0, le=0.9)
    require_request_bounded_cost: bool = False
    require_runner_enforced_time: bool = False


class Permissions(Strict):
    classification: Literal["local_only", "restricted", "public"]
    host_model_processing: Literal["allowed", "denied"]
    other_model_providers: list[str] = Field(default_factory=list)
    cloud_ocr_providers: list[str] = Field(default_factory=list)
    search_providers: list[str] = Field(default_factory=list)
    search_disclosure: Literal["public_concepts_only", "title_allowed", "none"] = "public_concepts_only"
    code_execution: Literal["denied", "approved_plans_only"] = "denied"
    paid_services: list[str] = Field(default_factory=list)
    source_modification: Literal["denied", "patch_only"] = "denied"
    authorization_basis: str


class RubricSpec(Strict):
    profile: str
    source_file: Optional[str]
    venue: Optional[str]
    submission_cutoff: Optional[str]  # YYYY-MM-DD
    score_requested: bool = False
    decision_requested: bool = False


class OutputSpec(Strict):
    language: Literal["zh-CN", "en"] = "zh-CN"
    audience: Audience
    formats: list[Literal["txt", "md", "json", "html", "docx"]]
    max_external_references: int = Field(ge=0)
    include_original_materials: bool = False


class RunConfig(Strict):
    schema_version: str = "1.0.0"
    workspace: str = ".scholarly-review"
    budget_account_id: Optional[str]
    input: InputSpec
    mode: Mode
    effort: Effort
    execution_mode: ExecutionMode
    host: HostSpec
    scope: ScopeSpec
    budget: Budget
    permissions: Permissions
    rubric: RubricSpec
    outputs: OutputSpec
    extensions: dict = Field(default_factory=dict)
