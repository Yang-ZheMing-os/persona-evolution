# 适配器说明 —— 统一接口接入不同模型

**原则**：`persona.json` / 策略库 / 评估规则 / 演化循环全部模型无关（不含任何 provider/model 字段）；
模型相关信息只存在于 `persona.config.json`（model_slot / adapter）。**换模型 = 改 config，
persona.json 零改动**（这是"状态与配置分离"的验收标准）。

## 1. 引擎对模型的全部期望：LLMHooks

```python
class LLMHooks:
    narrate(event, relation, tension_sig) -> {her_read, threat_type, active_hypothesis_ids}  # 必须
    generate_behavior(policy, hard_rules) -> constraints_echo                                # 必须
    attribute(outcome) -> {attribution, param_changes}                                       # 必须
    propose_revision(trigger_ctx) -> revision | None                                         # 可选
    propose_policy(reflection) -> candidate | None                                           # 可选
```

评估、trial 计数、回滚、tick、审计全部是本地确定性逻辑，不经模型。模型只出现在两个边界点：
**叙事生成**（事件 → her_read JSON）与**行为生成**（约束包 → 文本），均受 schema 校验，
校验失败按 uncertain 处理。

## 2. 三种接入形态

### 2.1 DeepSeek Harness（DSH）—— prefill 模式

宿主 agent 会话本身就是模型能力提供方：

- **技能方式**：把本包挂到 DSH 技能目录（见 `integrations/dsh/README.md`），agent 按
  `SKILL.md` 的流程调 `src/run.py`，her_read/归因以 prefill JSON 传入（`AgentPrefillLLM`）。
- **MCP 方式**：MCP 客户端 spawn `src/mcp_persona_evo.py`（零依赖 stdio JSON-RPC），
  模型只能调 6 个受控工具；tick 由服务进程内部定时器持有。

```json
// persona.config.json
{ "adapter": "dsh", "llm_hooks": { "mode": "prefill" } }
```

### 2.2 OpenAI 兼容 API —— api 模式

适配器内直连 chat completions（JSON mode 结构化输出）：

```json
// persona.config.json
{
  "adapter": "openai",
  "model_slot": {
    "provider": "my-relay",
    "model": "your-model-id",
    "api_key_path": "C:/secrets/persona.key",
    "endpoint": "https://your-endpoint/v1"
  }
}
```

```python
from adapter import load_config, resolve_llm_hooks, resolve_plugin_root
from persona_evo import PersonaEvo

root = resolve_plugin_root(load_config("path/to/plugin"))
hooks = resolve_llm_hooks(load_config(root))     # → OpenAIHooks（标准库 urllib，无第三方依赖）
engine = PersonaEvo(root, llm=hooks)
```

密钥只经 `api_key_path`（路径引用）读取，不落配置文件。

### 2.3 LangChain —— Runnable 模式

```python
from langchain_core.runnables import RunnableLambda
from persona_evo import PersonaEvo, LLMHooks

narrate_chain = your_model | RunnableLambda(lambda s: parse_json(s))  # 你的链

class ChainHooks(LLMHooks):
    def narrate(self, event, relation, tension_sig):
        out = narrate_chain.invoke(NARRATE_PROMPT_USER_PAYLOAD)      # 同 2.2 的提示词
        return {"trigger_scene": event.get("semantic", ""),
                "her_read": out["her_read"], "threat_type": out.get("threat_type", "none"),
                "active_hypothesis_ids": out.get("active_hypothesis_ids", [])}
    def attribute(self, outcome):
        try:
            out = attribute_chain.invoke(json.dumps(outcome))
            return out
        except Exception:
            return {"attribution": "insufficient_data", "param_changes": []}
    # generate_behavior 直接用默认实现（回传约束包）

engine = PersonaEvo(root, llm=ChainHooks())
```

模型换了 → 换链；persona.json 不动。

## 3. 平台适配（消息层）：PersonaEvoAdapter

平台差异被压缩到三个转换函数：

| 函数 | 职责 |
|---|---|
| `_to_event(raw)` | 平台消息 → `{relation_id, semantic, narrative_prefill?}`；semantic 是语义描述，非分类标签 |
| `_to_outcome(raw, event)` | 平台反馈 → `{kind, relation_intact, boundary_acknowledged, ...}`；判不定的字段一律置 null → uncertain |
| `_to_platform(behavior)` | 约束包 → 平台消息；宿主模型在 intent+boundary+hard_rules 约束内生成，适配器不得填固定台词 |

## 4. 模型无关性保证（验收清单）

1. `persona.json` 无任何模型字段——换模型时该文件 diff 为空；
2. 评估/trial/回滚/tick/审计全部本地确定性逻辑；
3. 模型只出现在 narrate 与 generate_behavior 两个边界点，输出经 schema 校验，
   校验失败按 uncertain 处理（模型故障不会污染演化状态）；
4. 密钥走 `api_key_path` 路径引用；
5. `logs/audit_log.jsonl` 记录所有状态变更，迁移前后可 diff 审计。
