"""Deterministic numeric verification (spec §8.6, golden cases A01-A04).

Rules: confusion-matrix orientation must be explicit; macro/micro/weighted are named
separately; row-normalised matrices without supports never yield accuracy or weighted F1;
displayed values are compared by rounding interval; percentage-point and relative change are
distinct; relative change from a zero baseline is undefined; no p-values without data.
"""
from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

CODE_VERSION = "numeric-0.1.4"
INEQ_RE = re.compile(r"^(>=|<=|≥|≤|>|<)\s*(.+)$")
EPS = 1e-9

# unit of every output metric: ratio (0..1), pp (percentage points), raw
RATIO_METRICS = {"accuracy", "macro_precision", "macro_recall", "macro_f1", "micro_f1", "weighted_f1",
                 "relative_change", "kappa"}


class NumericInputError(ValueError):
    pass


# ---------------------------------------------------------------- scientific notation
SUPERSCRIPT = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻", "0123456789-")
# "31.0 · 10^6", "3.3×10¹⁸", and the PDF-flattened "31.0 · 106" (the exponent loses its superscript).
# Only a middle dot or a multiplication sign counts: "2 x 106" stays a literal string.
SCI_RE = re.compile(r"^\s*([-+−]?\d+(?:\.\d+)?)\s*[·×∙⋅]\s*10\s*(?:\^|\*\*)?\s*\(?([-−]?\d{1,2})\)?\s*(%?)\s*$")


def sci_to_decimal_str(s: str) -> Optional[str]:
    """'31.0 · 106' -> '31.0E6' (keeps the mantissa's precision for the rounding interval); None if not sci."""
    m = SCI_RE.match(s.translate(SUPERSCRIPT))
    if not m:
        return None
    return f"{m.group(1).replace('−', '-')}E{m.group(2).replace('−', '-')}{m.group(3)}"


def normalize_inputs(obj: Any) -> Any:
    if isinstance(obj, str):
        return sci_to_decimal_str(obj) or obj
    if isinstance(obj, list):
        return [normalize_inputs(v) for v in obj]
    if isinstance(obj, dict):
        return {k: normalize_inputs(v) for k, v in obj.items()}
    return obj


# ---------------------------------------------------------------- displays
def parse_display(display: str, unit: str, scale_hint: Optional[str] = None) -> tuple[float, float, str]:
    """Return the rounding interval [lo, hi] in the metric's unit, plus a description."""
    s = display.strip().replace("−", "-").replace(",", "")
    s = sci_to_decimal_str(s) or s
    s = re.sub(r"\s*(pp|p\.p\.|points?)$", "", s, flags=re.I)
    pct = s.endswith("%")
    if pct:
        s = s[:-1].strip()
    if s.startswith("+"):
        s = s[1:]
    try:
        d = Decimal(s)
    except InvalidOperation as e:
        raise NumericInputError(f"cannot parse displayed value '{display}'") from e
    exp = d.as_tuple().exponent  # "84.0" -> -1, "80" -> 0, "3.3e18" -> 17
    if not isinstance(exp, int):
        raise NumericInputError(f"'{display}' is not a finite number")
    half = Decimal(5) * Decimal(10) ** (exp - 1)
    lo, hi = d - half, d + half
    if unit == "ratio":
        if pct or scale_hint == "percent":
            lo, hi = lo / 100, hi / 100
        elif abs(d) <= 1:
            pass
        else:
            raise NumericInputError(f"'{display}' is ambiguous: a ratio metric shown without % and > 1")
    # pp and raw: the number is already in the metric's unit ("4%" for a pp change means 4 points)
    return float(lo), float(hi), f"{display} -> [{lo}, {hi}] {unit}"


def intervals_overlap(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return a[0] <= b[1] + EPS and b[0] <= a[1] + EPS


# ---------------------------------------------------------------- metrics
def _matrix(inputs: dict) -> list[list[float]]:
    m = inputs.get("matrix")
    if not isinstance(m, list) or not m or not all(isinstance(r, list) for r in m):
        raise NumericInputError("matrix must be a non-empty list of rows")
    k = len(m)
    if any(len(r) != k for r in m):
        raise NumericInputError("confusion matrix must be square")
    orientation = inputs.get("orientation")
    if orientation not in ("rows_true", "rows_pred"):
        raise NumericInputError("orientation must be stated explicitly: 'rows_true' or 'rows_pred'")
    mm = [[float(x) for x in r] for r in m]
    if orientation == "rows_pred":
        mm = [list(col) for col in zip(*mm)]
    return mm


def confusion_metrics(inputs: dict) -> tuple[dict, list[str]]:
    m = _matrix(inputs)
    k = len(m)
    labels = inputs.get("labels") or [str(i) for i in range(k)]
    n = sum(sum(r) for r in m)
    if n <= 0:
        raise NumericInputError("matrix total is zero")
    support = [sum(m[i]) for i in range(k)]
    pred = [sum(m[r][i] for r in range(k)) for i in range(k)]
    tp = [m[i][i] for i in range(k)]
    notes: list[str] = ["single-label multiclass assumed; micro-F1 equals accuracy under this assumption",
                        "zero-denominator policy: undefined per-class values make the affected average undefined"]
    prec = [tp[i] / pred[i] if pred[i] else None for i in range(k)]
    rec = [tp[i] / support[i] if support[i] else None for i in range(k)]
    f1 = [2 * tp[i] / (support[i] + pred[i]) if (support[i] + pred[i]) else None for i in range(k)]

    def mean_or_none(xs):
        return None if any(x is None for x in xs) else sum(xs) / len(xs)

    out = {
        "accuracy": sum(tp) / n,
        "macro_precision": mean_or_none(prec),
        "macro_recall": mean_or_none(rec),
        "macro_f1": mean_or_none(f1),
        # many papers report macro-F1 as the F1 of macro precision and macro recall instead
        "macro_f1_of_macro_pr": None,
        "micro_f1": sum(tp) / n,
        "weighted_f1": None if any(f is None and s for f, s in zip(f1, support))
        else sum((f or 0) * s for f, s in zip(f1, support)) / n,
        "kappa": None,
        "support": dict(zip(labels, support)),
    }
    mp, mr = out["macro_precision"], out["macro_recall"]
    if mp is not None and mr is not None and mp + mr > 0:
        out["macro_f1_of_macro_pr"] = 2 * mp * mr / (mp + mr)
    pe = sum(support[i] * pred[i] for i in range(k)) / (n * n)
    out["kappa"] = None if abs(1 - pe) < EPS else (out["accuracy"] - pe) / (1 - pe)
    for i, lab in enumerate(labels):
        out[f"precision_{lab}"] = prec[i]
        out[f"recall_{lab}"] = rec[i]
        out[f"f1_{lab}"] = f1[i]
    return out, notes


def normalized_confusion(inputs: dict) -> tuple[dict, list[str]]:
    m = _matrix(inputs)
    k = len(m)
    supports = inputs.get("supports")
    if supports:
        if len(supports) != k:
            raise NumericInputError("supports length must equal number of classes")
        counts = [[m[i][j] * float(supports[i]) for j in range(k)] for i in range(k)]
        out, notes = confusion_metrics({"matrix": counts, "orientation": "rows_true", "labels": inputs.get("labels")})
        notes.append("counts reconstructed from row-normalised matrix and stated class supports")
        return out, notes
    for i, r in enumerate(m):
        if abs(sum(r) - 1) > 0.02:
            raise NumericInputError(f"row {i} does not sum to 1; is the matrix really row-normalised?")
    out = {
        "macro_recall": sum(m[i][i] for i in range(k)) / k,
        "accuracy": None, "weighted_f1": None, "macro_f1": None, "macro_precision": None, "micro_f1": None,
    }
    notes = ["class supports unknown: accuracy, precision and any F1 are not determinable; "
             "equal class sizes are NOT assumed"]
    return out, notes


def change(inputs: dict) -> tuple[dict, list[str]]:
    old, new = float(inputs["old"]), float(inputs["new"])
    unit = inputs.get("unit")
    if unit not in ("percent", "ratio", "raw"):
        raise NumericInputError("unit must be 'percent', 'ratio' or 'raw'")
    notes = []
    if unit == "percent":
        diff = new - old
        out = {"pp_change": diff}
    elif unit == "ratio":
        diff = new - old
        out = {"pp_change": diff * 100}
    else:
        diff = new - old
        out = {"absolute_change": diff}
    if old == 0:
        out["relative_change"] = None
        notes.append("relative change is undefined for a zero baseline")
    else:
        out["relative_change"] = diff / abs(old)
    return out, notes


def mean_(inputs: dict) -> tuple[dict, list[str]]:
    vals = [float(v) for v in inputs["values"]]
    if not vals:
        raise NumericInputError("values is empty")
    return {"mean": sum(vals) / len(vals)}, []


def sum_(inputs: dict) -> tuple[dict, list[str]]:
    return {"sum": sum(float(v) for v in inputs["values"])}, []


def weighted_mean(inputs: dict) -> tuple[dict, list[str]]:
    vals = [float(v) for v in inputs["values"]]
    w = [float(x) for x in inputs["weights"]]
    if len(vals) != len(w) or sum(w) == 0:
        raise NumericInputError("values/weights mismatch or zero total weight")
    return {"weighted_mean": sum(v * x for v, x in zip(vals, w)) / sum(w)}, []


def rounding_compatible(inputs: dict) -> tuple[dict, list[str]]:
    displays = inputs.get("displays") or []
    if len(displays) < 2:
        raise NumericInputError("need at least two displayed values")
    hint = inputs.get("scale_hint")
    unit = inputs.get("unit", "ratio")  # "raw" for BLEU, F1 points, counts ...
    if unit not in ("ratio", "raw"):
        raise NumericInputError("unit must be 'ratio' or 'raw'")
    ivs = [parse_display(d, unit, hint)[:2] for d in displays]
    ok = all(intervals_overlap(ivs[0], iv) for iv in ivs[1:]) and all(
        intervals_overlap(a, b) for i, a in enumerate(ivs) for b in ivs[i + 1:])
    return {"compatible": ok, "intervals": ivs}, ["compared as rounding intervals, not digit-by-digit"]


def mean_difference(inputs: dict) -> tuple[dict, list[str]]:
    a, b = float(inputs["a"]), float(inputs["b"])
    unit = inputs.get("unit", "percent")
    diff = a - b
    out: dict[str, Any] = {"pp_change" if unit == "percent" else "absolute_change": diff}
    notes = []
    per_run_a, per_run_b = inputs.get("per_run_a"), inputs.get("per_run_b")
    if per_run_a and per_run_b and len(per_run_a) >= 2 and len(per_run_b) >= 2:
        def sd(xs):
            mu = sum(xs) / len(xs)
            return math.sqrt(sum((x - mu) ** 2 for x in xs) / (len(xs) - 1))
        out["sd_a"], out["sd_b"] = sd([float(x) for x in per_run_a]), sd([float(x) for x in per_run_b])
        out["significance"] = "not_computed"
        notes.append("descriptive spread only; a significance test needs a declared protocol (pairing, test)")
    else:
        out["significance"] = "unknown"
        notes.append("no variance or per-run results: significance is unknown; no p-value or CI is generated, "
                     "and the difference is NOT judged non-significant")
    return out, notes


def product(inputs: dict) -> tuple[dict, list[str]]:
    vals = [float(v) for v in inputs["values"]]
    consts = [float(v) for v in inputs.get("constants", [])]
    if not vals:
        raise NumericInputError("values is empty")
    p = 1.0
    for v in vals + consts:
        p *= v
    notes = ["plain product of the listed factors; units are the caller's responsibility"]
    if consts:
        notes.append(f"unit-conversion constants not taken from the paper: {consts}")
    return {"product": p}, notes


def ratio(inputs: dict) -> tuple[dict, list[str]]:
    num, den = float(inputs["numerator"]), float(inputs["denominator"])
    if den == 0:
        return {"ratio": None}, ["ratio undefined for a zero denominator"]
    return {"ratio": num / den}, []


def _half_unit(v: Any) -> float:
    """Half of the last displayed digit of an input ("91.47" -> 0.005, 12 -> 0.5)."""
    try:
        d = Decimal(str(v).strip().replace("−", "-").replace(",", "").rstrip("%"))
    except InvalidOperation:
        return 0.0
    exp = d.as_tuple().exponent
    return float(Decimal(5) * Decimal(10) ** (exp - 1)) if isinstance(exp, int) else 0.0


# Ops whose inputs are displayed (rounded) numbers; the output range under input rounding is
# obtained by evaluating every corner of the input box (scalars) or the all-low / all-high
# vectors (monotone list ops on non-negative values).
SCALAR_INPUTS = {"change": ("old", "new"), "ratio": ("numerator", "denominator"), "mean_difference": ("a", "b")}
LIST_INPUTS = {"mean": "values", "sum": "values", "product": "values"}


def output_ranges(op: str, inputs: dict) -> dict[str, tuple[float, float]]:
    variants: list[dict] = []
    if op in SCALAR_INPUTS:
        keys = SCALAR_INPUTS[op]
        if any(k not in inputs for k in keys):
            return {}
        from itertools import product as _prod
        for signs in _prod((-1, 1), repeat=len(keys)):
            v = dict(inputs)
            for k, sg in zip(keys, signs):
                v[k] = float(inputs[k]) + sg * _half_unit(inputs[k])
            variants.append(v)
    elif op in LIST_INPUTS:
        vals = inputs.get(LIST_INPUTS[op]) or []
        if op == "product" and any(float(x) < 0 for x in vals):
            return {}  # mean and sum are monotone in every element; a product with negatives is not
        for sg in (-1, 1):
            v = dict(inputs)
            v[LIST_INPUTS[op]] = [float(x) + sg * _half_unit(x) for x in vals]
            variants.append(v)
    else:
        return {}
    ranges: dict[str, list[float]] = {}
    for v in variants:
        try:
            out, _ = OPS[op](v)
        except (NumericInputError, KeyError, TypeError, ValueError, ZeroDivisionError):
            return {}
        for k, x in out.items():
            if isinstance(x, (int, float)) and not isinstance(x, bool):
                ranges.setdefault(k, []).append(float(x))
    return {k: (min(xs), max(xs)) for k, xs in ranges.items()}


OPS = {
    "product": product,
    "ratio": ratio,
    "confusion_metrics": confusion_metrics,
    "normalized_confusion": normalized_confusion,
    "change": change,
    "mean": mean_,
    "sum": sum_,
    "weighted_mean": weighted_mean,
    "rounding_compatible": rounding_compatible,
    "mean_difference": mean_difference,
}


# A claimed metric may legitimately follow another common definition; it is supported if either fits.
ALTERNATIVE_DEFINITIONS = {"macro_f1": ("macro_f1_of_macro_pr", "F1 of macro precision and macro recall")}


def display_unit(metric: str, disp: str, computed: Optional[float]) -> str:
    """Unit for comparing a display: a percent display of a fraction-valued result ("93.18%" vs 0.9318)
    is a ratio even when the metric name is generic (e.g. op 'ratio' or 'mean')."""
    unit = metric_unit(metric)
    if unit == "raw" and disp.strip().endswith("%") and computed is not None and abs(computed) <= 1.5:
        return "ratio"
    return unit


def metric_unit(metric: str) -> str:
    if metric in RATIO_METRICS or metric.split("_")[0] in ("precision", "recall", "f1"):
        return "ratio"
    if metric == "pp_change":
        return "pp"
    return "raw"


def run_check(op: str, inputs: dict, claimed: dict[str, str]) -> dict:
    """Compute and compare. Never raises on bad inputs: returns status 'unresolved' instead."""
    inputs = normalize_inputs(inputs)
    result: dict[str, Any] = {"op": op, "inputs": inputs, "code_version": CODE_VERSION,
                              "rounding_rule": "displayed values compared by half-unit rounding interval",
                              "outputs": {}, "notes": [], "comparisons": [], "error": None}
    try:
        outputs, notes = OPS[op](inputs)
    except (NumericInputError, KeyError, TypeError, ValueError) as e:
        result["error"] = f"{type(e).__name__}: {e}"
        for metric, disp in claimed.items():
            result["comparisons"].append({"metric": metric, "claimed": disp, "computed": None,
                                          "status": "unresolved", "reason": "inputs not usable: " + str(e)})
        return result
    result["outputs"], result["notes"] = outputs, notes
    ranges = output_ranges(op, inputs)
    if ranges:
        result["rounding_rule"] += "; input rounding propagated (inputs treated as displayed values)"
    hint = inputs.get("claimed_scale")
    for metric, disp in claimed.items():
        comp = {"metric": metric, "claimed": disp, "computed": outputs.get(metric), "status": "unresolved",
                "reason": None}
        if metric not in outputs:
            comp["reason"] = f"metric '{metric}' is not produced by op '{op}' (name the averaging explicitly)"
        elif outputs[metric] is None:
            comp["reason"] = "metric not determinable from the given inputs"
        elif isinstance(outputs[metric], bool):
            want = disp.strip().lower() in ("true", "yes", "compatible")
            comp["status"] = "supported" if outputs[metric] == want else "contradicted"
        elif INEQ_RE.match(disp.strip()):
            m = INEQ_RE.match(disp.strip())
            op_, bound_txt = m.group(1), m.group(2)
            try:
                lo, hi, desc = parse_display(bound_txt, display_unit(metric, bound_txt, outputs[metric]), hint)
                bound = (lo + hi) / 2  # the stated bound itself, not its rounding interval
                v = float(outputs[metric])

                def holds(x: float) -> bool:
                    return {">": x > bound + EPS, ">=": x >= bound - EPS, "≥": x >= bound - EPS,
                            "<": x < bound - EPS, "<=": x <= bound + EPS, "≤": x <= bound + EPS}[op_]
                vlo, vhi = ranges.get(metric, (v, v))
                # contradicted only if no value consistent with the rounded inputs satisfies the bound
                if holds(v):
                    comp["status"] = "supported"
                elif holds(vlo) or holds(vhi):
                    comp["status"] = "supported"
                    comp["reason_extra"] = "holds only within input rounding"
                else:
                    comp["status"] = "contradicted"
                comp["reason"] = f"claimed bound {disp}; computed {v:g}" + (
                    f" (range under input rounding [{vlo:g}, {vhi:g}])" if metric in ranges else "")
            except NumericInputError as e:
                comp["reason"] = str(e)
        else:
            try:
                lo, hi, desc = parse_display(disp, display_unit(metric, disp, outputs[metric]), hint)
                v = float(outputs[metric])
                comp["interval"] = [lo, hi]
                vlo, vhi = ranges.get(metric, (v, v))
                comp["status"] = "supported" if intervals_overlap((lo, hi), (vlo, vhi)) else "contradicted"
                comp["reason"] = desc + (f"; computed range under input rounding [{vlo:g}, {vhi:g}]"
                                         if metric in ranges else "")
                alt = ALTERNATIVE_DEFINITIONS.get(metric)
                if comp["status"] == "contradicted" and alt and outputs.get(alt[0]) is not None:
                    av = float(outputs[alt[0]])
                    if intervals_overlap((lo, hi), (av, av)):
                        comp["status"] = "supported"
                        comp["computed"] = av
                        comp["reason"] += f"; matches the alternative definition ({alt[1]}) = {av:g}"
            except NumericInputError as e:
                comp["reason"] = str(e)
        result["comparisons"].append(comp)
    return result


def numbers_in(obj: Any) -> list[float]:
    if isinstance(obj, bool):
        return []
    if isinstance(obj, (int, float)):
        return [float(obj)]
    if isinstance(obj, str):  # inputs may be given as displayed strings ("91.47") to keep their precision
        try:
            return [float(obj.strip().replace("−", "-").replace(",", "").rstrip("%"))]
        except ValueError:
            return []
    if isinstance(obj, list):
        return [x for v in obj for x in numbers_in(v)]
    if isinstance(obj, dict):
        return [x for k, v in obj.items() if k not in ("orientation", "unit", "labels", "claimed_scale", "constants")
                for x in numbers_in(v)]
    return []


def inputs_found_in_text(inputs: dict, text: str) -> tuple[bool, list[float]]:
    """Every numeric input must literally appear in the quoted source (spec: inputs verified)."""
    missing = []
    inputs = normalize_inputs(inputs)
    hay = text.replace(",", "")
    for v in numbers_in(inputs):
        forms = {f"{v:g}", str(v)}
        if v == int(v):
            forms.add(str(int(v)))
        if 0 < abs(v) < 1:
            forms.add(f"{v:g}".lstrip("0"))
            forms.add(f"{v * 100:g}")
            plain = f"{v:.12f}".rstrip("0")  # 3.6e-05 is printed as 0.000036
            forms.update({plain, plain.lstrip("-").lstrip("0") if v > 0 else plain})
        if abs(v) >= 1e5:  # "3.3 · 10^18" / "9.5 TFLOPS": match the printed mantissa
            mant = abs(v) / 10 ** math.floor(math.log10(abs(v)))
            forms.add(f"{round(mant, 6):g}")
            forms.add(f"{round(mant * 10, 6):g}")
            forms.add(f"{round(mant * 100, 6):g}")
        # "0.958" also matches a printed "0.9580"; "12" also matches "12.0" (never "120")
        if not any(re.search(rf"(?<![\d.]){re.escape(f)}{'0*' if '.' in f else '(?:[.]0+)?'}(?![\d])", hay)
                   for f in forms):
            missing.append(v)
    return (not missing), missing
