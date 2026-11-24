"""Effort presets and layered config resolution (spec §3.1, App. A1).

Merge order: built-in defaults -> effort preset -> project config file -> explicit user params.
An explicit ``null`` clears an optional limit; an absent key inherits; ``0`` stays ``0``.
The parser really expands defaults; JSON-Schema ``default`` is never relied upon.
"""
from __future__ import annotations

import copy
from typing import Any, Optional

from pydantic import ValidationError

from ..models.common import sha256_json
from ..models.config import RunConfig
from .errors import SRError

MiB = 1024 * 1024
GiB = 1024 * MiB

EFFORT_PRESETS: dict[str, dict[str, Any]] = {
    "quick": dict(max_active_seconds=600, max_tasks=12, max_model_requests=24, max_total_tokens=40_000,
                  max_search_requests=12, max_external_fulltexts=4, max_concurrency=1,
                  max_download_bytes=256 * MiB, max_cpu_seconds=600),
    "standard": dict(max_active_seconds=2700, max_tasks=48, max_model_requests=96, max_total_tokens=180_000,
                     max_search_requests=60, max_external_fulltexts=12, max_concurrency=2,
                     max_download_bytes=1 * GiB, max_cpu_seconds=3600),
    "deep": dict(max_active_seconds=7200, max_tasks=120, max_model_requests=240, max_total_tokens=500_000,
                 max_search_requests=160, max_external_fulltexts=30, max_concurrency=3,
                 max_download_bytes=2 * GiB, max_cpu_seconds=7200),
    "reproduction": dict(max_active_seconds=14400, max_tasks=160, max_model_requests=320,
                         max_total_tokens=750_000, max_search_requests=200, max_external_fulltexts=40,
                         max_concurrency=3, max_download_bytes=5 * GiB, max_cpu_seconds=14400),
}
EFFORT_PRESETS["custom"] = EFFORT_PRESETS["standard"]  # custom inherits standard resources

# Which checks run at each effort (profile check priority threshold) - spec §3.2.
EFFORT_MAX_PRIORITY = {"quick": 1, "standard": 2, "custom": 2, "deep": 3, "reproduction": 3}

BUILTIN_DEFAULTS: dict[str, Any] = {
    "schema_version": "1.0.0",
    "workspace": ".scholarly-review",
    "budget_account_id": None,
    "input": {"supplements": [], "source_files": [], "code_repositories": [], "data_files": []},
    "mode": "author",
    "effort": "standard",
    "execution_mode": "interactive",
    "host": {"name": "claude", "model": "host_current", "required_capabilities": []},
    # AI-first product: ml-v1 is the default domain profile (documented deviation from the A1 example).
    "scope": {"domain_profile": "ml-v1", "reviewer_expertise": "general", "focus_claim_ids": [],
              # Only restrict suggestions when the user says new experiments are impossible (--no-new-experiments).
              "include_appendices": True, "allow_new_experiments": True},
    "budget": {
        "absolute_deadline": None, "max_input_tokens": None, "max_output_tokens": None,
        "max_estimated_cost": None, "currency": "USD", "max_gpu_seconds": 0, "max_attempts": 2,
        "verification_reserve_ratio": 0.20, "finalization_reserve_ratio": 0.10,
        "require_request_bounded_cost": False, "require_runner_enforced_time": False,
    },
    "permissions": {
        "classification": "restricted", "host_model_processing": "allowed", "other_model_providers": [],
        "cloud_ocr_providers": [], "search_providers": ["arxiv", "openalex"],
        "search_disclosure": "public_concepts_only", "code_execution": "denied", "paid_services": [],
        "source_modification": "denied", "authorization_basis": "explicit_request",
    },
    "rubric": {"profile": None, "source_file": None, "venue": None, "submission_cutoff": None,
               "score_requested": False, "decision_requested": False},
    "outputs": {"language": "zh-CN", "audience": None, "formats": ["txt", "md", "json", "html"],
                "max_external_references": 4, "include_original_materials": False},
    "extensions": {},
}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def resolve_config(project_config: Optional[dict] = None, user_params: Optional[dict] = None) -> tuple[RunConfig, dict]:
    """Return (validated config, provenance map of which layer set each top-level budget key)."""
    project_config = project_config or {}
    user_params = user_params or {}
    probe = deep_merge(deep_merge(BUILTIN_DEFAULTS, project_config), user_params)
    effort = probe.get("effort", "standard")
    if effort not in EFFORT_PRESETS:
        raise SRError("invalid_input", f"unknown effort '{effort}'")

    merged = deep_merge(BUILTIN_DEFAULTS, {"budget": EFFORT_PRESETS[effort]})
    provenance = {k: "effort_preset" for k in EFFORT_PRESETS[effort]}
    merged = deep_merge(merged, project_config)
    for k in (project_config.get("budget") or {}):
        provenance[k] = "project_config"
    merged = deep_merge(merged, user_params)
    for k in (user_params.get("budget") or {}):
        provenance[k] = "user_explicit"

    # Derived defaults.
    if merged["outputs"].get("audience") is None:
        merged["outputs"]["audience"] = "author_private" if merged["mode"] == "author" else "reviewer_private"
    if merged["rubric"].get("profile") is None:
        merged["rubric"]["profile"] = merged["scope"]["domain_profile"]

    try:
        cfg = RunConfig.model_validate(merged)
    except ValidationError as e:
        raise SRError("schema_invalid", "configuration is invalid", details={"errors": _errs(e)}) from e

    # Mandatory requirements that this version cannot honour fail the plan (never silently flipped).
    if cfg.execution_mode == "managed":
        raise SRError("missing_capability", "managed execution (codex exec / claude -p workers) is not implemented "
                      "in v0.1; use execution_mode=interactive", details={"supported": ["interactive"]})
    if cfg.budget.require_request_bounded_cost:
        raise SRError("missing_capability", "interactive mode cannot bound model cost per request; "
                      "require_request_bounded_cost=true cannot be honoured")
    if cfg.budget.require_runner_enforced_time:
        raise SRError("missing_capability", "interactive mode only gates task claims by time (advisory); "
                      "require_runner_enforced_time=true cannot be honoured")
    if cfg.permissions.classification == "local_only" and cfg.permissions.host_model_processing == "allowed" \
            and cfg.host.name in ("codex", "claude"):
        raise SRError("policy_denied", "local_only forbids sending manuscript text to a cloud host model")
    if cfg.permissions.code_execution != "denied" and cfg.budget.max_gpu_seconds is None:
        raise SRError("invalid_input", "code execution requires an explicit max_gpu_seconds (0 forbids GPU)")
    return cfg, provenance


def config_hash(cfg: RunConfig) -> str:
    return sha256_json(cfg.model_dump(mode="json"))


def _errs(e: ValidationError) -> list[dict]:
    return [{"loc": ".".join(str(p) for p in err["loc"]), "msg": err["msg"]} for err in e.errors()]
