"""adapter.py — 平台/模型适配器（换模型只改 persona.config.json，persona.json 零改动）。

引擎对模型能力的全部期望只有 LLMHooks 的五个方法；适配器的职责是：
1. 把平台消息/反馈转成引擎的 event/outcome（_to_event / _to_outcome / _to_platform）；
2. 按 persona.config.json 的 adapter/model_slot 构造 LLMHooks（resolve_llm_hooks）。

三种接入形态（详见 docs/adapter_guide.md）：
- dsh：宿主 agent 会话即模型能力提供方（prefill 模式，或 MCP 受控工具面）；
- openai：适配器内直连 OpenAI 兼容 chat completions（JSON mode 结构化输出）；
- langchain：LLMHooks 用 Runnable 实现。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from persona_evo import PersonaEvo, LLMHooks


def load_config(root: str | Path) -> dict:
    p = Path(root) / "persona.config.json"
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}


def resolve_plugin_root(config: dict, default_root: str | Path | None = None) -> Path:
    """运行时根覆盖：config.runtime.plugin_root → 环境变量 → 本文件上级。"""
    import os
    candidates = [
        (config.get("runtime") or {}).get("plugin_root"),
        os.environ.get("PERSONA_EVO_ROOT"),
        str(default_root) if default_root else None,
        str(Path(__file__).resolve().parent.parent),
    ]
    for c in candidates:
        if c and Path(c).joinpath("persona.json").exists():
            return Path(c)
    return Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# OpenAI 兼容钩子（示例实现：结构化 JSON 调用）
# ---------------------------------------------------------------------------

NARRATE_PROMPT = (
    "你是人格演化引擎的叙事生成器。读取事件与关系先验，输出 JSON："
    '{"her_read": "第一人称解读", "threat_type": "autonomy_threat|exposure_threat|relational_uncertainty|none", '
    '"active_hypothesis_ids": []}。只输出 JSON。'
)

ATTRIBUTE_PROMPT = (
    "你是人格演化引擎的反思归因器。读取互动结果，输出 JSON："
    '{"attribution": "narrative_error|strategy_error|both_ok|insufficient_data", '
    '"my_read": "", "her_read_pattern": "", "param_changes": []}。'
    "证据不足时必须选 insufficient_data，不强行解释。只输出 JSON。"
)


class OpenAIHooks(LLMHooks):
    """OpenAI 兼容 API 的 LLMHooks 实现（密钥经 api_key_path 读取，不落配置）。"""

    def __init__(self, model_slot: dict):
        self.model = model_slot.get("model", "")
        self.endpoint = (model_slot.get("endpoint") or "https://api.openai.com/v1").rstrip("/")
        key_path = model_slot.get("api_key_path", "")
        self.api_key = Path(key_path).read_text(encoding="utf-8-sig").strip() if key_path else ""
        self._headers = {"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"}

    def _chat_json(self, system: str, user: str) -> dict:
        import urllib.request
        body = json.dumps({"model": self.model, "response_format": {"type": "json_object"},
                           "messages": [{"role": "system", "content": system},
                                        {"role": "user", "content": user}]}).encode()
        req = urllib.request.Request(f"{self.endpoint}/chat/completions", data=body,
                                     headers=self._headers, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
        return json.loads(data["choices"][0]["message"]["content"])

    def narrate(self, event, relation, tension_sig):
        try:
            out = self._chat_json(NARRATE_PROMPT, json.dumps(
                {"event": event, "relation": relation, "open_tensions": tension_sig},
                ensure_ascii=False))
            return {"trigger_scene": event.get("semantic", ""),
                    "her_read": out.get("her_read", ""),
                    "threat_type": out.get("threat_type", "none"),
                    "active_hypothesis_ids": out.get("active_hypothesis_ids", [])}
        except Exception:
            return {"trigger_scene": event.get("semantic", ""),
                    "her_read": "(模型调用失败) 信息不足",
                    "threat_type": "none", "active_hypothesis_ids": []}

    def attribute(self, outcome):
        try:
            return self._chat_json(ATTRIBUTE_PROMPT, json.dumps(outcome, ensure_ascii=False))
        except Exception:
            return {"attribution": "insufficient_data", "param_changes": []}


def resolve_llm_hooks(config: dict) -> LLMHooks:
    """按 config.adapter 构造钩子。dsh → prefill（宿主提供）；openai → OpenAIHooks。"""
    adapter = (config.get("adapter") or "dsh").lower()
    if adapter in ("openai", "openai-compatible"):
        return OpenAIHooks(config.get("model_slot") or {})
    # dsh / langchain / custom：由宿主注入（dsh 用 src/run.py 的 AgentPrefillLLM 或 MCP 工具面；
    # langchain 见 docs/adapter_guide.md 的 Runnable 模板）
    from run import AgentPrefillLLM
    return AgentPrefillLLM()


# ---------------------------------------------------------------------------
# 平台适配基类
# ---------------------------------------------------------------------------

class PersonaEvoAdapter:
    """宿主接入基类。生命周期：load → on_interaction → on_feedback → tick → shutdown。"""

    def __init__(self, plugin_root: str | Path | None = None):
        self.root = resolve_plugin_root(load_config(plugin_root or Path(__file__).resolve().parent.parent),
                                        plugin_root)
        self.config = load_config(self.root)
        self.engine = PersonaEvo(self.root, llm=resolve_llm_hooks(self.config))
        self.tick_interval_ms = (self.config.get("tick") or {}).get("interval_ms", 300000)

    # ---- 宿主需实现的三个转换 ---------------------------------------------

    def _to_event(self, raw_message: dict) -> dict:
        """平台消息 → InteractionEvent。必须产出 semantic（语义描述，非分类标签）
        与 relation_id；dsh prefill 模式下可附 narrative_prefill。"""
        raise NotImplementedError

    def _to_outcome(self, raw_feedback: dict, event: dict) -> dict:
        """平台反馈 → Outcome。feedback 至少含 kind/relation_intact/boundary_acknowledged；
        无法判定的字段一律置 None（→ evaluate_outcome 判 uncertain）。"""
        raise NotImplementedError

    def _to_platform(self, behavior: dict) -> Any:
        """引擎约束包 → 平台消息。behavior 只含意图+边界+hard_rules，宿主模型在约束内
        生成具体内容；本适配器不得填入固定台词。"""
        raise NotImplementedError

    # ---- 生命周期 -----------------------------------------------------------

    def load(self) -> dict:
        return {"plugin": "persona-evolution", "root": str(self.root),
                "adapter": self.config.get("adapter", "dsh"),
                "policies": len(self.engine.state.get("policy_lib", {}).get("policies", [])),
                "self_locked": self.engine.state.get("self", {}).get("revision_gate", {}).get("locked"),
                "self_slots_unfilled": self.engine.state.get("self", {}).get("identity", "").startswith("{"),
                "big5_enabled": bool(self.engine.state.get("big5", {}).get("enabled"))}

    def handle_message(self, raw_message: dict) -> Any:
        event = self._to_event(raw_message)
        behavior = self.engine.on_interaction(event)
        self.engine.state.setdefault("policy_lib", {}).setdefault("runtime", {})["_last_relation"] = \
            event.get("relation_id", "unknown")
        return self._to_platform(behavior)

    def handle_feedback(self, raw_feedback: dict, event: dict) -> str:
        outcome = self._to_outcome(raw_feedback, event)
        return self.engine.on_feedback(outcome)   # success / fail / uncertain

    def idle(self) -> None:
        """空闲钩子：批量事件反思 + 触发式叙事反思 + tick。"""
        self.engine.reflect_event()
        self.engine.maybe_reflect_narrative()
        self.engine.tick()

    def prompt_context(self) -> dict:
        return self.engine.build_prompt_context()
