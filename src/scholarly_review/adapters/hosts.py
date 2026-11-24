"""Host capability probe and Skill installer (spec §6.3, §6.4, §15.3).

The probe only reads versions and help text: it never sends a manuscript and never makes a
billable call. Capabilities that need a real model call to confirm stay 'unknown'.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from importlib import resources
from pathlib import Path
from typing import Optional

from .. import __version__
from ..core.errors import SRError
from ..models.common import now_utc

SKILLS = ("paper-preflight", "paper-referee")
OWNED_MARKER = ".scholarly-review-owned"
HOSTS = {
    "claude": {"binary": "claude", "project_dir": ".claude/skills", "user_dir": "~/.claude/skills",
               "entry": "/", "noninteractive": "claude -p",
               "embedded_env": "CLAUDE_CODE_EXECPATH", "inside_env": "CLAUDECODE",
               "flags": {"structured_output": "--json-schema", "event_stream": "stream-json",
                         "native_budget_limit": "--max-budget-usd", "native_turn_limit": "--max-turns"}},
    "codex": {"binary": "codex", "project_dir": ".agents/skills", "user_dir": "~/.agents/skills",
              "entry": "$", "noninteractive": "codex exec",
              "flags": {"structured_output": "--output-schema", "event_stream": "--json"}},
}


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def probe(host: str) -> dict:
    if host not in HOSTS:
        raise SRError("invalid_input", f"unknown host '{host}' (codex or claude)")
    spec = HOSTS[host]
    binary = shutil.which(spec["binary"])
    found_via = "PATH" if binary else None
    env_var = spec.get("embedded_env")
    if not binary and env_var and os.environ.get(env_var) and os.access(os.environ[env_var], os.X_OK):
        binary, found_via = os.environ[env_var], f"${env_var} (embedded host, e.g. desktop app)"
    inside = bool(spec.get("inside_env") and os.environ.get(spec["inside_env"]))
    caps: dict[str, str] = {k: "unknown" for k in (
        "interactive_skill", "noninteractive_worker", "structured_output", "event_stream", "usage_report",
        "cost_report", "per_request_output_limit", "native_budget_limit", "native_turn_limit",
        "independent_context", "subagents", "mcp_stdio", "image_input", "sandbox", "cancel_scope")}
    notes: list[str] = []
    version, help_ok = None, False
    if inside:
        caps["interactive_skill"] = "supported"
        notes.append(f"probe is running inside a {host} session, so interactive Skill use is observed directly")
    if not binary:
        # Not finding a CLI is not evidence the host is unavailable (it may be embedded in an app).
        notes.append(f"'{spec['binary']}' was not found on PATH; capabilities stay 'unknown', not 'unsupported'. "
                     "Interactive use inside the host app can still work; managed workers would need the CLI.")
    if binary:
        rc, out = _run([binary, "--version"])
        version = out.strip().splitlines()[0] if rc == 0 and out.strip() else None
        sub = [binary, "exec", "--help"] if host == "codex" else [binary, "--help"]
        rc, help_text = _run(sub)
        help_ok = rc == 0
        if help_ok:
            for cap, flag in spec["flags"].items():
                caps[cap] = "supported" if flag in help_text else "unknown"
            caps["noninteractive_worker"] = "supported" if host == "codex" or " -p" in help_text or "--print" in help_text else "unknown"
    return {
        "host": host, "binary": binary, "binary_found_via": found_via, "host_version": version,
        "adapter_version": __version__, "probed_at": now_utc(),
        "method": "version/help text only (no model call, no manuscript sent)",
        "capabilities": caps,
        "notes": notes + [
            "Capabilities marked 'supported' are inferred from help text; account, version and organisation policy "
            "may still forbid them.",
            "Runs are interactive; verification tasks can run in an isolated kernel-launched worker "
            "(`plan --verify-worker claude`, then `sreview worker verify`)" if host == "claude" else
            "Runs are interactive; isolated verification workers are implemented for claude only.",
            f"Skill entry: {spec['entry']}paper-preflight and {spec['entry']}paper-referee.",
        ],
    }


def install_skills(host: str, scope: str, project_root: Optional[str] = None, dry_run: bool = False) -> dict:
    if host not in HOSTS:
        raise SRError("invalid_input", f"unknown host '{host}'")
    spec = HOSTS[host]
    if scope == "project":
        base = Path(project_root or ".").resolve() / spec["project_dir"]
    elif scope == "user":
        base = Path(spec["user_dir"]).expanduser()
    else:
        raise SRError("invalid_input", "scope must be project or user")
    actions = []
    for name in SKILLS:
        src = resources.files("scholarly_review.skill_sources").joinpath(name).joinpath("SKILL.md").read_text("utf-8")
        text = src.replace("{{ENTRY}}", spec["entry"]).replace("{{HOST}}", host)
        dest = base / name
        if dest.exists() and not (dest / OWNED_MARKER).exists():
            raise SRError("conflict", f"{dest} exists and is not owned by ScholarlyReview; refusing to overwrite",
                          details={"options": ["rename the existing skill", "choose the other scope"]})
        actions.append({"skill": name, "path": str(dest / "SKILL.md"),
                        "action": "update" if dest.exists() else "create"})
        if dry_run:
            continue
        dest.mkdir(parents=True, exist_ok=True)
        if (dest / "SKILL.md").exists():
            (dest / "SKILL.md.bak").write_text((dest / "SKILL.md").read_text("utf-8"), encoding="utf-8")
        (dest / "SKILL.md").write_text(text, encoding="utf-8")
        (dest / OWNED_MARKER).write_text(f"scholarly-review {__version__}\n", encoding="utf-8")
    return {"host": host, "scope": scope, "dry_run": dry_run, "actions": actions,
            "not_modified": ["global host instructions", "MCP configuration", "other skills"]}
