## What to do: reverse verification

For each item in `verification_items` you receive a **neutral question**, the claims involved and the evidence
locations - not the candidate conclusion. Answer the question from the original material:

1. Look for **counter-evidence and alternative explanations first**: search the full text, appendix and
   supplements (a "missing ablation" is often in the appendix; a "missing baseline" may be discussed under a
   different name; a numeric mismatch may come from a different averaging or split).
2. For numeric items (`has_calculation_evidence: true`), re-read the original source values and confirm the
   inputs used by the recomputation are the right ones (correct table, row/column orientation, units). Set
   `inputs_rechecked: true` only if you actually did this.
3. Decide `result`:
   - `validated` - the problem holds; give `confirmed_impact` (critical/major/minor/informational).
   - `invalidated` - the material shows the problem does not hold (cite the counter-evidence).
   - `inconclusive` - the material is insufficient; say what would decide it.
4. Every new quote goes into `evidence` with verbatim `source_refs`; reference it from
   `supporting_evidence_keys` or `counterevidence_keys`.
5. If you found a well-evidenced positive point while doing this (e.g. the complete ablation in the appendix),
   you may report it in the next check task; do not stretch it here.

### Declare your context honestly

- `same_context`: you are the same session/agent that produced the candidate finding.
- `inherited`: a new context that still shares memory/history (e.g. a sub-agent of this session).
- `fresh`: an isolated worker with only these inputs. In interactive mode the kernel cannot prove isolation
  and records `fresh` as `inherited`.
- `human` is only for `sreview verify-human` by the user.
`blinded` is `true` only if you did not see the candidate conclusion (e.g. a sub-agent given just this bundle).

A same-context or inherited verification can confirm minor issues and numeric issues with re-checked inputs,
but **cannot** confirm a major/critical non-numeric concern; those stay open questions until a human verifies
them. This is expected - do not overstate independence to get a concern confirmed.

## Output

```json
{"output": {
  "context_independence": "same_context", "blinded": false,
  "evidence": [{"key": "v1", "kind": "supplement", "content_summary": "Appendix C has the ablation",
                "source_refs": [{"block_id": "s1-b00012", "quote": "Table 7: ablation"}], "method": "searched appendix"}],
  "verifications": [{"finding_id": "<finding_id>", "method": "re-read Sec. 5 and Appendix C", "result": "invalidated",
                     "rationale": "...", "counterevidence_keys": ["v1"], "supporting_evidence_keys": [],
                     "confirmed_impact": null, "inputs_rechecked": false, "limitations": []}]
}, "usage": {"basis": "unknown"}}
```
