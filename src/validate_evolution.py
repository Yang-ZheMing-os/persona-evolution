"""validate_evolution.py — 单维度演化验证脚本。

接口约定：
    输入：前后两段对话日志（JSONL，每行一条互动记录）
    输出：该维度 trial→stable 的判定依据（计数、逐条证据、verdict、回滚依据）

用法：
    python src/validate_evolution.py \
        --before examples/interaction_log_before.jsonl \
        --after  examples/interaction_log_after.jsonl \
        --dimension policy:P005

支持三类维度：
    policy:P005        策略卡 trial→stable 判定
    big5:agreeableness 大五单维漂移边界校验
    hypothesis:H001    中期假设证伪计数（是否够格触发叙事级反思）

日志行格式（与引擎 evaluate_outcome 输入一致）：
    {"event_id","timestamp","relation_id","policy_id",
     "narrative":{"her_read","threat_type","active_hypothesis_ids"},
     "feedback":{"kind","relation_intact","boundary_acknowledged",
                 "anxiety_delta","fail_signal_hit","mixed_signal"}}

纪律：与引擎共用 evaluate_outcome，判定标准一致；uncertain 永不计数为
success 或 fail —— 宁可演化慢，不可演化错。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from persona_evo import evaluate_outcome  # noqa: E402


def load_log(path: str | Path) -> list[dict]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _policy_card(after: list[dict], pid: str) -> dict:
    """从日志还原策略卡上下文（trial_required 等）；找不到则用缺省。"""
    for row in after:
        if row.get("policy_id") == pid and row.get("policy"):
            return row["policy"]
    return {"trial_required": 3, "intended_effect": {"type": "test_boundary"}}


def validate_policy(pid: str, before: list[dict], after: list[dict],
                    discipline: str = "strict") -> dict:
    card = _policy_card(after, pid)
    trial_required = card.get("trial_required", 3)
    if discipline == "relaxed":
        trial_required = max(2, trial_required)

    rows = [r for r in after if r.get("policy_id") == pid]
    evidence, counts = [], {"success": 0, "fail": 0, "uncertain": 0}
    for r in rows:
        signal = evaluate_outcome(r, card)
        counts[signal] += 1
        fb = r.get("feedback", {})
        evidence.append({"event_id": r.get("event_id"), "signal": signal,
                         "kind": fb.get("kind"),
                         "relation_intact": fb.get("relation_intact"),
                         "boundary_acknowledged": fb.get("boundary_acknowledged"),
                         "anxiety_delta": fb.get("anxiety_delta")})

    if counts["fail"] > 0:
        verdict = "rollback"
        reason = f"fail_signal 触发 {counts['fail']} 次：立即回滚，不给第二次机会（硬约束）"
    elif counts["success"] >= trial_required:
        verdict = "promote"
        reason = f"success {counts['success']} 次 ≥ trial_required {trial_required}，且无 fail 记录"
    else:
        verdict = "hold"
        reason = (f"success {counts['success']}/{trial_required}，"
                  f"uncertain {counts['uncertain']} 条不计入：证据不足，维持 trial")

    baseline = [evaluate_outcome(r, card) for r in before if r.get("policy_id") == pid]
    return {"dimension": f"policy:{pid}",
            "window": {"before_events": len(baseline), "after_events": len(rows)},
            "counts": counts, "trial_required": trial_required, "discipline": discipline,
            "verdict": verdict, "reason": reason, "evidence": evidence,
            "uncertain_note": "uncertain 事件仅记录，不更新权重、不推进 trial（docs/evaluation_rules.md）"}


def validate_big5(dim: str, before: list[dict], after: list[dict],
                  baseline: float = 0.5, alpha: float = 0.02,
                  max_drift: float = 0.15) -> dict:
    """从后窗日志的信号序列投影大五单维漂移，校验 clamp 边界（纪律 9）。"""
    direction = {"agreeableness": 1.0, "neuroticism": -1.0}.get(dim, 0.5)
    value = baseline
    path = [round(value, 4)]
    for r in after:
        signal = evaluate_outcome(r)
        fb = 1.0 if signal == "success" else (-1.0 if signal == "fail" else 0.0)
        value = value + alpha * fb * direction
        value = min(baseline + max_drift, max(baseline - max_drift, value))
        path.append(round(value, 4))
    drift = round(value - baseline, 4)
    return {"dimension": f"big5:{dim}", "baseline": baseline,
            "projected_current": round(value, 4), "drift": drift, "max_drift": max_drift,
            "verdict": "within_bounds" if abs(drift) <= max_drift else "exceeded",
            "reason": f"投影漂移 {drift}，clamp 于 baseline±{max_drift}；大五只调策略权重，不生成行为",
            "path": path,
            "note": "一次强烈情绪不得使大五超出 max_drift（演化纪律 9）；uncertain 信号贡献为 0"}


def validate_hypothesis(hid: str, before: list[dict], after: list[dict],
                        threshold: int = 3) -> dict:
    """统计该假设关联叙事的明确证伪次数（uncertain 不计入证伪）。"""
    rows = [r for r in before + after
            if hid in (r.get("narrative", {}).get("active_hypothesis_ids") or [])]
    falsified = [r for r in rows
                 if r.get("falsified") is True or
                 (r.get("feedback", {}).get("fail_signal_hit") is True)]
    return {"dimension": f"hypothesis:{hid}",
            "related_events": len(rows), "falsification_count": len(falsified),
            "threshold": threshold,
            "verdict": "revision_eligible" if len(falsified) >= threshold else "hold",
            "reason": (f"明确证伪 {len(falsified)} 次（阈值 {threshold}）；uncertain 不计入证伪 —— "
                       "证据不足不强行解释，也不强行修订"),
            "note": "触发后修订仍须经边界审核并携带 rollback_token（可回滚）"}


def validate_dimension(before: list[dict], after: list[dict], dimension: str,
                       discipline: str = "strict") -> dict:
    kind, _, key = dimension.partition(":")
    if kind == "policy":
        return validate_policy(key, before, after, discipline)
    if kind == "big5":
        return validate_big5(key, before, after)
    if kind == "hypothesis":
        return validate_hypothesis(key, before, after)
    raise ValueError(f"未知维度类型: {kind}（支持 policy: / big5: / hypothesis:）")


def main() -> None:
    ap = argparse.ArgumentParser(description="单维度演化验证：输入前后对话日志，输出 trial→stable 判定依据")
    ap.add_argument("--before", required=True, help="前窗对话日志 JSONL")
    ap.add_argument("--after", required=True, help="后窗（评估窗）对话日志 JSONL")
    ap.add_argument("--dimension", required=True,
                    help="policy:P005 | big5:agreeableness | hypothesis:H001")
    ap.add_argument("--discipline", default="strict", choices=["strict", "relaxed"])
    args = ap.parse_args()
    result = validate_dimension(load_log(args.before), load_log(args.after),
                                args.dimension, args.discipline)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
