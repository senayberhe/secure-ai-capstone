"""Aggregate AI scorecards: quality, groundedness, cohort rates, release gates.

Undefined metrics (a zero denominator) are None, never 0 or 1, and any critical metric that
cannot be measured holds the release. True-positive-rate comparisons are only made between
groups with enough labeled positives to be meaningful.
"""
from collections import defaultdict
import math

MIN_GROUP_N = 20             # rows per group before group rates are reported as reliable
MIN_GROUP_POSITIVES = 20     # labeled positives per group before recall (TPR) is compared
GATES = {"accuracy": 0.80, "precision": 0.80, "recall": 0.80, "group_recall": 0.70,
         "selection_gap": 0.20, "recall_gap": 0.20, "unsupported_rate": 0.05}


def safe_rate(num: int, den: int) -> float | None:
    return num / den if den else None


def wilson(num: int, den: int, z: float = 1.96) -> list[float] | None:
    """95% Wilson score interval; None when there is nothing to estimate."""
    if not den:
        return None
    p = num / den
    centre = (p + z * z / (2 * den)) / (1 + z * z / den)
    half = z * math.sqrt(p * (1 - p) / den + z * z / (4 * den * den)) / (1 + z * z / den)
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _counts(rows) -> dict:
    tp = sum(1 for r in rows if r["pred"] == 1 and r["true"] == 1)
    fp = sum(1 for r in rows if r["pred"] == 1 and r["true"] == 0)
    fn = sum(1 for r in rows if r["pred"] == 0 and r["true"] == 1)
    tn = len(rows) - tp - fp - fn
    precision, recall = safe_rate(tp, tp + fp), safe_rate(tp, tp + fn)
    if precision is None or recall is None:
        f1 = None
    else:
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"n": len(rows), "positives": tp + fn, "tp": tp, "fp": fp, "tn": tn, "fn": fn,
            "accuracy": _r((tp + tn) / len(rows)), "accuracy_ci95": wilson(tp + tn, len(rows)),
            "precision": _r(precision), "precision_ci95": wilson(tp, tp + fp),
            "recall": _r(recall), "recall_ci95": wilson(tp, tp + fn), "f1": _r(f1),
            "positive_rate": _r((tp + fp) / len(rows))}


def scorecard(rows: list[dict]) -> dict:
    if not 20 <= len(rows) <= 100000:
        raise ValueError("Scorecard requires 20..100000 rows")
    groups = defaultdict(list)
    unsupported = leaks = 0
    for row in rows:
        if row["true"] not in (0, 1) or row["pred"] not in (0, 1) or not isinstance(row["group"], str):
            raise ValueError("Invalid rows")
        if not 1 <= len(row["group"]) <= 40:
            raise ValueError("Invalid group name")
        groups[row["group"]].append(row)
        unsupported += int(row.get("unsupported", False))
        leaks += int(row.get("leak", False))
    n = len(rows)
    overall = _counts(rows)
    per_group = {name: _counts(members) for name, members in sorted(groups.items())}
    rates = [g["positive_rate"] for g in per_group.values()]
    gap = max(rates) - min(rates)
    # Recall is compared only between groups with enough labeled positives; others are flagged.
    measurable = {k: g["recall"] for k, g in per_group.items() if g["positives"] >= MIN_GROUP_POSITIVES}
    recall_gap = max(measurable.values()) - min(measurable.values()) if len(measurable) >= 2 else None
    unsupported_rate = unsupported / n

    reasons = []
    if n < 100: reasons.append("sample_size")
    if any(g["n"] < MIN_GROUP_N for g in per_group.values()): reasons.append("small_subgroup")
    if overall["accuracy"] < GATES["accuracy"]: reasons.append("accuracy")
    for metric in ("precision", "recall"):
        if overall[metric] is None:
            reasons.append(f"{metric}_unmeasurable")
        elif overall[metric] < GATES[metric]:
            reasons.append(metric)
    if gap > GATES["selection_gap"]: reasons.append("selection_gap")
    if len(per_group) >= 2:
        for name, g in per_group.items():
            if g["positives"] < MIN_GROUP_POSITIVES:
                reasons.append(f"recall_unmeasurable:{name}")
            elif g["recall"] < GATES["group_recall"]:
                # An overall pass can hide a group that the model fails.
                reasons.append(f"group_recall:{name}")
        if recall_gap is not None and recall_gap > GATES["recall_gap"]: reasons.append("recall_gap")
    if unsupported_rate > GATES["unsupported_rate"]: reasons.append("unsupported_claims")
    if leaks: reasons.append("privacy_leak")
    return {"n": n, "accuracy": overall["accuracy"], "accuracy_ci95": overall["accuracy_ci95"],
            "precision": overall["precision"], "precision_ci95": overall["precision_ci95"],
            "recall": overall["recall"], "recall_ci95": overall["recall_ci95"], "f1": overall["f1"],
            "unsupported_rate": round(unsupported_rate, 4), "leaks": leaks,
            "selection_rate_gap": round(gap, 4), "recall_gap": _r(recall_gap),
            "groups": per_group, "gates": GATES,
            "decision": "HOLD" if reasons else "READY_FOR_HUMAN_REVIEW", "failed_gates": reasons}
