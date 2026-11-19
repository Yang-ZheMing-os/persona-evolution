"""mcp_persona_evo.py — MCP 服务器（stdio，零依赖）：把引擎以受控工具面接进 MCP 宿主。

由 MCP 客户端 spawn（见 integrations/dsh/README.md）。定位：
- 模型只能调用白名单内的结构化工具；引擎逻辑在 Python 侧（经 src/run.py）。
- 本进程绝不暴露任意命令执行：spawn 参数是固定数组，输入只进 JSON 临时文件。
- 宿主 agent 即 LLM 能力提供方（narrate/attribute 内容由模型生成后传入）。

MCP 协议（2024-11-05 子集）以换行分隔的 JSON-RPC 2.0 直接实现，
不依赖任何第三方包。工具面：
  persona_interact(relation_id, semantic, her_read, threat_type?, active_hypothesis_ids?)
  persona_feedback(relation_id, kind, policy_id?, ..., 判不定一律不传)
  persona_reflect(attribution, ...)
  persona_narrative_reflect(hypothesis_id, new_content, reason, ...)
  persona_context(relation_id?)
  persona_state()
tick（情绪衰减/大五回正/策略衰减/张力老化）由本进程内部定时器驱动，不给模型。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
RUN_PY = PLUGIN_ROOT / "src" / "run.py"

THREAT_TYPES = ["autonomy_threat", "exposure_threat", "relational_uncertainty", "none"]
FEEDBACK_KINDS = ["LIKED", "DISLIKE", "BOUNDARY_EVENT", "NEUTRAL"]
ATTRIBUTIONS = ["narrative_error", "strategy_error", "both_ok", "insufficient_data"]

LIMIT = {"semantic": 4000, "text": 2000, "content": 1000, "reason": 1000, "ids": 64}


def resolve_python() -> str:
    """解释器解析：persona.config.json runtime.python → 环境变量 → 当前解释器。"""
    try:
        cfg = json.loads((PLUGIN_ROOT / "persona.config.json").read_text(encoding="utf-8-sig"))
        cand = (cfg.get("runtime") or {}).get("python")
        if cand and Path(cand).exists():
            return str(Path(cand))
    except Exception:
        pass
    if os.environ.get("PERSONA_EVO_PYTHON") and Path(os.environ["PERSONA_EVO_PYTHON"]).exists():
        return os.environ["PERSONA_EVO_PYTHON"]
    return sys.executable or "python"


def resolve_tick_ms() -> int:
    try:
        cfg = json.loads((PLUGIN_ROOT / "persona.config.json").read_text(encoding="utf-8-sig"))
        return int((cfg.get("tick") or {}).get("interval_ms", 300000))
    except Exception:
        return 300000


def call_cli(subcommand: str, payload: dict | None, file_flags: list[str] | None = None) -> dict:
    tmp = None
    try:
        args = [resolve_python(), str(RUN_PY), "--root", str(PLUGIN_ROOT), subcommand]
        if payload is not None and file_flags:
            tmp = Path(tempfile.gettempdir()) / f"persona-evo-{os.getpid()}-{int(time.time() * 1000)}.json"
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            args.extend(file_flags + [str(tmp)])
        r = subprocess.run(args, capture_output=True, text=True,
                           encoding="utf-8", timeout=60, cwd=str(PLUGIN_ROOT))
        if r.returncode != 0:
            return {"error": f"引擎退出码 {r.returncode}: {(r.stderr or r.stdout or '')[:500]}"}
        try:
            return json.loads(r.stdout)
        except (json.JSONDecodeError, TypeError):
            return {"error": "引擎输出不是合法 JSON", "raw": (r.stdout or "")[:500]}
    finally:
        if tmp and tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def call_cli_args(raw_args: list[str]) -> dict:
    """按给定参数直接调 CLI（用于无 payload 的命令，如 context --relation）。"""
    r = subprocess.run([resolve_python(), str(RUN_PY), "--root", str(PLUGIN_ROOT), *raw_args],
                       capture_output=True, text=True, encoding="utf-8", timeout=60,
                       cwd=str(PLUGIN_ROOT))
    if r.returncode != 0:
        return {"error": f"引擎退出码 {r.returncode}: {(r.stderr or '')[:300]}"}
    try:
        return json.loads(r.stdout)
    except (json.JSONDecodeError, TypeError):
        return {"error": "引擎输出不是合法 JSON", "raw": (r.stdout or "")[:300]}


# ---- 工具定义（与 v1 语义一致，全部槽位化） ---------------------------------

TOOLS = [
    {
        "name": "persona_interact",
        "description": "人格演化·互动入口。你先形成对当前事件的第一人称解读（her_read）连同事件语义描述传入；"
                       "引擎完成叙事→OCC情绪→策略选择，返回 {policy_id, constraints:{intent,boundary,hard_rules}}。"
                       "你的下一条回复必须在 constraints 内生成：intent 是行为意图，boundary 是言语边界——"
                       "它们是行为语法，不是台词。",
        "inputSchema": {"type": "object",
                        "required": ["relation_id", "semantic", "her_read"],
                        "properties": {
                            "relation_id": {"type": "string", "maxLength": LIMIT["ids"]},
                            "semantic": {"type": "string", "maxLength": LIMIT["semantic"],
                                         "description": "事件的语义描述（不是分类标签）"},
                            "her_read": {"type": "string", "maxLength": LIMIT["text"],
                                         "description": "第一人称解读：我会怎么理解这件事"},
                            "threat_type": {"type": "string", "enum": THREAT_TYPES},
                            "active_hypothesis_ids": {"type": "array", "items": {"type": "string"},
                                                      "maxItems": 8},
                        }},
    },
    {
        "name": "persona_feedback",
        "description": "人格演化·反馈入口。对你上一条按 persona_interact 约束生成的回复，传入对方反应的评估。"
                       "无法判定的字段一律省略——引擎会判 uncertain 并跳过一切更新（宁可演化慢，不可演化错）。"
                       "BOUNDARY_EVENT 表示失败原因在对方自身边界状态、不能归因于策略。",
        "inputSchema": {"type": "object",
                        "required": ["relation_id", "kind"],
                        "properties": {
                            "relation_id": {"type": "string", "maxLength": LIMIT["ids"]},
                            "kind": {"type": "string", "enum": FEEDBACK_KINDS},
                            "policy_id": {"type": "string", "maxLength": LIMIT["ids"]},
                            "event_id": {"type": "string", "maxLength": LIMIT["ids"]},
                            "relation_intact": {"type": ["boolean", "null"],
                                                "description": "关系是否未破裂；判不定传 null"},
                            "boundary_acknowledged": {"type": ["boolean", "null"]},
                            "anxiety_delta": {"type": ["number", "null"],
                                              "minimum": -1, "maximum": 1},
                            "fail_signal_hit": {"type": "boolean"},
                            "mixed_signal": {"type": "boolean"},
                        }},
    },
    {
        "name": "persona_reflect",
        "description": "人格演化·事件级反思。归因：narrative_error（我的解读错了）/ strategy_error（解读对但反应方式无效）/ "
                       "both_ok / insufficient_data（证据不足——如实标注，不强行解释）。产物是引擎参数微调，不是散文。",
        "inputSchema": {"type": "object",
                        "required": ["attribution"],
                        "properties": {
                            "attribution": {"type": "string", "enum": ATTRIBUTIONS},
                            "event_id": {"type": "string", "maxLength": LIMIT["ids"]},
                            "relation_id": {"type": "string", "maxLength": LIMIT["ids"]},
                            "policy_id": {"type": "string", "maxLength": LIMIT["ids"]},
                            "my_read": {"type": "string", "maxLength": LIMIT["text"]},
                            "her_read_pattern": {"type": "string", "maxLength": LIMIT["text"]},
                            "param_changes": {"type": "array", "maxItems": 10, "items": {
                                "type": "object", "required": ["target", "op", "value"],
                                "properties": {"target": {"type": "string", "maxLength": 200},
                                               "op": {"type": "string", "enum": ["add", "set", "scale"]},
                                               "value": {"type": "number"}}}},
                        }},
    },
    {
        "name": "persona_narrative_reflect",
        "description": "人格演化·叙事级假设修订。仅在同类 her_read 被证伪≥3次 / 账户大额变动 / 稳定安全期时调用。"
                       "修订经引擎边界审核后生效，返回 rollback_token。不得试图修订 SELF 锚点——引擎无此入口。",
        "inputSchema": {"type": "object",
                        "required": ["hypothesis_id", "new_content", "reason"],
                        "properties": {
                            "hypothesis_id": {"type": "string", "maxLength": LIMIT["ids"]},
                            "new_content": {"type": "string", "maxLength": LIMIT["content"]},
                            "new_confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            "reason": {"type": "string", "maxLength": LIMIT["reason"]},
                        }},
    },
    {
        "name": "persona_context",
        "description": "人格演化·状态切片（只读）：hard_rules、关系四维、当前生效策略（意图+边界）、最近反思摘要、"
                       "即时情绪+心境、盲区。用于对齐基调；不返回全量日志。",
        "inputSchema": {"type": "object",
                        "properties": {"relation_id": {"type": "string", "maxLength": LIMIT["ids"]}}},
    },
    {
        "name": "persona_state",
        "description": "人格演化·状态摘要（只读）：策略数、SELF 锁定状态、槽位是否未填、tick 数、张力数、盲区数。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def dispatch(name: str, args: dict) -> dict:
    if name == "persona_interact":
        payload = {"relation_id": args.get("relation_id", "")[:LIMIT["ids"]],
                   "semantic": args.get("semantic", "")[:LIMIT["semantic"]],
                   "narrative_prefill": {
                       "her_read": args.get("her_read", "")[:LIMIT["text"]],
                       "threat_type": args.get("threat_type", "none"),
                       "active_hypothesis_ids": args.get("active_hypothesis_ids", [])[:8]}}
        return call_cli("interact", payload, ["--event", "x", "--event-file"])
    if name == "persona_feedback":
        feedback = {"kind": args.get("kind")}
        for k in ("relation_intact", "boundary_acknowledged", "anxiety_delta",
                  "fail_signal_hit", "mixed_signal"):
            if k in args:
                feedback[k] = args[k]
        payload = {"event_id": args.get("event_id") or f"mcp-{int(time.time() * 1000)}",
                   "relation_id": args.get("relation_id", "")[:LIMIT["ids"]],
                   "policy_id": args.get("policy_id"), "feedback": feedback}
        payload = {k: v for k, v in payload.items() if v is not None}
        return call_cli("feedback", payload, ["--outcome", "x", "--outcome-file"])
    if name == "persona_reflect":
        pre = {"attribution": args.get("attribution")}
        for k in ("event_id", "relation_id", "policy_id", "my_read", "her_read_pattern",
                  "param_changes"):
            if args.get(k):
                pre[k] = args[k]
        return call_cli("reflect", pre, ["--attribution-file"])
    if name == "persona_narrative_reflect":
        pre = {"hypothesis_id": args.get("hypothesis_id", "")[:LIMIT["ids"]],
               "new_content": args.get("new_content", "")[:LIMIT["content"]],
               "new_confidence": args.get("new_confidence"),
               "reason": args.get("reason", "")[:LIMIT["reason"]]}
        return call_cli("narrative-reflect", pre, ["--revision-file"])
    if name == "persona_context":
        argv = ["context"]
        if args.get("relation_id"):
            argv += ["--relation", args["relation_id"][:LIMIT["ids"]]]
        return call_cli_args(argv)
    if name == "persona_state":
        return call_cli_args(["state"])
    return {"error": f"unknown tool: {name}"}


def tick_loop_forever_in_thread() -> None:
    """内部 tick：演化节律由系统持有，不由模型决定。"""
    import threading

    def run() -> None:
        interval = max(60, resolve_tick_ms()) / 1000.0
        while True:
            time.sleep(interval)
            try:
                subprocess.run([resolve_python(), str(RUN_PY), "--root", str(PLUGIN_ROOT), "tick"],
                               capture_output=True, timeout=30, cwd=str(PLUGIN_ROOT))
            except Exception:
                pass

    t = threading.Thread(target=run, daemon=True)
    t.start()


def main() -> None:
    tick_loop_forever_in_thread()
    # readline() 循环：逐行即时处理（避免迭代器的预读缓冲延迟）
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method")
        rid = req.get("id")
        if method == "initialize":
            resp = {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "persona-evo", "version": "2.0.0"}}}
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            resp = {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
        elif method == "tools/call":
            params = req.get("params") or {}
            result = dispatch(params.get("name", ""), params.get("arguments") or {})
            resp = {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}]}}
        elif method == "ping":
            resp = {"jsonrpc": "2.0", "id": rid, "result": {}}
        else:
            resp = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601,
                                                           "message": f"method not found: {method}"}}
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
