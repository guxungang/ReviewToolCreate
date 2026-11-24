## What to do

You do **not** compute anything. You extract numbers and the claimed values exactly as printed; the kernel
recomputes them with a deterministic program and decides consistency.

Look for numbers that can be recomputed: confusion matrices vs reported metrics, totals and averages in
tables, "improves by X points / X%" statements, values repeated in abstract, text and tables, and claims of
significance. Prefer numbers tied to **core claims**.

Also check every stated multiple or factor against the two numbers it relates - in the method and setup
sections as well as the results: "N times fewer steps", "a factor of 5, to 0.0075", "0.045 (30 times that of
X)", "half the cost". Use `ratio` (or `product` for "increased by a factor of k, to v"): e.g. inputs
`numerator: "0.045"`, `denominator: "0.0015"`, claimed `ratio: "30"`. Hyperparameters such as learning rates,
batch sizes and step counts are cheap to check this way and easy to get wrong.

For each item give `op`, `inputs`, `claimed`, `claim_ids` (from context), verbatim `source_refs` for every
place the inputs and claimed values come from, and a `description`. Inputs must literally appear in the
quotes; if numbers come from a figure image you cannot quote, put the item in `not_extractable`.

| op | inputs | claimed keys |
| --- | --- | --- |
| `confusion_metrics` | `matrix` (counts), `orientation`: `rows_true` or `rows_pred` (**required**), optional `labels` | `accuracy`, `macro_f1`, `micro_f1`, `weighted_f1`, `macro_precision`, `macro_recall`, `kappa`, `f1_<label>`, `precision_<label>`, `recall_<label>` |
| `normalized_confusion` | row-normalised `matrix`, `orientation`, optional `supports` (class counts) | same as above; without supports only `macro_recall` is determinable |
| `change` | `old`, `new`, `unit`: `percent` / `ratio` / `raw` | `pp_change`, `relative_change`, `absolute_change` |
| `mean` / `sum` / `weighted_mean` | `values` (+ `weights`) | `mean` / `sum` / `weighted_mean` |
| `rounding_compatible` | `displays`: list of displayed strings, e.g. `["84.0%", "0.8402"]`; `unit`: `ratio` (default) or `raw` (BLEU, F1 points, counts) | `compatible`: `"true"` |
| `product` | `values` taken from the paper (e.g. hours, GPUs, FLOPS) + `constants` for unit conversions not in the paper (e.g. `[3600]`) | `product` |
| `ratio` | `numerator`, `denominator` | `ratio` |
| `mean_difference` | `a`, `b`, `unit`, optional `per_run_a`, `per_run_b` | `pp_change` |

Claimed values may be bounds when the paper states one: `">2.0"`, `"<0.25"`, `">=90"` ("by over 2 BLEU",
"less than 1/4 the cost"). Give numbers in scientific notation exactly as printed (`"3.3 · 10^18"`, or `"31.0 · 106"` when
the extracted text has lost the superscript); the kernel reads these forms and keeps the mantissa's precision.

Rules: if the paper says only "F1" for a multi-class task, do not guess the averaging - use the key the paper
defines, or list it in `not_extractable` with the reason. Copy claimed values as displayed (`"80%"`,
`"0.8402"`, `"+4.0"`). Give inputs as strings exactly as printed (`"0.9580"`, not `0.958`) so their
precision is kept. Do not check a reported macro-F1 as the `mean` of per-class F1 values: papers often
define macro-F1 as the F1 of macro precision and macro recall; use `confusion_metrics` (which accepts either
definition) or skip it. If a ratio metric is shown without % and is > 1 (e.g. `76.2`), add
`"claimed_scale": "percent"` to `inputs` only if the paper states the unit.

## Output

```json
{"output": {
  "checks": [{"key": "n1", "op": "confusion_metrics",
              "inputs": {"matrix": [[90, 10], [20, 30]], "orientation": "rows_true"},
              "claimed": {"accuracy": "80%", "macro_f1": "80%"}, "claim_ids": ["<claim_id>"],
              "source_refs": [{"block_id": "b00051", "quote": "Positive 90 10"}, {"block_id": "b00051", "quote": "Negative 20 30"},
                              {"block_id": "b00049", "quote": "accuracy and macro-F1 of 80%"}],
              "description": "Table 3 confusion matrix vs. reported accuracy/macro-F1"}],
  "analyzed_block_ids": ["b00049", "b00051"],
  "not_extractable": ["Fig. 4 bar values are only in the image"]
}, "usage": {"basis": "unknown"}}
```
