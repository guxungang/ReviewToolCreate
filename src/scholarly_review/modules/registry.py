"""Domain profiles (RubricProfile, App. A5) and the check registry.

Domain profiles extend generic-v1 by adding checks and applicability rules; they never
override the generic citation, evidence and uncertainty requirements.
"""
from __future__ import annotations

import copy
import json
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Optional

from ..core.errors import SRError

PROFILE_IDS = ("generic-v1", "ml-v1", "systems-v1", "security-v1")
DOMAIN_OF_PROFILE = {"generic-v1": None, "ml-v1": "ml", "systems-v1": "systems", "security-v1": "security"}
NUMERIC_CHECK = "numeric.consistency"
LITERATURE_CHECK = "core.novelty_positioning"
REQUIRED_PROFILE_KEYS = ("profile_id", "version", "domain", "source_urls", "retrieved_at", "valid_from",
                         "submission_cutoff", "criteria", "score_scales", "policy_refs")


def _read(profile_id: str) -> dict:
    if profile_id not in PROFILE_IDS:
        raise SRError("invalid_input", f"unknown domain profile '{profile_id}'", details={"known": PROFILE_IDS})
    text = resources.files("scholarly_review.profiles").joinpath(f"{profile_id}.json").read_text("utf-8")
    data = json.loads(text)
    missing = [k for k in REQUIRED_PROFILE_KEYS if k not in data]
    if missing:
        raise SRError("schema_invalid", f"profile {profile_id} misses keys {missing}")
    return data


@lru_cache(maxsize=None)
def load_profile(profile_id: str) -> dict:
    data = _read(profile_id)
    parent_id = data.get("extends")
    if parent_id:
        parent = copy.deepcopy(load_profile(parent_id))
        own = {c["check_type"] for c in data["checks"]}
        data["checks"] = [c for c in parent["checks"] if c["check_type"] not in own] + data["checks"]
        data["subfield_checks"] = parent.get("subfield_checks", []) + data.get("subfield_checks", [])
        data["criteria"] = parent["criteria"] + data["criteria"]
    for c in data["checks"] + data.get("subfield_checks", []):
        c.setdefault("subfields", [])
    return data


def load_rubric_file(path: str) -> dict:
    """User-supplied venue rubric: only validated keys are used; scales are never invented."""
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SRError("invalid_input", f"rubric file unreadable: {e}") from e
    missing = [k for k in REQUIRED_PROFILE_KEYS if k not in data]
    if missing:
        raise SRError("schema_invalid", f"rubric file misses keys {missing}")
    for s in data["score_scales"]:
        for k in ("criterion_id", "allowed_values", "value_definitions", "required"):
            if k not in s:
                raise SRError("schema_invalid", f"score scale misses '{k}'")
    return data


def check_spec(profile: dict, check_type: str) -> Optional[dict]:
    if check_type == NUMERIC_CHECK:
        return {"check_type": NUMERIC_CHECK, "module": "numeric", "priority": 1, "name": "Numeric consistency",
                "name_zh": "数值一致性", "questions": [], "evidence_needed": [], "must_not": [], "subfields": [],
                "applicable_paper_types": ["empirical", "systems", "mixed"]}
    for c in profile["checks"] + profile.get("subfield_checks", []):
        if c["check_type"] == check_type:
            return c
    return None


# quick = focused scope (abstract, contributions, key results, contradictions, recomputable numbers,
# closest prior work). Measured host time leaves room for exactly these in the 70 % analysis phase.
QUICK_CHECKS = ("core.claim_evidence", LITERATURE_CHECK)


def base_checks(profile: dict, max_priority: int, effort: str = "standard") -> list[dict]:
    checks = (c for c in profile["checks"] if c["priority"] <= max_priority)
    if effort == "quick":
        checks = (c for c in checks if c["check_type"] in QUICK_CHECKS)
    return sorted(checks, key=lambda c: (c["priority"], c["check_type"]))


def subfield_checks(profile: dict, subfields: list[str], max_priority: int) -> list[dict]:
    want = set(subfields)
    return [c for c in profile.get("subfield_checks", [])
            if c["priority"] <= max_priority and want.intersection(c["subfields"])]


def all_check_types(profile: dict) -> set[str]:
    return {c["check_type"] for c in profile["checks"] + profile.get("subfield_checks", [])} | {NUMERIC_CHECK}
