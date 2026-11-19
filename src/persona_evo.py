"""persona_evo.py — 通用人格演化引擎（v2，性格无关）。

三文件制：
- persona.json        她的人格状态（SELF 槽位 + 演化状态），仅引擎可写（SELF 段除外）
- persona.config.json 环境配置（模型插槽/适配器/tick），仅使用者可写，引擎只读
- state.json          运行时快照，引擎每次写盘时重建（可随时删除）

设计纪律（与 SKILL.md 对齐）：
- 性格无关：一切性格内容在 persona.json 槽位内，引擎不预设。
- 不产出任何台词；行为生成只返回「意图 + 边界 + hard_rules」约束包。
- 不存反应清单；策略按叙事（threat_type / her_read_pattern / safety_band）匹配。
- 情绪不先于叙事：on_interaction 步骤顺序不可重排。
- uncertain 一律不更新权重、不推进 trial：宁可演化慢，不可演化错。
- SELF 无写入口；写盘前逐字比对 SELF 快照。

依赖：仅标准库。LLM 能力经 LLMHooks 注入（见 src/adapter.py）；未注入时用
HeuristicLLM，其归因恒为 insufficient_data（→ BLINDSPOT），保证无模型环境下
系统只会更保守、不会瞎演化。
"""

from __future__ import annotations

import json
import math
import random
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

Signal = Literal["success", "fail", "uncertain"]
FEEDBACK_KINDS = ("LIKED", "DISLIKE", "BOUNDARY_EVENT", "NEUTRAL")
GROWTH_EFFECTS = {"accept_partially", "acknowledge_minimally"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# 评估规则（可执行版；完整定义见 docs/evaluation_rules.md）
# 与 validate_evolution.py 共用同一实现，保证线上判定与验证判定永不漂移。
# ---------------------------------------------------------------------------

def evaluate_outcome(outcome: dict, policy: dict | None = None) -> Signal:
    """R1-R5 的代码化。判定顺序即优先级，默认出口是 uncertain 而非 success。"""
    fb = outcome.get("feedback") or {}
    kind = fb.get("kind")

    # R5 边界事件例外：失败原因在对方自身边界状态，不能归因于策略 → uncertain
    if kind == "BOUNDARY_EVENT":
        return "uncertain"

    # R4 数据缺失：关键字段不完整 → uncertain
    if kind not in ("LIKED", "DISLIKE", "NEUTRAL"):
        return "uncertain"
    if fb.get("relation_intact") is None or fb.get("boundary_acknowledged") is None:
        return "uncertain"

    anxiety_delta = fb.get("anxiety_delta")
    if anxiety_delta is None:
        anxiety_delta = 0.0

    # R2 fail（任一即 fail，立即回滚）
    if fb.get("relation_intact") is False:
        return "fail"
    if fb.get("fail_signal_hit") is True:
        return "fail"
    if kind == "DISLIKE" and anxiety_delta >= 0.2:
        return "fail"
    # 成长型策略推太快：解读对但步子超安全感 → fail（归因 strategy_error）
    if policy and policy.get("intended_effect", {}).get("type") in GROWTH_EFFECTS:
        if anxiety_delta >= 0.2:
            return "fail"

    # R3 混合信号 → uncertain
    if fb.get("mixed_signal") is True:
        return "uncertain"
    if kind == "DISLIKE" and fb.get("boundary_acknowledged") is True:
        return "uncertain"   # 点踩但边界被承认：信号矛盾，证据不足
    if kind == "LIKED" and fb.get("boundary_acknowledged") is False:
        return "uncertain"   # 点赞但边界未被承认：可能是礼貌性正向

    # R1 success（全部满足）
    if kind in ("LIKED", "NEUTRAL") and fb.get("relation_intact") is True \
            and fb.get("boundary_acknowledged") is True and anxiety_delta <= 0.05:
        return "success"

    return "uncertain"


# ---------------------------------------------------------------------------
# OCC → Plutchik 映射（本地规则表；情绪是叙事的读数）
# ---------------------------------------------------------------------------

def occ_appraise(narrative: dict, relation: dict, mood: dict) -> dict:
    """OCC 评估维度：效价 / 预期 / 对象 / 可控性。纯本地规则。"""
    threat = narrative.get("threat_type", "none")
    valence = -1.0 if threat != "none" else 0.5
    if threat == "relational_uncertainty":
        valence = -0.3
    expectedness = 0.8 if relation.get("trust", 0.3) < 0.4 else 0.4
    target = "self" if threat == "exposure_threat" else ("other" if threat == "autonomy_threat" else "relation")
    controllability = 0.7 if threat == "autonomy_threat" else 0.4
    return {"valence": valence, "expectedness": expectedness,
            "target": target, "controllability": controllability}


OCC_PLUTCHIK: dict[tuple, list[tuple]] = {
    ("neg", "self", "low"): [("shame", 0.9), ("wariness", 0.4)],
    ("neg", "self", "mid"): [("wariness", 0.7), ("anxiety", 0.5)],
    ("neg", "other", "high"): [("anger", 0.8), ("wariness", 0.3)],
    ("neg", "other", "low"): [("anxiety", 0.6), ("wariness", 0.6)],
    ("neg", "relation", "mid"): [("betrayal", 0.5), ("anxiety", 0.7)],
    ("pos", "relation", "any"): [("joy", 0.6), ("anxiety", 0.2)],
    ("pos", "self", "any"): [("joy", 0.5), ("shame", 0.3)],
}


def occ_to_plutchik(occ: dict, intensity: float = 0.6) -> list[dict]:
    key = (
        "neg" if occ["valence"] < 0 else "pos",
        occ["target"],
        "high" if occ["controllability"] >= 0.6 else ("mid" if occ["controllability"] >= 0.4 else "low"),
    )
    pairs = OCC_PLUTCHIK.get(key, [("wariness", 0.4)])
    out = []
    for kind, w in pairs:
        out.append({
            "kind": kind,
            "intensity": round(min(1.0, intensity * w + 0.1 * (1 - occ["expectedness"])), 3),
            "decay_rate": 0.35 if kind in ("joy", "relief") else 0.25,
            "target": occ["target"],
            "rounds_left": 2,
        })
    return out


# ---------------------------------------------------------------------------
# LLM 挂钩：narrate / generate_behavior / attribute / propose_*（由适配器注入）
# ---------------------------------------------------------------------------

class LLMHooks:
    """引擎对模型能力的全部期望（三项必须 + 两项可选）。"""

    narrate: Callable[..., dict] = lambda self, event, relation, tension_sig: {
        "trigger_scene": event.get("semantic", ""),
        "her_read": "", "threat_type": "none", "active_hypothesis_ids": [],
    }
    generate_behavior: Callable[..., dict] = lambda self, policy, narrative, hard_rules, emotion: {
        "policy_id": policy["id"],
        "constraints": {"intent": policy["verbal_pattern"]["intent"],
                        "boundary": policy["verbal_pattern"]["boundary"],
                        "hard_rules": hard_rules},
    }
    attribute: Callable[..., dict] = lambda self, outcome: {"attribution": "insufficient_data", "param_changes": []}
    propose_policy: Callable[..., dict | None] = lambda self, reflection: None
    propose_revision: Callable[..., dict | None] = lambda self, trigger_ctx: None


class HeuristicLLM(LLMHooks):
    """无模型兜底：归因恒为 insufficient_data → BLINDSPOT。宁可慢，不可错。"""

    def narrate(self, event, relation, tension_sig):
        return {"trigger_scene": event.get("semantic", ""),
                "her_read": "(prefill 缺失) 信息不足，无法生成第一人称解读",
                "threat_type": "none", "active_hypothesis_ids": []}


# ---------------------------------------------------------------------------
# 引擎本体
# ---------------------------------------------------------------------------

class PersonaEvo:
    """PersonaEvoPlugin 接口参考实现。

    on_interaction(event) -> Behavior
    on_feedback(outcome) -> Signal
    reflect_event(outcomes=None) -> None          # 支持批量
    maybe_reflect_narrative() -> dict | None
    propose_policy(reflection) -> dict | None
    evaluate_trial(policy_id) -> str
    tick() -> None
    build_prompt_context() -> dict
    get_state() -> dict
    self_write_policy(candidate) -> dict | None
    rollback_hypothesis(token) -> bool
    # 注意：接口刻意的没有 set_self / modify_anchor —— SELF 无写入口
    """

    def __init__(self, root: str | Path, llm: LLMHooks | None = None):
        self.root = Path(root)
        self.llm = llm or HeuristicLLM()
        self.state_path = self.root / "persona.json"
        self.snapshot_path = self.root / "state.json"
        self.config_path = self.root / "persona.config.json"
        self.audit_path = self.root / "logs" / "audit_log.jsonl"
        self.config: dict = self._load_json(self.config_path, {})
        self.state: dict = self._load_json(self.state_path, None) or {}
        if not self.state.get("policy_lib", {}).get("initialized"):
            self._init_from_seeds()
        self._pending_reflections: list[dict] = []

    # ---- 基础设施 ---------------------------------------------------------

    @staticmethod
    def _load_json(path: Path, default: Any) -> Any:
        try:
            text = path.read_text(encoding="utf-8-sig")
            return json.loads(text)
        except FileNotFoundError:
            return default

    def _init_from_seeds(self) -> None:
        seeds = self._load_json(self.root / "policies" / "seed_policies.json", None)
        if not seeds:
            return
        self.state.setdefault("policy_lib", {})["policies"] = seeds["policies"]
        self.state["policy_lib"]["seed_source"] = "policies/seed_policies.json"
        self.state["policy_lib"]["initialized"] = True
        self._save()
        self._audit("policy", "init_from_seeds", before={"policies": []},
                    after={"count": len(seeds["policies"])},
                    trigger="首次加载：种子策略仅作起点，不穷举")

    def _save(self) -> None:
        self._guard_self_immutable()
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        self._write_snapshot()

    def _write_snapshot(self) -> None:
        """state.json：运行时快照，由 persona.json 派生，可随时重建。"""
        active = [p for p in self.state.get("policy_lib", {}).get("policies", [])
                  if p.get("status") in ("trial", "stable")][:3]
        last = (self.state.get("reflection", {}).get("event_reflections") or [None])[-1]
        bs = [b for b in self.state.get("blindspots", []) if b.get("status") == "open"]
        snapshot = {
            "current_emotion": {
                "immediate": self.state.get("emotion", {}).get("immediate", []),
                "mood": self.state.get("emotion", {}).get("mood", {}),
            },
            "active_policies": [
                {"id": p["id"], "intent": p["verbal_pattern"]["intent"],
                 "boundary": p["verbal_pattern"]["boundary"], "status": p["status"]}
                for p in active],
            "last_reflection_summary": (
                f"归因={last['attribution']}；参数微调 {len(last.get('param_changes', []))} 项" if last else None),
            "open_blindspots": [b["question"] for b in bs],
            "tick_count": self.state.get("tick_count", 0),
            "snapshot_at": now_iso(),
        }
        self.snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    def _audit(self, layer: str, op: str, *, before: Any, after: Any,
               trigger: str, rollback_token: str | None = None) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"log_id": new_id(), "timestamp": now_iso(), "layer": layer, "op": op,
                 "before": before, "after": after, "trigger": trigger,
                 "rollback_token": rollback_token}
        with self.audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _policy(self, pid: str) -> dict | None:
        for p in self.state.get("policy_lib", {}).get("policies", []):
            if p["id"] == pid:
                return p
        return None

    def _relation(self, rid: str) -> dict:
        for r in self.state.get("relations", []):
            if r["relation_id"] == rid:
                return r
        r = {"relation_id": rid, "trust": 0.3, "safety": 0.3,
             "autonomy_threat": 0.3, "exposure_fear": 0.5, "key_events": []}
        self.state.setdefault("relations", []).append(r)
        return r

    def _account(self, rid: str) -> dict:
        for a in self.state.get("midterm", {}).get("relation_accounts", []):
            if a["relation_id"] == rid:
                return a
        a = {"relation_id": rid, "balance": 0.0, "overdrawn": False,
             "repair_stage": "none", "last_transaction": ""}
        self.state.setdefault("midterm", {}).setdefault("relation_accounts", []).append(a)
        return a

    def _safety_band(self, relation: dict) -> str:
        s = relation.get("safety", 0.3)
        if s < 0.35:
            return "low"
        if s < 0.6:
            return "mid"
        return "rising"

    # ---- L0 守卫：任何路径都无权写 SELF ------------------------------------

    def _guard_self_immutable(self) -> None:
        """写盘前断言：SELF 段与本引擎首次加载时的快照逐字一致。"""
        frozen = getattr(self, "_self_frozen", None)
        if frozen is None:
            object.__setattr__(self, "_self_frozen",
                               json.dumps(self.state.get("self", {}), sort_keys=True))
            return
        current = json.dumps(self.state.get("self", {}), sort_keys=True)
        if current != frozen:
            raise RuntimeError("SELF 锚点被非法修改 —— 防漂移硬约束触发，拒绝写回")

    # ---- 演化循环 ---------------------------------------------------------

    def on_interaction(self, event: dict) -> dict:
        rid = event.get("relation_id", "unknown")
        relation = self._relation(rid)
        account = self._account(rid)
        tension_sig = [t["narrative_signature"]
                       for t in self.state.get("midterm", {}).get("open_tensions", [])
                       if not t.get("resolved")]

        # 1. 叙事先行（L3）：her_read / threat_type 由模型能力生成
        narrative = dict(self.llm.narrate(event, relation, tension_sig))
        narrative.setdefault("trigger_scene", event.get("semantic", ""))
        unit = {
            "unit_id": new_id(),
            "trigger_scene": narrative["trigger_scene"],
            "her_read": narrative["her_read"],
            "threat_type": narrative.get("threat_type", "none"),
            "active_hypothesis_ids": narrative.get("active_hypothesis_ids", []),
            "falsified": False,
            "timestamp": now_iso(),
        }
        self.state.setdefault("narrative", {}).setdefault("recent_units", []).append(unit)
        self.state["narrative"]["recent_units"] = self.state["narrative"]["recent_units"][-50:]

        # 2. 情绪 = 叙事的读数（L4：OCC → Plutchik）
        occ = occ_appraise(unit, relation, self.state.get("emotion", {}).get("mood", {}))
        intensity = 0.5 + 0.3 * (1 - relation.get("safety", 0.3)) + 0.2 * max(0.0, account.get("balance", 0.0))
        immediate = occ_to_plutchik(occ, min(1.0, max(0.2, intensity)))
        emotion = self.state.setdefault("emotion", {})
        emotion["immediate"] = (emotion.get("immediate", []) + immediate)[-8:]
        m = emotion.setdefault("mood", {"valence": 0.0, "arousal": 0.4, "dominance": 0.5})
        m["valence"] = round(0.85 * m["valence"] + 0.15 * occ["valence"], 3)
        m["arousal"] = round(0.8 * m["arousal"] + 0.2 * max(e["intensity"] for e in immediate), 3)

        # 3. 中期前置（L2）：张力与账户调防御初始权重
        preload = self._preload(unit, rid)

        # 4. 策略选择（L5）：叙事匹配 × 情绪 × 中期前置 × 大五（若启用）
        policy = self._select_policy(unit, relation, preload)

        # 5. 行为输出：只返回意图+边界+hard_rules 约束包（不产台词）
        behavior = dict(self.llm.generate_behavior(
            policy, unit, self.state.get("self", {}).get("hard_rules", []), emotion))
        behavior["policy_id"] = policy["id"]
        behavior["narrative_unit_id"] = unit["unit_id"]

        self.state.setdefault("policy_lib", {}).setdefault("runtime", {}).update(
            {"last_policy_id": policy["id"], "last_narrative_unit_id": unit["unit_id"]})
        self._save()
        return behavior

    def _preload(self, unit: dict, rid: str) -> dict:
        tensions = [t for t in self.state.get("midterm", {}).get("open_tensions", [])
                    if not t.get("resolved") and unit["threat_type"] in t.get("narrative_signature", "")]
        account = self._account(rid)
        return {"defense_boost": min(0.4, sum(t.get("intensity", 0) * 0.15 for t in tensions)),
                "overdrawn": account.get("overdrawn", False),
                "repair_incomplete": account.get("repair_stage") == "incomplete_reset"}

    def _select_policy(self, unit: dict, relation: dict, preload: dict) -> dict:
        band = self._safety_band(relation)
        cands = []
        for p in self.state.get("policy_lib", {}).get("policies", []):
            if p.get("status") == "rolled_back":
                continue
            an = p["applicable_narrative"]
            if an.get("threat_type") not in ("any", unit["threat_type"]):
                continue
            if an.get("safety_band") not in ("any", band):
                continue
            cands.append(p)
        if not cands:
            cands = [p for p in self.state.get("policy_lib", {}).get("policies", [])
                     if p.get("status") != "rolled_back"][:1] or [{"id": "none", "verbal_pattern": {"intent": "", "boundary": ""}, "intended_effect": {"type": "test_boundary"}}]

        defense_types = {"reduce_exposure", "counter_attack", "cut_off", "restore_equality"}
        scores = []
        for p in cands:
            s = p.get("weight", 0.5)
            if p.get("intended_effect", {}).get("type") in defense_types:
                s *= (1.0 + preload["defense_boost"])        # 未结账张力前置加载
                if preload["overdrawn"] or preload["repair_incomplete"]:
                    s *= 1.2                                  # 修复未完成：防御不按预期下降
            if p.get("intended_effect", {}).get("type") in GROWTH_EFFECTS:
                s *= (0.6 + 0.8 * relation.get("safety", 0.3))  # 成长策略随安全感解锁
            if self.state.get("big5", {}).get("enabled"):
                s *= self._big5_modifier(p)                    # L1 仅调权重（硬约束）
            m = self.state.get("emotion", {}).get("mood", {})
            if m.get("valence", 0) < -0.2 and p.get("intended_effect", {}).get("type") in GROWTH_EFFECTS:
                s *= 0.7                                      # 负心境压成长策略
            scores.append((p, max(0.01, s)))

        return self._softmax_sample(scores)

    def _big5_modifier(self, p: dict) -> float:
        cur = self.state["big5"]["current"]
        defense_types = {"reduce_exposure", "counter_attack", "cut_off"}
        if p["intended_effect"]["type"] in defense_types:
            return 1.0 + 0.3 * (cur["neuroticism"] - 0.5) - 0.2 * (cur["agreeableness"] - 0.5)
        return 1.0 + 0.2 * (cur["openness"] - 0.5) + 0.2 * (cur["agreeableness"] - 0.5)

    @staticmethod
    def _softmax_sample(scored: list[tuple[dict, float]]) -> dict:
        if not scored:
            raise RuntimeError("无可用策略")
        temps = [s / 0.5 for _, s in scored]
        mx = max(temps)
        exps = [math.exp(t - mx) for t in temps]
        total = sum(exps)
        r = random.random() * total
        acc = 0.0
        for (p, _), e in zip(scored, exps):
            acc += e
            if r <= acc:
                return p
        return scored[-1][0]

    # ---- 反馈与评估 -------------------------------------------------------

    def on_feedback(self, outcome: dict) -> Signal:
        pid = outcome.get("policy_id") or self.state.get("policy_lib", {}).get("runtime", {}).get("last_policy_id")
        policy = self._policy(pid) if pid else None
        signal = evaluate_outcome(outcome, policy)

        if signal == "uncertain":
            # 核心纪律：不确定 → 只记日志，不更新权重、不推进 trial
            self._audit("policy", "outcome_uncertain", before={"policy_id": pid},
                        after={"signal": "uncertain",
                               "reason": (outcome.get("feedback") or {}).get("kind")},
                        trigger=str(pid))
            self._pending_reflections.append(outcome)   # 仍进反思队列（归因多为 insufficient_data）
            return "uncertain"

        fb = outcome.get("feedback", {})
        rid = outcome.get("relation_id", "unknown")
        account = self._account(rid)
        before = {"balance": account.get("balance"), "policy_weight": policy.get("weight") if policy else None}

        # 关系账户
        delta = 0.5 if signal == "success" else (-1.0 if fb.get("fail_signal_hit") else -0.5)
        account["balance"] = round(account.get("balance", 0.0) + delta, 2)
        account["overdrawn"] = account["balance"] < -3.0
        account["last_transaction"] = new_id()
        if signal == "fail" and delta <= -1.0 and account.get("repair_stage") in ("none",):
            account["repair_stage"] = "amends_transparency" if account["balance"] > -3.0 else "none"

        # 策略权重与 trial 计数
        if policy:
            if signal == "success":
                policy["trial_success_count"] = policy.get("trial_success_count", 0) + 1
                policy["weight"] = round(min(1.0, policy.get("weight", 0.5) * 1.1), 3)
                policy["last_reinforced_tick"] = self.state.get("tick_count", 0)
                self.evaluate_trial(pid)
            else:
                self._rollback_policy(pid, trigger=str(outcome.get("event_id", "unknown")))
        # 高唤醒残留 → 中期层
        if signal == "fail" and fb.get("fail_signal_hit"):
            self.state.setdefault("emotion", {}).setdefault("arousal_residue", []).append({
                "source_event": outcome.get("event_id", new_id()),
                "kind": "humiliated" if "羞辱" in str(fb.get("note", "")) else "betrayed",
                "intensity": 0.7, "fed_into_midterm": True,
            })

        self._midterm_update(outcome, signal, rid)
        if self.state.get("big5", {}).get("enabled"):
            self._big5_update(signal, outcome.get("context_weight", 1.0))

        self._audit("relation", "feedback_applied", before=before,
                    after={"balance": account["balance"], "signal": signal,
                           "policy_weight": policy.get("weight") if policy else None},
                    trigger=str(outcome.get("event_id", "unknown")))
        self._pending_reflections.append(outcome)
        return signal

    def _rollback_policy(self, pid: str, trigger: str) -> None:
        p = self._policy(pid)
        if not p or p.get("status") == "rolled_back":
            return
        before = {"status": p["status"], "weight": p.get("weight")}
        p["status"] = "rolled_back"
        p["weight"] = 0.0
        p.setdefault("rollback_log", []).append(f"{now_iso()} trigger={trigger}")
        self._audit("policy", "rollback", before=before, after={"status": "rolled_back"},
                    trigger=trigger, rollback_token=new_id())

    def _midterm_update(self, outcome: dict, signal: Signal, rid: str) -> None:
        fb = outcome.get("feedback", {})
        if signal == "fail" and fb.get("relation_intact") is False:
            self.state["midterm"].setdefault("open_tensions", []).append({
                "tension_id": new_id(), "relation_id": rid,
                "narrative_signature": fb.get("narrative_signature", f"{rid}/relational_uncertainty"),
                "intensity": 0.7, "age_ticks": 0, "resolved": False,
            })
        if signal == "success":
            for t in self.state["midterm"].get("open_tensions", []):
                if not t.get("resolved") and t.get("relation_id") == rid:
                    t["intensity"] = round(max(0.0, t.get("intensity", 0) - 0.15), 2)
                    if t["intensity"] == 0.0:
                        t["resolved"] = True

    def _big5_update(self, signal: Signal, context_weight: float) -> None:
        b = self.state["big5"]
        feedback = 1.0 if signal == "success" else (-1.0 if signal == "fail" else 0.0)
        before = dict(b["current"])
        for dim in b["current"]:
            old = b["current"][dim]
            base = b["baseline"][dim]
            new = old + b["alpha"] * feedback * context_weight - b["lambda"] * (old - base)
            b["current"][dim] = round(min(base + b["max_drift"], max(base - b["max_drift"], new)), 4)
        self._audit("big5", "update", before=before, after=dict(b["current"]),
                    trigger=f"signal={signal}")

    # ---- 反思（双层） -----------------------------------------------------

    def reflect_event(self, outcomes: list[dict] | dict | None = None) -> None:
        """事件级反思（支持批量）。产物是参数微调数组，不是散文。"""
        if outcomes is None:
            outcomes = self._pending_reflections
        if isinstance(outcomes, dict):
            outcomes = [outcomes]
        processed = []
        for outcome in outcomes:
            processed.append(outcome)
            attribution = dict(self.llm.attribute(outcome))
            att = attribution.get("attribution", "insufficient_data")

            if att == "insufficient_data":
                q = attribution.get("question") or f"无法归因事件 {outcome.get('event_id')}"
                self.state.setdefault("blindspots", []).append(
                    {"id": new_id(), "question": q, "created": now_iso(), "status": "open"})
                self._audit("reflection", "blindspot_opened", before=None,
                            after={"question": q}, trigger=str(outcome.get("event_id", "unknown")))
                continue

            changes: list[dict] = list(attribution.get("param_changes", []))
            if att == "narrative_error":
                pattern = attribution.get("her_read_pattern", "unknown")
                nrs = self.state.setdefault("reflection", {}).setdefault(
                    "narrative_reflection_state", {})
                cnt = nrs.setdefault("falsification_counter", {})
                cnt[pattern] = cnt.get(pattern, 0) + 1
                for u in reversed(self.state.get("narrative", {}).get("recent_units", [])):
                    if u["unit_id"] == self.state.get("policy_lib", {}).get("runtime", {}).get("last_narrative_unit_id"):
                        u["falsified"] = True
                        break
            if att == "strategy_error":
                pid = outcome.get("policy_id") or self.state.get("policy_lib", {}).get("runtime", {}).get("last_policy_id")
                if pid:
                    changes.append({"target": f"policy:{pid}.weight", "op": "scale", "value": 0.9})

            self._apply_param_changes(changes)
            self.state.setdefault("reflection", {}).setdefault("event_reflections", []).append({
                "event_id": outcome.get("event_id", new_id()),
                "my_read": attribution.get("my_read", ""),
                "strategy_used": outcome.get("policy_id", ""),
                "attribution": att,
                "param_changes": changes,
            })
            self._audit("reflection", "event_reflection", before=None,
                        after={"attribution": att, "param_changes": changes},
                        trigger=str(outcome.get("event_id", "unknown")))
        self._pending_reflections = [o for o in self._pending_reflections if o not in processed]
        self._save()

    def _apply_param_changes(self, changes: list[dict]) -> None:
        for c in changes or []:
            target, op, val = c["target"], c["op"], c["value"]
            parts = target.split(":")
            try:
                if parts[0] == "policy" and len(parts) == 2:
                    pid, field = parts[1].split(".")
                    p = self._policy(pid)
                    if p:
                        p[field] = round(max(0.0, min(1.0, p.get(field, 0.5) * val if op == "scale"
                                                    else p.get(field, 0.5) + val if op == "add" else val)), 3)
                elif parts[0] == "ledger" and len(parts) == 2:
                    rid = parts[1].split(".")[0]
                    a = self._account(rid)
                    a["balance"] = round(a["balance"] + (val if op == "add" else val), 2)
                elif parts[0] == "midterm":
                    tid = parts[1].split(".")[0]
                    for t in self.state.get("midterm", {}).get("open_tensions", []):
                        if t.get("tension_id") == tid:
                            t["intensity"] = round(max(0.0, t.get("intensity", 0) + val), 2)
            except (KeyError, ValueError):
                continue  # 非法参数变更忽略并留审计可查

    def maybe_reflect_narrative(self) -> dict | None:
        """叙事级反思：触发式；允许修订中期假设；止步于 SELF 之前。"""
        nrs = self.state.get("reflection", {}).get("narrative_reflection_state", {})
        discipline = self.state.get("evolution_discipline", {})
        counters = nrs.get("falsification_counter", {})
        triggered = []
        if any(v >= discipline.get("falsify_threshold", 3) for v in counters.values()):
            triggered.append("her_read证伪≥阈值")
        for a in self.state.get("midterm", {}).get("relation_accounts", []):
            if abs(a.get("balance", 0.0)) >= discipline.get("large_move", 2.0):
                triggered.append("账户大额变动")
                break
        if nrs.get("stable_safety_ticks", 0) >= discipline.get("safe_period_ticks", 20):
            triggered.append("稳定安全期")
        if not triggered:
            return None

        revision = self.llm.propose_revision({
            "triggers": triggered,
            "falsification_counter": counters,
            "hypotheses": [{"id": h["hypothesis_id"], "content": h["content"],
                            "confidence": h.get("confidence", 0.5)}
                           for h in self.state.get("midterm", {}).get("meaning_hypotheses", [])],
        })
        if not revision or not self._boundary_check(revision):
            self._audit("narrative_revision", "rejected", before=revision, after=None,
                        trigger=";".join(triggered))
            return None

        for h in self.state.get("midterm", {}).get("meaning_hypotheses", []):
            if h["hypothesis_id"] == revision["hypothesis_id"]:
                token = new_id()
                prev = {"content": h["content"], "confidence": h.get("confidence", 0.5)}
                h.setdefault("revision_history", []).append({
                    "revision_id": new_id(),
                    "prev_content": prev["content"], "prev_confidence": prev["confidence"],
                    "new_content": revision["new_content"],
                    "new_confidence": revision.get("new_confidence", prev["confidence"] - 0.2),
                    "reason": revision.get("reason", ""), "timestamp": now_iso(),
                    "rollback_token": token,
                })
                h["content"] = revision["new_content"]
                h["confidence"] = round(revision.get("new_confidence", prev["confidence"] - 0.2), 3)
                h["status"] = revision.get("status", "downgraded")
                nrs["last_triggered"] = now_iso()
                nrs["stable_safety_ticks"] = 0
                self._save()
                self._audit("narrative_revision", "applied", before=prev,
                            after={"content": h["content"], "confidence": h["confidence"]},
                            trigger=";".join(triggered), rollback_token=token)
                return {"rollback_token": token, "hypothesis_id": h["hypothesis_id"]}
        return None

    def _boundary_check(self, proposal: dict) -> bool:
        """边界审核：是否触碰 SELF 锚点 / hard_rules。所有入 trial 的变更必经此闸。"""
        text = json.dumps(proposal, ensure_ascii=False)
        if any(k in text for k in ("\"identity\"", "\"values\"", "\"revision_gate\"")):
            return False
        if any(k in text for k in ("修改SELF", "replace_self", "override_hard_rules")):
            return False
        return True

    def rollback_hypothesis(self, token: str) -> bool:
        for h in self.state.get("midterm", {}).get("meaning_hypotheses", []):
            for r in h.get("revision_history", []):
                if r["rollback_token"] == token:
                    h["content"], h["confidence"] = r["prev_content"], r["prev_confidence"]
                    h["status"] = "rolled_back"
                    self._audit("narrative_revision", "rolled_back",
                                before={"content": r["new_content"]},
                                after={"content": r["prev_content"]}, trigger=token,
                                rollback_token=token)
                    self._save()
                    return True
        return False

    # ---- 策略生命周期 -----------------------------------------------------

    def propose_policy(self, reflection: dict) -> dict | None:
        cand = self.llm.propose_policy(reflection)
        if not cand or not self._boundary_check(cand):
            return None
        cand = self._normalize_candidate(cand, created_by="reflection")
        self.state["policy_lib"]["policies"].append(cand)
        self._audit("policy", "candidate_mounted", before=None,
                    after={"id": cand["id"]}, trigger="reflection")
        self._save()
        return cand

    def evaluate_trial(self, policy_id: str) -> str:
        p = self._policy(policy_id)
        if not p or p.get("status") != "trial":
            return p.get("status", "not_found") if p else "not_found"
        required = p.get("trial_required", 3)
        if self.state.get("evolution_discipline", {}).get("mode") == "relaxed":
            required = max(self.state.get("evolution_discipline", {}).get("trial_required_relaxed", 2), 2)
        if p.get("trial_success_count", 0) >= required:
            p["status"] = "stable"
            self._audit("policy", "promoted_stable", before={"status": "trial"},
                        after={"status": "stable", "trial_success_count": p["trial_success_count"]},
                        trigger=f"达 trial_required={required}")
            self._save()
        return p["status"]

    def self_write_policy(self, candidate: dict) -> dict | None:
        """自写插件只能产候选策略卡；边界审核是唯一入闸口。"""
        if not self._boundary_check(candidate):
            self._audit("policy", "self_write_rejected", before=candidate, after=None,
                        trigger="boundary_check_failed")
            return None
        cand = self._normalize_candidate(candidate, created_by="self_write")
        self.state["policy_lib"]["policies"].append(cand)
        self._audit("policy", "self_write_mounted", before=None,
                    after={"id": cand["id"], "created_by": "self_write"},
                    trigger="self_write_policy")
        self._save()
        return cand

    @staticmethod
    def _normalize_candidate(cand: dict, created_by: str) -> dict:
        return {
            "id": cand.get("id") or f"P{new_id()[:8]}",
            "name": cand.get("name", "unnamed"),
            "applicable_narrative": cand.get("applicable_narrative", {
                "threat_type": "any", "her_read_pattern": "", "safety_band": "any"}),
            "verbal_pattern": cand.get("verbal_pattern", {"intent": "", "boundary": ""}),
            "nonverbal_pattern": cand.get("nonverbal_pattern", ""),
            "action_compensation": cand.get("action_compensation", "none"),
            "intended_effect": cand.get("intended_effect", {"type": "test_boundary", "metric": ""}),
            "success_signal": cand.get("success_signal", ""),
            "fail_signal": cand.get("fail_signal", ""),
            "status": "candidate",
            "weight": 0.3,
            "trial_required": cand.get("trial_required", 3),
            "trial_success_count": 0,
            "last_reinforced_tick": None,
            "created_by": created_by,
            "rollback_log": [],
        }

    # ---- tick 循环 ---------------------------------------------------------

    def tick(self) -> None:
        self.state["tick_count"] = self.state.get("tick_count", 0) + 1
        # 情绪衰减：即时 → 心境；高唤醒残留不归零
        for e in self.state.get("emotion", {}).get("immediate", []):
            e["intensity"] = round(e.get("intensity", 0) * (1 - e.get("decay_rate", 0.25)), 3)
            e["rounds_left"] = e.get("rounds_left", 2) - 1
        self.state["emotion"]["immediate"] = [
            e for e in self.state["emotion"]["immediate"]
            if e.get("rounds_left", 0) > 0 and e.get("intensity", 0) > 0.05]
        m = self.state["emotion"]["mood"]
        m["valence"] = round(0.95 * m.get("valence", 0.0), 3)
        m["arousal"] = round(0.9 * m.get("arousal", 0.4) + 0.1 * 0.4, 3)

        # 大五回正（若启用）
        if self.state.get("big5", {}).get("enabled"):
            b = self.state["big5"]
            for dim in b["current"]:
                b["current"][dim] = round(b["current"][dim] - b["lambda"] *
                                          (b["current"][dim] - b["baseline"][dim]), 4)

        # 未强化 stable 策略权重衰减
        for p in self.state.get("policy_lib", {}).get("policies", []):
            if p.get("status") == "stable":
                last = p.get("last_reinforced_tick")
                if last is None or self.state["tick_count"] - last >= 10:
                    p["weight"] = round(max(0.2, p.get("weight", 0.5) * 0.97), 3)

        # 张力老化 + 账户利息式回归
        for t in self.state.get("midterm", {}).get("open_tensions", []):
            if not t.get("resolved"):
                t["age_ticks"] = t.get("age_ticks", 0) + 1
                t["intensity"] = round(max(0.0, t.get("intensity", 0) - 0.02), 3)
                if t["intensity"] == 0.0:
                    t["resolved"] = True
        for a in self.state.get("midterm", {}).get("relation_accounts", []):
            if a.get("balance", 0) > 0:
                a["balance"] = round(a["balance"] * 0.995, 3)
            elif not a.get("overdrawn"):
                a["balance"] = round(min(0.0, a["balance"] + 0.01), 3)

        # 安全期计数
        nrs = self.state.setdefault("reflection", {}).setdefault("narrative_reflection_state", {})
        negative = m.get("valence", 0) < -0.1 or any(
            not t.get("resolved") for t in self.state.get("midterm", {}).get("open_tensions", []))
        nrs["stable_safety_ticks"] = 0 if negative else nrs.get("stable_safety_ticks", 0) + 1

        self._save()

    # ---- 注入与审计 -------------------------------------------------------

    def build_prompt_context(self) -> dict:
        """只注入必要状态：白名单键、约 5% 状态切片。不注入全量日志。"""
        rid = self.state.get("policy_lib", {}).get("runtime", {}).get("_last_relation", "unknown")
        relation = self._relation(rid)
        band = self._safety_band(relation)
        active = [p for p in self.state.get("policy_lib", {}).get("policies", [])
                  if p.get("status") in ("trial", "stable")][:3]
        last = (self.state.get("reflection", {}).get("event_reflections") or [None])[-1]
        bs = next((b for b in self.state.get("blindspots", []) if b.get("status") == "open"), None)
        return {
            "hard_rules": self.state.get("self", {}).get("hard_rules", []),
            "relation_state": {
                "relation_id": relation["relation_id"],
                "trust": relation.get("trust", 0.3), "safety": relation.get("safety", 0.3),
                "autonomy_threat": relation.get("autonomy_threat", 0.3),
                "exposure_fear": relation.get("exposure_fear", 0.5),
            },
            "current_policies": [
                {"id": p["id"], "intent": p["verbal_pattern"]["intent"],
                 "boundary": p["verbal_pattern"]["boundary"], "status": p["status"]}
                for p in active],
            "last_reflection": (
                f"归因={last['attribution']}；参数微调 {len(last.get('param_changes', []))} 项"
                if last else None),
            "current_emotion": {
                "immediate": [f"{e['kind']} {e['intensity']}"
                              for e in self.state.get("emotion", {}).get("immediate", [])],
                "mood": self.state.get("emotion", {}).get("mood", {}),
                "safety_band": band,
            },
            "blindspot": bs["question"] if bs else None,
        }

    def get_state(self) -> dict:
        """可审计全量状态（只读副本）。"""
        return json.loads(json.dumps(self.state, ensure_ascii=False))


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="persona-evolution 引擎自检")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    args = ap.parse_args()
    eng = PersonaEvo(args.root)
    print("initialized:", eng.state.get("policy_lib", {}).get("initialized"))
    print("policies:", len(eng.state.get("policy_lib", {}).get("policies", [])))
    print("self locked:", eng.state.get("self", {}).get("revision_gate", {}).get("locked"))
    # 冒烟：评估规则三分类（默认出口是 uncertain）
    print(evaluate_outcome({"feedback": {"kind": "LIKED", "relation_intact": True,
                                         "boundary_acknowledged": True, "anxiety_delta": -0.1}}))
    print(evaluate_outcome({"feedback": {"kind": "BOUNDARY_EVENT"}}))
    print(evaluate_outcome({"feedback": {"kind": "DISLIKE", "relation_intact": True,
                                         "boundary_acknowledged": True, "anxiety_delta": 0.0}}))
