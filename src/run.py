"""run.py — persona-evolution 的会话操作 CLI（宿主 agent 即模型能力提供方）。

宿主 agent（DSH 会话 / OpenAI 管道 / LangChain chain）就是 LLM 能力提供方：
narrate / attribute 的模型侧内容由调用方以 prefill 字段随命令传入；引擎负责
全部确定性逻辑（评估、计数、回滚、衰减、审计）。

用法（cwd = 插件根目录，或 --root 指定）：
  python src/run.py state
  python src/run.py context [--relation R088]
  python src/run.py interact  --event-file event.json
  python src/run.py feedback  --outcome-file outcome.json
  python src/run.py reflect   [--attribution-file attr.json]
  python src/run.py narrative-reflect [--revision-file revision.json]
  python src/run.py tick
  python src/run.py trial --policy P005
  python src/run.py self-write --candidate-file candidate.json
  python src/run.py rollback-hypothesis --token <rollback_token>
  python src/run.py validate --before a.jsonl --after b.jsonl --dimension policy:P005

所有输出 JSON。--*-file 优先（规避 shell 引号剥蚀；utf-8-sig 兼容 BOM）。
禁止：直接编辑 persona.json 的 SELF 段（引擎守卫会拒绝写回）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from persona_evo import PersonaEvo, LLMHooks, now_iso, new_id  # noqa: E402


class AgentPrefillLLM(LLMHooks):
    """把宿主 agent 的 prefill 接进引擎挂钩。"""

    def narrate(self, event, relation, tension_sig):
        pre = event.get("narrative_prefill") or {}
        if pre.get("her_read"):
            return {"trigger_scene": event.get("semantic", ""),
                    "her_read": pre["her_read"],
                    "threat_type": pre.get("threat_type", "none"),
                    "active_hypothesis_ids": pre.get("active_hypothesis_ids", [])}
        return {"trigger_scene": event.get("semantic", ""),
                "her_read": "(prefill 缺失) 信息不足，无法生成第一人称解读",
                "threat_type": "none", "active_hypothesis_ids": []}

    def attribute(self, outcome):
        pre = outcome.get("attribution_prefill") or {}
        if pre.get("attribution"):
            return {"attribution": pre["attribution"],
                    "my_read": pre.get("my_read", ""),
                    "her_read_pattern": pre.get("her_read_pattern", ""),
                    "question": pre.get("question"),
                    "param_changes": pre.get("param_changes", [])}
        return {"attribution": "insufficient_data", "param_changes": []}

    def propose_revision(self, trigger_ctx):
        return trigger_ctx.get("revision_prefill")

    def propose_policy(self, reflection):
        return reflection.get("candidate_prefill")


def _load_json_arg(value: str | None, file_value: str | None) -> dict:
    if file_value:
        return json.loads(Path(file_value).read_text(encoding="utf-8-sig"))
    return json.loads(value)


def main() -> None:
    ap = argparse.ArgumentParser(description="persona-evolution CLI")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("state")
    p_ctx = sub.add_parser("context"); p_ctx.add_argument("--relation", default=None)
    p_int = sub.add_parser("interact")
    p_int.add_argument("--event", required=True)
    p_int.add_argument("--event-file", dest="event_file")
    p_fb = sub.add_parser("feedback")
    p_fb.add_argument("--outcome", required=True)
    p_fb.add_argument("--outcome-file", dest="outcome_file")
    p_rf = sub.add_parser("reflect")
    p_rf.add_argument("--attribution-json", default=None)
    p_rf.add_argument("--attribution-file", dest="attribution_file", default=None)
    p_nr = sub.add_parser("narrative-reflect")
    p_nr.add_argument("--revision-json", default=None)
    p_nr.add_argument("--revision-file", dest="revision_file", default=None)
    sub.add_parser("tick")
    p_tr = sub.add_parser("trial"); p_tr.add_argument("--policy", required=True)
    p_sw = sub.add_parser("self-write")
    p_sw.add_argument("--candidate", required=True)
    p_sw.add_argument("--candidate-file", dest="candidate_file", default=None)
    p_rb = sub.add_parser("rollback-hypothesis"); p_rb.add_argument("--token", required=True)
    p_vd = sub.add_parser("validate")
    p_vd.add_argument("--before", required=True)
    p_vd.add_argument("--after", required=True)
    p_vd.add_argument("--dimension", required=True)
    p_vd.add_argument("--discipline", default="strict", choices=["strict", "relaxed"])

    args = ap.parse_args()
    eng = PersonaEvo(args.root, llm=AgentPrefillLLM())

    if args.cmd == "state":
        out = eng.get_state()
        print(json.dumps({"summary": {
            "policies": len(out.get("policy_lib", {}).get("policies", [])),
            "self_locked": out.get("self", {}).get("revision_gate", {}).get("locked"),
            "self_slots_unfilled": out.get("self", {}).get("identity", "").startswith("{"),
            "tick_count": out.get("tick_count", 0),
            "open_tensions": len([t for t in out.get("midterm", {}).get("open_tensions", [])
                                  if not t.get("resolved")]),
            "blindspots": len([b for b in out.get("blindspots", []) if b.get("status") == "open"]),
        }, "full_state": out}, ensure_ascii=False, indent=2))
    elif args.cmd == "context":
        if args.relation:
            eng.state.setdefault("policy_lib", {}).setdefault("runtime", {})["_last_relation"] = args.relation
        print(json.dumps(eng.build_prompt_context(), ensure_ascii=False, indent=2))
    elif args.cmd == "interact":
        event = _load_json_arg(args.event, args.event_file)
        print(json.dumps(eng.on_interaction(event), ensure_ascii=False, indent=2))
    elif args.cmd == "feedback":
        outcome = _load_json_arg(args.outcome, args.outcome_file)
        signal = eng.on_feedback(outcome)
        print(json.dumps({"signal": signal,
                          "note": "uncertain → 未更新任何参数，未推进 trial"}, ensure_ascii=False))
    elif args.cmd == "reflect":
        kwargs = {}
        if args.attribution_json or args.attribution_file:
            pre = _load_json_arg(args.attribution_json, args.attribution_file)
            kwargs = {"outcomes": [{"event_id": pre.get("event_id", "unknown"),
                                    "relation_id": pre.get("relation_id", "unknown"),
                                    "policy_id": pre.get("policy_id"),
                                    "attribution_prefill": pre}]}
        eng.reflect_event(**kwargs)
        print(json.dumps({"reflected": True,
                          "pending": len(eng._pending_reflections)}, ensure_ascii=False))
    elif args.cmd == "narrative-reflect":
        trig = {}
        if args.revision_json or args.revision_file:
            trig["revision_prefill"] = _load_json_arg(args.revision_json, args.revision_file)
        revision = trig.get("revision_prefill")
        applied = None
        if revision:
            # 走与引擎一致的边界审核 + 可回滚写入（与 maybe_reflect_narrative 同闸）
            import persona_evo as pe
            trigger_ctx = {
                "triggers": ["agent-invoked"],
                "falsification_counter": eng.state.get("reflection", {})
                    .get("narrative_reflection_state", {}).get("falsification_counter", {}),
                "hypotheses": [{"id": h["hypothesis_id"], "content": h["content"],
                                "confidence": h.get("confidence", 0.5)}
                               for h in eng.state.get("midterm", {}).get("meaning_hypotheses", [])],
                "revision_prefill": revision,
            }
            cand = eng.llm.propose_revision(trigger_ctx)
            if cand and eng._boundary_check(cand):
                for h in eng.state.get("midterm", {}).get("meaning_hypotheses", []):
                    if h["hypothesis_id"] == cand["hypothesis_id"]:
                        token = new_id()
                        prev = {"content": h["content"], "confidence": h.get("confidence", 0.5)}
                        h.setdefault("revision_history", []).append({
                            "revision_id": new_id(),
                            "prev_content": prev["content"],
                            "prev_confidence": prev["confidence"],
                            "new_content": cand["new_content"],
                            "new_confidence": cand.get("new_confidence", prev["confidence"] - 0.2),
                            "reason": cand.get("reason", ""),
                            "timestamp": now_iso(), "rollback_token": token,
                        })
                        h["content"] = cand["new_content"]
                        h["confidence"] = round(cand.get("new_confidence", prev["confidence"] - 0.2), 3)
                        h["status"] = cand.get("status", "downgraded")
                        applied = {"rollback_token": token, "hypothesis_id": h["hypothesis_id"]}
                        eng._save()
                        eng._audit("narrative_revision", "applied", before=prev,
                                   after={"content": h["content"], "confidence": h["confidence"]},
                                   trigger="agent-invoked", rollback_token=token)
                        break
        print(json.dumps({"revision_applied": applied,
                          "note": "未经边界审核或未命中假设 id → None"},
                         ensure_ascii=False))
    elif args.cmd == "tick":
        eng.tick()
        print(json.dumps({"tick_count": eng.state.get("tick_count", 0)}, ensure_ascii=False))
    elif args.cmd == "trial":
        print(json.dumps({"policy": args.policy,
                          "status": eng.evaluate_trial(args.policy)}, ensure_ascii=False))
    elif args.cmd == "self-write":
        cand = _load_json_arg(args.candidate, args.candidate_file)
        result = eng.self_write_policy(cand)
        print(json.dumps({"accepted": result is not None, "policy": result,
                          "note": "拒绝原因见审计日志 self_write_rejected"},
                         ensure_ascii=False, indent=2))
    elif args.cmd == "rollback-hypothesis":
        print(json.dumps({"rolled_back": eng.rollback_hypothesis(args.token)}, ensure_ascii=False))
    elif args.cmd == "validate":
        from validate_evolution import load_log, validate_dimension
        result = validate_dimension(load_log(args.before), load_log(args.after),
                                    args.dimension, args.discipline)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
