"""Challenge 14: precision/recall/F1, uncertainty, and subgroup gates that hold when unmeasurable."""
from capstone.scorecard import scorecard, wilson


def rows(group, n, *, positives, tp, fp=0):
    """n rows in one group: `positives` labeled 1, of which `tp` predicted 1; plus `fp` false alarms."""
    out = []
    for i in range(n):
        true = 1 if i < positives else 0
        pred = 1 if (i < tp) or (positives <= i < positives + fp) else 0
        out.append({"true": true, "pred": pred, "group": group})
    return out


def test_balanced_model_passes_with_intervals():
    r = scorecard(rows("A", 100, positives=50, tp=48, fp=2) + rows("B", 100, positives=50, tp=47, fp=3))
    assert r["decision"] == "READY_FOR_HUMAN_REVIEW", r["failed_gates"]
    assert r["precision"] == 0.95 and r["recall"] == 0.95 and r["f1"] == 0.95
    lo, hi = r["recall_ci95"]
    assert lo < 0.95 < hi
    assert r["groups"]["A"]["recall_ci95"][0] < r["groups"]["A"]["recall"]


def test_zero_denominators_are_none_and_hold():
    # Nobody is predicted positive, and nobody is truly positive: precision and recall are undefined.
    r = scorecard([{"true": 0, "pred": 0, "group": "A"} for _ in range(100)])
    assert r["precision"] is None and r["recall"] is None and r["f1"] is None
    assert {"precision_unmeasurable", "recall_unmeasurable"} <= set(r["failed_gates"])
    assert wilson(0, 0) is None


def test_small_group_recall_is_not_compared():
    r = scorecard(rows("A", 120, positives=60, tp=58) + rows("B", 25, positives=5, tp=1))
    assert "recall_unmeasurable:B" in r["failed_gates"] and "small_subgroup" not in r["failed_gates"]
    assert r["recall_gap"] is None  # only one group had enough positives to compare
    assert r["decision"] == "HOLD"


def test_accuracy_passes_but_recall_fails():
    # 10% prevalence: predicting almost everything negative looks accurate but misses positives.
    r = scorecard(rows("A", 200, positives=20, tp=5))
    assert r["accuracy"] >= 0.8 and "accuracy" not in r["failed_gates"]
    assert r["recall"] == 0.25 and "recall" in r["failed_gates"]


def test_overall_pass_hides_a_failing_subgroup():
    # Group B is a quarter of the data and its recall is poor; overall recall still clears 0.80.
    r = scorecard(rows("A", 300, positives=150, tp=150) + rows("B", 100, positives=50, tp=30))
    assert r["recall"] >= 0.8 and r["accuracy"] >= 0.8
    assert "group_recall:B" in r["failed_gates"] and "recall_gap" in r["failed_gates"]
    assert r["decision"] == "HOLD"
