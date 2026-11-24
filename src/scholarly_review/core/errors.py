"""Unified error protocol and exit codes (spec §11.5, §13.4)."""
from __future__ import annotations

from typing import Any, Optional

EXIT_CODES = {
    "invalid_input": 2,
    "schema_invalid": 2,
    "semantic_invalid": 2,
    "policy_denied": 3,
    "budget_exhausted": 4,
    "material_error": 5,
    "parse_failed": 5,
    "missing_capability": 6,
    "provider_rate_limited": 7,
    "provider_timeout": 7,
    "provider_failed": 7,
    "unknown_outcome": 7,
    "artifact_corrupt": 8,
    "resource_exhausted": 9,
    "internal_error": 9,
    "cancelled": 10,
    "conflict": 2,
    "not_found": 2,
    "invalid_state": 2,
}


class SRError(Exception):
    def __init__(
        self,
        category: str,
        message: str,
        *,
        code: Optional[str] = None,
        retryable: bool = False,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.code = code or category
        self.message = message
        self.retryable = retryable
        self.details = details or {}

    @property
    def exit_code(self) -> int:
        return EXIT_CODES.get(self.category, 9)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "category": self.category,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }
