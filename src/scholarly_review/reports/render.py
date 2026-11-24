"""Deterministic report templates and the export file contract (spec §14, B2).

v0.1 never asks a model to polish the final text: every sentence maps to a Finding or
Assessment ID, so no judgement can be upgraded, invented or merged during writing.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Optional

from ..core.errors import SRError
from ..core.kernel import Kernel
from ..models.common import IMPACT_RANK, new_id, now_utc
from .snapshot import EXTERNAL_AUDIENCES, build_snapshot, category, sort_findings

L = {
    "zh-CN": {
        "_lang": "zh-CN",
        "title_author": "投稿前自查报告", "title_reviewer": "审稿意见草稿", "focused": "重点检查",
        "partial": "部分报告", "paper": "稿件版本", "role": "使用角色", "effort": "投入档位", "date": "生成日期",
        "rubric": "标准来源", "scope": "完成范围", "limits": "主要限制", "control": "预算控制等级",
        "overview": "研究概述", "strengths": "值得保留的贡献与证据充分的优点", "readiness": "投稿准备度",
        "issues": "已确认的问题（按影响排序）", "questions": "待澄清事项与未能确认的疑点",
        "plan": "修改计划", "coverage": "检查覆盖与未完成项", "sources": "关键来源",
        "summary": "Summary", "major": "Major Concerns", "minor": "Minor Comments", "qs": "Questions",
        "refs": "References", "s": "Strengths", "none": "（无）", "evidence": "证据",
        "action": "建议", "why": "影响", "verify_how": "验证方式",
        "must": "提交前应解决", "should": "优先加强", "could": "可选优化",
        "estimate": "工作量为估计值，不保证完成时间。",
        "readiness_text": {"blocked": "blocked：存在已确认且影响核心结论或投稿要求的问题。",
                           "conditional": "conditional：核心检查已完成，但存在待满足条件或待澄清事项。",
                           "no_blocker_found": "no_blocker_found：仅在已完成范围内未发现阻断问题，不代表论文没有错误。",
                           "not_assessed": "not_assessed：核心检查覆盖不足，无法判断。"},
        "appendix_supplied": "已单独提供", "appendix_not_supplied": "未提供",
        "appendix_in_manuscript": "包含在稿件中（{appendix_analyzed}/{appendix_blocks} 块已分析）",
        "pending_human": "未确认：同一会话的复核不能确认重大问题，需要你本人核对原文后用 `sreview verify-human` 记录结论",
        "unconfirmed": "未确认的疑点",
        "no_new_exp": "（当前设定不允许新增实验：可改为澄清、更正或限定表述，但证据缺口仍然存在）",
        "scope_fmt": "已分析正文块 {analyzed_blocks}/{readable_blocks}；附录 {appendix}；已检查核心主张 "
                     "{core_claims_checked}/{core_claims_extracted}（仅指已识别的主张）；拟报告问题已复核 "
                     "{concerns_verified}/{concerns_total}；外部全文 {external_fulltexts} 篇",
        "disclaimer": "本报告为可审阅草稿，由 ScholarlyReview 生成；不代表录用或拒稿决定，也不证明不存在其他错误。",
    },
    "en": {
        "_lang": "en",
        "title_author": "Pre-submission self-check report", "title_reviewer": "Review draft", "focused": "Focused check",
        "partial": "Partial report", "paper": "Paper version", "role": "Role", "effort": "Effort", "date": "Date",
        "rubric": "Standard", "scope": "Completed scope", "limits": "Main limitations", "control": "Budget control",
        "overview": "Overview", "strengths": "Contributions and well-supported strengths", "readiness": "Submission readiness",
        "issues": "Confirmed issues (by impact)", "questions": "Questions and unconfirmed concerns",
        "plan": "Revision plan", "coverage": "Coverage and unfinished items", "sources": "Key sources",
        "summary": "Summary", "major": "Major Concerns", "minor": "Minor Comments", "qs": "Questions",
        "refs": "References", "s": "Strengths", "none": "(none)", "evidence": "Evidence",
        "action": "Suggested action", "why": "Impact", "verify_how": "How to verify",
        "must": "Resolve before submission", "should": "Strengthen with priority", "could": "Optional improvement",
        "estimate": "Effort levels are estimates, not guarantees.",
        "readiness_text": {"blocked": "blocked: a confirmed issue affects a core conclusion or a submission requirement.",
                           "conditional": "conditional: core checks done, but conditions or clarifications remain.",
                           "no_blocker_found": "no_blocker_found: no blocking issue found within the completed scope only.",
                           "not_assessed": "not_assessed: core-check coverage is insufficient to judge."},
        "appendix_supplied": "supplied separately", "appendix_not_supplied": "not supplied",
        "appendix_in_manuscript": "included in the manuscript ({appendix_analyzed}/{appendix_blocks} blocks analysed)",
        "pending_human": "unconfirmed: a same-session verification cannot confirm a major issue; check the "
                         "original yourself and record it with `sreview verify-human`",
        "unconfirmed": "unconfirmed concern",
        "no_new_exp": " (new experiments are not allowed in this run: consider clarification, correction or scoped "
                      "wording; the evidence gap remains)",
        "scope_fmt": "{analyzed_blocks}/{readable_blocks} readable blocks analysed; appendix {appendix}; core claims "
                     "checked {core_claims_checked}/{core_claims_extracted} (of claims identified); concerns verified "
                     "{concerns_verified}/{concerns_total}; external full texts {external_fulltexts}",
        "disclaimer": "Draft generated by ScholarlyReview for the user to review; not an accept/reject decision and "
                      "not a proof that no other errors exist.",
    },
}


# Stored messages stay English and stable; they are localised only when rendered.
ZH_MESSAGES = [
    ("integrity check failed", "部分产物的完整性校验失败，相关发现已排除"),
    ("no domain-specific standard", "未应用领域专用标准（通用配置）"),
    ("generic profile:", "通用配置：未应用领域专用标准"),
    ("interactive mode: model usage", "交互模式：不计量宿主会话的模型用量和费用；同一宿主的复核不能单独确认重大问题"),
    ("interactive mode with isolated verification worker", "交互模式 + 隔离复核 Worker：不计量宿主会话的模型用量，"
     "Worker 用量按实际上报计入；复核来自同一模型家族的新上下文，并非统计独立的审稿人"),
    ("quick runs the focused checks only", "quick 档只做聚焦检查（主张与证据、最接近的相关工作、数值）；"
     "领域专项检查需要 standard 或更高档位"),
    ("venue fit and scores", "期刊适配与评分：未评估（未提供有效的评审表单）"),
    ("two-column reading order", "双栏阅读顺序、表格和公式来自纯文本抽取，重要数值应对照原 PDF 页面核实"),
    ("scores requested but", "要求了评分，但未提供有效评分量表：评分保持未评估"),
    ("code execution is not implemented", "本版本不执行代码：reproduction 档只生成复现计划"),
    ("submission cutoff unknown", "投稿截止日期未知：相关工作的时间先后标为不确定，不判定为漏引"),
    ("paper looks like", "论文所属领域与所选领域配置不一致，建议改用匹配的配置重新规划"),
    ("removed ", None),
]
ZH_ENFORCEMENT = {
    "wall_time": "主动时间", "model_usage": "模型用量", "monetary_cost": "费用", "scope": "控制范围",
    "advisory": "建议值", "runner_enforced": "强制", "unavailable": "不可计量", "measured": "已计量",
    "request_bounded": "按请求封顶", "unknown": "未知", "estimated": "估计", "reservation_bounded": "预留封顶",
    "tool_calls_only": "仅限内核签发的任务", "managed_worker_calls": "受控 Worker 调用", "complete_run": "整个运行",
    "host chat session model usage": "宿主聊天会话的模型用量", "host background agents": "宿主后台 Agent",
    "host tools invoked outside sreview": "未经 sreview 调用的宿主工具",
}
ZH_LABELS = {
    "correct_fact": "更正事实", "add_evidence": "补充证据", "clarify_scope": "澄清适用范围", "improve_writing": "改进表达",
    "hours": "数小时", "hours-days": "数小时至数天", "days-weeks (estimate)": "数天至数周（估计）", "unknown": "未知",
    "blocked": "已阻断", "partial": "部分完成", "skipped": "已跳过", "failed": "失败", "cancelled": "已取消",
    "stale": "已过期", "planned": "未开始", "ready": "未开始", "running": "进行中",
    "budget": "预算不足", "dependency": "依赖未完成", "not_applicable": "不适用", "lease_expired": "租约过期",
    "schema_invalid": "结构校验失败", "semantic_invalid": "语义校验失败", "user_cancel": "用户取消",
}


ZH_CODES = {
    "TEMPORAL_UNCERTAIN": "文献与投稿截止日期的先后无法判断",
    "LITERATURE_CONTENT_INSUFFICIENT": "只读到文献摘要或元数据，不足以判断技术内容",
    "DOWNGRADED_TO_QUESTION": "证据不足以确认，改作提问",
    "INSUFFICIENT_VERIFICATION_FOR_IMPACT": "复核强度不足以确认该影响级别",
    "SOURCE_AFTER_CUTOFF": "文献在投稿截止日期之后公开", "SOURCE_DATE_UNKNOWN": "文献公开日期未知",
    "EVIDENCE_MISSING": "缺少证据", "NO_EVIDENCE": "没有证据", "EVIDENCE_NOT_CURRENT": "证据已失效",
    "EVIDENCE_OTHER_VERSION": "证据来自其他稿件版本", "LOCATOR_INVALID": "原文定位无效",
    "VERSION_MISMATCH": "稿件版本不一致", "AUDIENCE_NOT_ALLOWED": "不适合该读者",
    "STRENGTH_NOT_CONFIRMED": "优点未经确认",
}


def _code(code: str, lang: str) -> str:
    if lang != "zh-CN":
        return code
    if code.startswith("DUPLICATE_OF_"):
        return "与另一条发现重复"
    return ZH_CODES.get(code, code)


def _lbl(value: str, lang: str) -> str:
    return ZH_LABELS.get(value, value) if lang == "zh-CN" else value


def tr(msg: str, lang: str) -> str:
    if lang != "zh-CN":
        return msg
    for prefix, zh in ZH_MESSAGES:
        if zh and (msg.startswith(prefix) or prefix in msg[:80]):
            return zh
    m = re.match(r"(\d+) of (\d+) pages have no extractable text", msg)
    if m:
        return f"{m.group(2)} 页中有 {m.group(1)} 页无法抽取文字，图片中的图表未被读取"
    return msg


def _enf(value: str, lang: str) -> str:
    return ZH_ENFORCEMENT.get(value, value) if lang == "zh-CN" else value


def _loc_str(ev: dict) -> str:
    parts = []
    for r in ev["source_refs"]:
        loc = r["locator"]
        if loc["type"] == "document":
            p = f"p{loc['page_index']}" if loc.get("page_index") else ""
            parts.append(f"{loc.get('block_id')}{(' ' + p) if p else ''} [{loc['match_status']}]")
        elif loc["type"] == "external":
            parts.append(f"{loc['source_id']} [{loc['access_level']}]")
        elif loc["type"] == "artifact":
            parts.append(f"calc:{loc['artifact_sha256'][:12]}")
    return "; ".join(parts)


def _ev_lines(snap: dict, f: dict, audience: str, max_quotes: int = 2) -> list[str]:
    out = []
    for eid in f["evidence_ids"]:
        ev = snap["evidence"].get(eid)
        if not ev or audience not in ev["disclosure"]["allowed_audiences"]:
            continue
        quote = next((r["quote"] for r in ev["source_refs"] if r.get("quote")), None)
        line = f"  - [{ev['kind']}] {ev['content_summary']} ({_loc_str(ev)})"
        if quote and max_quotes > 0:
            line += f'\n    > "{quote[:300]}"'
            max_quotes -= 1
        out.append(line)
    return out


def _header(snap: dict, t: dict) -> list[str]:
    cfg = snap["config"]
    cs = snap["coverage_summary"]
    title = t["title_author"] if cfg["mode"] == "author" else t["title_reviewer"]
    if cfg["effort"] == "quick" or not cs["core_coverage_complete"]:
        title = f"{t['focused']} · {title}"
    if snap["is_partial"]:
        title += f" ({t['partial']})"
    enf = snap["enforcement"]
    rub = cfg["rubric"]
    lang = cfg["outputs"]["language"]
    zh = lang == "zh-CN"
    control = ("；" if zh else "; ").join(f"{_enf(k, lang)}={_enf(enf[k], lang)}"
                        for k in ("wall_time", "model_usage", "monetary_cost", "scope"))
    excluded = ("、" if zh else ", ").join(_enf(x, lang) for x in enf["excluded_scope"])
    limits = list(dict.fromkeys(tr(x, lang) for x in snap["limitations"]))[:6]
    return [
        f"# {title}", "",
        f"- {t['paper']}: `{snap['paper_version_id']}` · run `{snap['run_id']}` · snapshot `{snap['snapshot_id']}`",
        f"- {t['role']}: {cfg['mode']} · {t['effort']}: {cfg['effort']} · profile: {cfg['scope']['domain_profile']}",
        f"- {t['date']}: {snap['generated_at']}",
        f"- {t['rubric']}: {rub.get('source_file') or rub['profile']} "
        + (f"（会议/期刊：{rub.get('venue') or '未指定'}；投稿截止：{rub.get('submission_cutoff') or '未知'}）" if zh else
           f"(venue: {rub.get('venue') or 'n/a'}; cutoff: {rub.get('submission_cutoff') or 'unknown'})"),
        f"- {t['scope']}: " + t["scope_fmt"].format(**{**cs, "appendix": t["appendix_" + cs["appendix"].replace(" ", "_")].format(
            appendix_analyzed=cs.get("appendix_in_manuscript_analyzed", 0), appendix_blocks=cs.get("appendix_in_manuscript_blocks", 0))}),
        f"- {t['control']}: {control}" + (f"；未计入：{excluded}" if zh else f"; excluded: {excluded}"),
        f"- {t['limits']}: " + (("；" if zh else "; ").join(limits) or t["none"]),
        "",
    ]


def _overview(snap: dict, t: dict) -> list[str]:
    s = snap.get("paper_summary")
    if not s:
        return [f"## {t['overview']}", "", t["none"], ""]
    lines = [f"## {t['overview']}", "", f"{s['research_problem']} {s['method']}", ""]
    if s.get("contributions"):
        lines += [f"- {c}" for c in s["contributions"]] + [""]
    return lines


def _finding_block(snap: dict, f: dict, t: dict, audience: str, with_ids: bool = True) -> list[str]:
    head = f"- **[{f['impact'] if f['impact'] != 'not_assessed' else f['potential_impact'] + '?'}]** {f['proposition']}"
    if with_ids:
        head += f"  `{f['finding_id'][-8:]}`"
    lines = [head]
    if f.get("impact_rationale"):
        lines.append(f"  - {t['why']}: {f['impact_rationale']}")
    lines += _ev_lines(snap, f, audience)
    if f.get("recommended_action"):
        lines.append(f"  - {t['action']}: {f['recommended_action']}")
    return lines


def render_markdown(snap: dict) -> str:
    cfg = snap["config"]
    t = L[cfg["outputs"]["language"]]
    aud = snap["audience"]
    internal = aud not in EXTERNAL_AUDIENCES
    adm = [f for f in snap["findings"] if f["report_eligibility"] and f["report_eligibility"][0]["admitted"]]
    strengths = sort_findings([f for f in adm if category(f) == "strength"])
    concerns = sort_findings([f for f in adm if category(f) == "confirmed_concern"])
    questions = sort_findings([f for f in adm if category(f) == "question"])
    lines = _header(snap, t)
    if cfg["mode"] == "author":
        lines += _overview(snap, t)
        r = snap["readiness"]
        lines += [f"## {t['readiness']}", "", t["readiness_text"][r["value"]], ""]
        lines += [f"## {t['strengths']}", ""] + sum((_finding_block(snap, f, t, aud) for f in strengths), []) \
            + ([t["none"]] if not strengths else []) + [""]
        lines += [f"## {t['issues']}", ""] + sum((_finding_block(snap, f, t, aud) for f in concerns), []) \
            + ([t["none"]] if not concerns else []) + [""]
        lines += [f"## {t['questions']}", ""] + sum((_question_block(snap, f, t, aud) for f in questions), []) \
            + ([t["none"]] if not questions else []) + [""]
        lines += [f"## {t['plan']}", "", t["estimate"], ""] + _plan_md(action_plan(snap), t)
    else:
        lines += [f"## {t['summary']}", ""] + (_overview(snap, t)[2:] if snap.get("paper_summary") else [t["none"], ""])
        lines += [f"## {t['s']}", ""] + sum((_finding_block(snap, f, t, aud, internal) for f in strengths), []) \
            + ([t["none"]] if not strengths else []) + [""]
        major = [f for f in concerns if IMPACT_RANK[f["impact"]] >= IMPACT_RANK["major"]]
        minor = [f for f in concerns if IMPACT_RANK[f["impact"]] < IMPACT_RANK["major"]]
        lines += [f"## {t['major']}", ""] + sum((_finding_block(snap, f, t, aud, internal) for f in major), []) \
            + ([t["none"]] if not major else []) + [""]
        lines += [f"## {t['minor']}", ""] + sum((_finding_block(snap, f, t, aud, internal) for f in minor), []) \
            + ([t["none"]] if not minor else []) + [""]
        lines += [f"## {t['qs']}", ""] + sum((_question_block(snap, f, t, aud) for f in questions), []) \
            + ([t["none"]] if not questions else []) + [""]
        refs = _references(snap, adm, cfg["outputs"]["max_external_references"])
        if refs:
            lines += [f"## {t['refs']}", ""] + refs + [""]
    if internal:
        lang = cfg["outputs"]["language"]
        lines += [f"## {t['coverage']}", ""] + [
            f"- {i['task']}: {_lbl(i['state'], lang)}" + (f" ({_lbl(i['reason'], lang)})" if i["reason"] else "")
            for i in snap["coverage_summary"]["incomplete_items"]] \
            + ([t["none"]] if not snap["coverage_summary"]["incomplete"] else []) + [""]
    lines += ["---", t["disclaimer"]]
    return "\n".join(lines) + "\n"


def _question_block(snap: dict, f: dict, t: dict, aud: str) -> list[str]:
    ext = f.get("extensions") or {}
    lines = [f"- {f['proposition']}"]
    if f["kind"] == "concern":
        if ext.get("pending"):
            lines.append(f"  - {t['pending_human']}")
        else:
            codes = list(dict.fromkeys(f["report_eligibility"][0]["reason_codes"]))
            lines.append(f"  - {t['unconfirmed']}: " + "；".join(_code(c, t["_lang"]) for c in codes)
                         if t["_lang"] == "zh-CN" else f"  - {t['unconfirmed']} ({', '.join(codes)})")
        if ext.get("neutral_question"):
            lines.append(f"  - ? {ext['neutral_question']}")
    lines += _ev_lines(snap, f, aud, max_quotes=1)
    return lines


def _references(snap: dict, adm: list[dict], limit: int) -> list[str]:
    out, seen = [], set()
    for f in adm:
        for eid in f["evidence_ids"]:
            ev = snap["evidence"].get(eid)
            if not ev or ev["kind"] != "literature":
                continue
            for r in ev["source_refs"]:
                sid = r["locator"].get("source_id")
                if sid and sid not in seen and sid in snap["sources"]:
                    s = snap["sources"][sid]
                    seen.add(sid)
                    authors = ", ".join((s.get("authors") or [])[:3])
                    out.append(f"- {authors}. {s.get('title')}. {s.get('venue') or ''} "
                               f"({(s.get('first_public_date') or '')[:4]}). {(s.get('links') or [''])[0]}")
    return out[:limit]


def action_plan(snap: dict) -> list[dict]:
    plan = []
    for f in snap["findings"]:
        cat = category(f)
        if cat not in ("confirmed_concern", "question"):
            continue
        ext = f.get("extensions") or {}
        if cat == "confirmed_concern" and IMPACT_RANK[f["impact"]] >= IMPACT_RANK["major"]:
            tier = "must"
        elif cat == "question" and IMPACT_RANK.get(f["potential_impact"], 0) >= IMPACT_RANK["major"]:
            tier = "should"
        elif cat == "confirmed_concern" and f["impact"] == "minor":
            tier = "could" if ext.get("action_type") == "improve_writing" else "should"
        else:
            tier = "could"
        plan.append({
            "tier": tier, "finding_id": f["finding_id"], "basis": f["proposition"], "claims": f["claim_ids"],
            "change_type": ext.get("action_type") or ("clarify_scope" if cat == "question" else "add_evidence"),
            "suggested_change": f.get("recommended_action"),
            "expected_benefit": f.get("impact_rationale"),
            "effort_estimate": {"correct_fact": "hours", "improve_writing": "hours", "clarify_scope": "hours-days",
                                "add_evidence": "days-weeks (estimate)"}.get(ext.get("action_type") or "", "unknown"),
            "verification": "re-run `sreview` on the revised version (new PaperVersion) and compare",
            "blocked_by_no_new_experiments": (ext.get("action_type") == "add_evidence"
                                              and not snap["config"]["scope"]["allow_new_experiments"]),
        })
    order = {"must": 0, "should": 1, "could": 2}
    return sorted(plan, key=lambda p: order[p["tier"]])


def _plan_md(plan: list[dict], t: dict) -> list[str]:
    lines = []
    for tier in ("must", "should", "could"):
        items = [p for p in plan if p["tier"] == tier]
        lines += [f"### {t[tier]}", ""]
        if not items:
            lines += [t["none"], ""]
            continue
        for p in items:
            note = t["no_new_exp"] if p["blocked_by_no_new_experiments"] else ""
            lang = t["_lang"]
            effort = f"（工作量：{_lbl(p['effort_estimate'], lang)}）" if lang == "zh-CN" else f" (effort: {p['effort_estimate']})"
            lines.append(f"- [{_lbl(p['change_type'], lang)}] {p['suggested_change'] or p['basis']}"
                         f"{effort}{note}  `{p['finding_id'][-8:]}`")
        lines.append("")
    return lines


def render_reviewer_private(snap: dict) -> str:
    lines = [f"# Reviewer-private analysis · run {snap['run_id']}", "",
             "Internal record: includes unconfirmed, refuted and excluded findings with reasons. Do not send to authors.", ""]
    for f in sort_findings(snap["findings"]):
        e = f["report_eligibility"][0] if f["report_eligibility"] else {}
        lines.append(f"- [{f['kind']}/{f['disposition']}/{f['impact']}] {f['proposition']}  `{f['finding_id']}`")
        lines.append(f"  - category: {e.get('category')} · reasons: {', '.join(e.get('reason_codes', [])) or '-'}")
        lines.append(f"  - basis: {f['confidence_basis']}")
        for vid in f["verification_ids"]:
            v = snap["verifications"].get(vid)
            if v:
                lines.append(f"  - verification {v['result']} ({v['context_independence']}, blinded={v['blinded']}): "
                             f"{v['rationale'][:300]}")
    return "\n".join(lines) + "\n"


def md_to_txt(md: str) -> str:
    txt = re.sub(r"^#+\s*", "", md, flags=re.M)
    txt = txt.replace("**", "").replace("`", "")
    return txt


def md_to_html(md: str, title: str) -> str:
    body = []
    for line in md.splitlines():
        esc = html.escape(line)
        if line.startswith("# "):
            body.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line.startswith("## "):
            body.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("### "):
            body.append(f"<h3>{html.escape(line[4:])}</h3>")
        elif line.strip().startswith(">"):
            body.append(f"<blockquote>{html.escape(line.strip()[1:])}</blockquote>")
        elif line.strip():
            body.append(f"<p>{esc}</p>")
    # local, escaped, no scripts and no remote resources (spec §12.4)
    return ("<!doctype html><html><head><meta charset='utf-8'><title>" + html.escape(title) + "</title>"
            "<style>body{font-family:system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem;line-height:1.5}"
            "blockquote{color:#555;border-left:3px solid #ccc;margin:0 0 .5rem 1rem;padding-left:.75rem}"
            "p{margin:.25rem 0}</style></head><body>" + "\n".join(body) + "</body></html>")


def export(k: Kernel, run_id: str, audience: Optional[str] = None, formats: Optional[list[str]] = None) -> dict:
    snap = build_snapshot(k, run_id, audience)
    cfg = snap["config"]
    aud = snap["audience"]
    if cfg["mode"] == "author" and aud == "reviewer_private" or cfg["mode"] == "reviewer" and aud == "author_private":
        raise SRError("invalid_input", f"audience {aud} does not belong to mode {cfg['mode']}")
    formats = formats or cfg["outputs"]["formats"]
    export_id = new_id("exp")
    out_dir = k.ws.exports_dir / run_id / export_id
    out_dir.mkdir(parents=True)
    warnings = []
    md = render_markdown(snap)
    files: dict[str, str] = {}

    def write(name: str, text: str) -> None:
        p = out_dir / name
        p.write_text(text, encoding="utf-8")
        files[name] = str(p)

    write("report.md", md)
    write("report.txt", md_to_txt(md))
    if "html" in formats:
        write("report.html", md_to_html(md, f"ScholarlyReview {run_id}"))
    if "docx" in formats:
        warnings.append("docx output is optional and not available in v0.1")
    internal = aud not in EXTERNAL_AUDIENCES
    if internal:
        findings_out = snap["findings"]
        ev_ids = set(snap["evidence"])
    else:
        findings_out = [f for f in snap["findings"] if f["report_eligibility"][0]["admitted"]]
        ev_ids = {e for f in findings_out for e in f["evidence_ids"]}
        findings_out = [{k_: f[k_] for k_ in ("finding_id", "kind", "proposition", "impact", "claim_ids",
                                              "evidence_ids", "recommended_action", "report_eligibility")}
                        for f in findings_out]
    write("findings.json", json.dumps(findings_out, ensure_ascii=False, indent=2))
    ev_lines = []
    for eid in sorted(ev_ids):
        ev = snap["evidence"][eid]
        if aud in ev["disclosure"]["allowed_audiences"]:
            ev_lines.append(json.dumps(ev, ensure_ascii=False))
    write("evidence.jsonl", "\n".join(ev_lines) + ("\n" if ev_lines else ""))
    write("coverage.json", json.dumps({"summary": snap["coverage_summary"], **snap["coverage"]}, ensure_ascii=False, indent=2))
    if internal:
        write("budget.json", json.dumps(snap["budget"] | {"enforcement": snap["enforcement"]}, ensure_ascii=False, indent=2))
        if cfg["mode"] == "author":
            plan = action_plan(snap)
            write("action_plan.json", json.dumps(plan, ensure_ascii=False, indent=2))
            write("action_plan.md", "\n".join(_plan_md(plan, L[cfg["outputs"]["language"]])))
        else:
            write("reviewer_private.md", render_reviewer_private(snap))
    manifest = {"export_id": export_id, "run_id": run_id, "snapshot_id": snap["snapshot_id"], "audience": aud,
                "paper_version_id": snap["paper_version_id"], "created_at": now_utc(), "partial": snap["is_partial"],
                "readiness": snap["readiness"], "files": sorted(files), "warnings": warnings,
                "admitted_finding_ids": [f["finding_id"] for f in snap["findings"]
                                         if f["report_eligibility"] and f["report_eligibility"][0]["admitted"]]}
    write("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))  # written last: completion marker
    with k.ws.tx() as c:
        c.execute("INSERT INTO exports VALUES(?,?,?,?,?)", (export_id, run_id, aud, json.dumps(manifest), now_utc()))
    return {"export_id": export_id, "directory": str(out_dir), "files": files, "partial": snap["is_partial"],
            "readiness": snap["readiness"], "warnings": warnings}
