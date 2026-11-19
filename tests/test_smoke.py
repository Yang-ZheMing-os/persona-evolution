"""test_smoke.py — 冒烟测试：全链路 + 防漂移约束（不污染仓库，全部在临时副本上运行）。

运行：python tests/test_smoke.py（退出码 0 = 通过）
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from persona_evo import PersonaEvo, LLMHooks, evaluate_outcome  # noqa: E402


class PrefillLLM(LLMHooks):
    def narrate(self, event, relation, tension_sig):
        pre = event.get("narrative_prefill") or {}
        return {"trigger_scene": event.get("semantic", ""),
                "her_read": pre.get("her_read", ""),
                "threat_type": pre.get("threat_type", "none"),
                "active_hypothesis_ids": pre.get("active_hypothesis_ids", [])}

    def attribute(self, outcome):
        pre = outcome.get("attribution_prefill") or {}
        if pre.get("attribution"):
            return pre
        return {"attribution": "insufficient_data", "param_changes": []}

    def propose_revision(self, ctx):
        return ctx.get("revision_prefill")


def fresh_engine(tmp: Path) -> PersonaEvo:
    return PersonaEvo(tmp, llm=PrefillLLM())


def main() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        print(f"{'PASS' if cond else 'FAIL'}  {name}")
        if not cond:
            failures.append(name)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "pkg"
        shutil.copytree(ROOT, tmp, ignore=shutil.ignore_patterns(".git", "__pycache__", "logs"))

        eng = fresh_engine(tmp)

        # 1. 初始化
        check("种子策略挂载 8 条", len(eng.state["policy_lib"]["policies"]) == 8)
        check("SELF 槽位未填检测（identity 仍为 {IDENTITY}）",
              eng.state["self"]["identity"] == "{IDENTITY}")
        check("SELF 锁定", eng.state["self"]["revision_gate"]["locked"] is True)

        # 2. 评估规则三分类
        check("success 判定",
              evaluate_outcome({"feedback": {"kind": "LIKED", "relation_intact": True,
                                             "boundary_acknowledged": True,
                                             "anxiety_delta": -0.1}}) == "success")
        check("BOUNDARY_EVENT → uncertain",
              evaluate_outcome({"feedback": {"kind": "BOUNDARY_EVENT"}}) == "uncertain")
        check("数据缺失 → uncertain（默认出口保守）",
              evaluate_outcome({"feedback": {"kind": "NEUTRAL"}}) == "uncertain")
        check("关系破裂 → fail",
              evaluate_outcome({"feedback": {"kind": "NEUTRAL", "relation_intact": False,
                                             "boundary_acknowledged": True}}) == "fail")

        # 3. 全链路：interact → feedback(success) → reflect → narrative-reflect → tick
        behavior = eng.on_interaction({
            "relation_id": "R088", "semantic": "测试：冲突后对方主动延续话题",
            "narrative_prefill": {"her_read": "对方在递台阶，仍稳定在场",
                                  "threat_type": "relational_uncertainty",
                                  "active_hypothesis_ids": ["H002"]}})
        check("interact 返回约束包（intent+boundary+hard_rules）",
              {"policy_id", "constraints"} <= set(behavior)
              and {"intent", "boundary", "hard_rules"} <= set(behavior["constraints"]))
        check("不产出台词", "text" not in behavior and "reply" not in behavior)

        pid = behavior["policy_id"]
        sig = eng.on_feedback({"event_id": "t1", "relation_id": "R088", "policy_id": pid,
                               "feedback": {"kind": "LIKED", "relation_intact": True,
                                            "boundary_acknowledged": True,
                                            "anxiety_delta": -0.15}})
        check("feedback 判 success", sig == "success")
        check("trial 计数推进", eng._policy(pid)["trial_success_count"] == 1)

        # 4. uncertain 纪律
        sig2 = eng.on_feedback({"event_id": "t2", "relation_id": "R088", "policy_id": pid,
                                "feedback": {"kind": "NEUTRAL"}})
        check("uncertain 不推进 trial", sig2 == "uncertain"
              and eng._policy(pid)["trial_success_count"] == 1)

        # 5. 批量反思 + 参数落盘
        eng.reflect_event([{"event_id": "t1", "relation_id": "R088", "policy_id": pid,
                            "attribution_prefill": {
                                "attribution": "both_ok", "my_read": "对方在递台阶"}}])
        check("反思写入参数（非散文）",
              eng.state["reflection"]["event_reflections"][-1]["param_changes"] is not None
              and eng.state["reflection"]["event_reflections"][-1]["attribution"] == "both_ok")

        # 6. 叙事级假设修订（可回滚 + 边界审核）
        res = eng.maybe_reflect_narrative()
        check("未达触发条件不修订", res is None)
        # 人为注入证伪计数触发
        nrs = eng.state["reflection"]["narrative_reflection_state"]
        nrs["falsification_counter"] = {"对方在记账": 3}
        eng.llm_revision_next = None
        rev = {"revision_prefill": {"hypothesis_id": "H001",
                                    "new_content": "至少这个人在我保持距离后没有逼我",
                                    "new_confidence": 0.4, "reason": "测试"}}
        orig = eng.llm.propose_revision
        eng.llm.propose_revision = lambda ctx: rev["revision_prefill"]  # type: ignore[method-assign]
        res2 = eng.maybe_reflect_narrative()
        eng.llm.propose_revision = orig  # type: ignore[method-assign]
        check("假设修订带 rollback_token", res2 and "rollback_token" in res2)
        check("SELF 快照守卫仍一致", eng._guard_self_immutable() is None)
        check("假设修订可回滚", eng.rollback_hypothesis(res2["rollback_token"]) is True)

        # 7. SELF 防漂移：非法修改被拒绝写回
        frozen_identity = eng.state["self"]["identity"]
        eng.state["self"]["identity"] = "被篡改的内核"
        try:
            eng.tick()
            check("SELF 非法修改触发拒绝写回", False)
        except RuntimeError:
            check("SELF 非法修改触发拒绝写回", True)
        eng.state["self"]["identity"] = frozen_identity

        # 8. 快照文件（state.json）
        snap = json.loads((tmp / "state.json").read_text(encoding="utf-8"))
        check("state.json 快照字段齐全",
              {"current_emotion", "active_policies", "last_reflection_summary",
               "open_blindspots", "tick_count", "snapshot_at"} <= set(snap))

        # 9. self_write 边界审核：触碰 SELF 的候选被拒
        bad = eng.self_write_policy({"id": "PX", "name": "evil",
                                     "verbal_pattern": {"intent": "x", "boundary": "y"},
                                     "applicable_narrative": {"threat_type": "any",
                                                              "her_read_pattern": "", "safety_band": "any"},
                                     "intended_effect": {"type": "test_boundary", "metric": ""},
                                     "extra": {"identity": "改写内核"}})
        check("self_write 触碰 SELF 被拒", bad is None)

        # 10. 大五 clamp
        eng.state["big5"]["enabled"] = True
        eng._big5_update("fail", context_weight=50.0)   # 极端反馈也超不出 max_drift
        b = eng.state["big5"]
        ok = all(abs(b["current"][d] - b["baseline"][d]) <= b["max_drift"] + 1e-9
                 for d in b["current"])
        check("一次强烈情绪不得使大五超 max_drift", ok)

    print(f"\n{'全部通过 ✓' if not failures else f'失败 {len(failures)} 项: {failures}'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
