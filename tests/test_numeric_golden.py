"""Golden numeric cases A01-A04 (spec §16.2), recomputed by the deterministic module."""
import pytest

from scholarly_review.modules import numeric


def approx(x, y, tol=1e-6):
    return abs(x - y) <= tol


def test_a01_confusion_matrix_metrics_named_separately():
    r = numeric.run_check("confusion_metrics", {"matrix": [[90, 10], [20, 30]], "orientation": "rows_true"},
                          {"accuracy": "80%", "macro_f1": "80%"})
    o = r["outputs"]
    assert approx(o["accuracy"], 0.80)
    assert approx(o["macro_f1"], 0.761905, 1e-6)
    assert approx(o["weighted_f1"], 0.793651, 1e-6)
    assert approx(o["micro_f1"], 0.80)
    assert approx(o["kappa"], 0.526316, 1e-6)
    status = {c["metric"]: c["status"] for c in r["comparisons"]}
    assert status == {"accuracy": "supported", "macro_f1": "contradicted"}


def test_a01_orientation_must_be_explicit():
    r = numeric.run_check("confusion_metrics", {"matrix": [[90, 10], [20, 30]]}, {"accuracy": "80%"})
    assert r["comparisons"][0]["status"] == "unresolved"


def test_a02_row_normalised_without_supports():
    r = numeric.run_check("normalized_confusion", {"matrix": [[0.9, 0.1], [0.4, 0.6]], "orientation": "rows_true"},
                          {"macro_recall": "75%", "accuracy": "75%", "weighted_f1": "70%"})
    o = r["outputs"]
    assert approx(o["macro_recall"], 0.75)
    assert o["accuracy"] is None and o["weighted_f1"] is None
    status = {c["metric"]: c["status"] for c in r["comparisons"]}
    assert status == {"macro_recall": "supported", "accuracy": "unresolved", "weighted_f1": "unresolved"}


def test_a03_pp_vs_relative_and_rounding():
    r = numeric.run_check("change", {"old": 80, "new": 84, "unit": "percent"},
                          {"pp_change": "4", "relative_change": "5%"})
    assert approx(r["outputs"]["pp_change"], 4) and approx(r["outputs"]["relative_change"], 0.05)
    assert all(c["status"] == "supported" for c in r["comparisons"])
    zero = numeric.run_check("change", {"old": 0, "new": 4, "unit": "percent"}, {"relative_change": "100%"})
    assert zero["outputs"]["relative_change"] is None
    assert zero["comparisons"][0]["status"] == "unresolved"
    comp = numeric.run_check("rounding_compatible", {"displays": ["84.0%", "0.8402"]}, {"compatible": "true"})
    assert comp["outputs"]["compatible"] is True
    assert comp["comparisons"][0]["status"] == "supported"


def test_a04_no_pvalue_without_variance():
    r = numeric.run_check("mean_difference", {"a": 84, "b": 83, "unit": "percent", "n_runs": 5}, {"pp_change": "1"})
    assert approx(r["outputs"]["pp_change"], 1)
    assert r["outputs"]["significance"] == "unknown"
    assert "p_value" not in r["outputs"]
    assert any("NOT judged non-significant" in n for n in r["notes"])


@pytest.mark.parametrize("disp,unit,ok", [("84.0%", "ratio", True), ("0.8402", "ratio", True), ("76.2", "ratio", False)])
def test_display_parsing(disp, unit, ok):
    if ok:
        lo, hi, _ = numeric.parse_display(disp, unit)
        assert lo < hi
    else:
        with pytest.raises(numeric.NumericInputError):
            numeric.parse_display(disp, unit)


def test_inputs_must_appear_in_quotes():
    found, missing = numeric.inputs_found_in_text({"matrix": [[90, 10], [20, 30]], "orientation": "rows_true"},
                                                  "Positive 90 10 Negative 20 30")
    assert found and not missing
    found, missing = numeric.inputs_found_in_text({"matrix": [[90, 10], [20, 31]], "orientation": "rows_true"},
                                                  "Positive 90 10 Negative 20 30")
    assert not found and missing == [31.0]
