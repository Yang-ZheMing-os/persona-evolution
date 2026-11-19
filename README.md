# Persona Evolution —— 通用人格演化插件框架

> 人格 = 稳定的 SELF 内核 + 一套关于"我如何保护自己、如何靠近他人"的假设系统。
> 场景只是检验假设的入口。成长 = 旧假设在关系反馈中被修订，但内核不漂移。

**性格无关**：本框架不绑定任何性格标签。SELF 层与种子策略全部是槽位（`{IDENTITY}` / `{VALUES}` / `{HARD_RULES}` / `{HYPOTHESIS}`），由使用者填入具体性格；系统在运行中经 trial→stable 机制自行演化表达方式。

**插件形态优先**：人格状态（`persona.json`）与环境配置（`persona.config.json`）分离；底层模型可插拔（DeepSeek Harness / OpenAI 兼容 / LangChain），换模型不改状态。

## 核心哲学（五条元原则）

1. **大五是仪表盘，不是方向盘**——可选层，只调策略概率，不生成行为
2. **情绪不先于叙事**——事件 → 解读 → OCC 评估 → 情绪 → 策略，顺序不可重排
3. **反思是唯一能改中期假设的手，权限止步于 SELF 之前**
4. **不写反应清单**——系统只存策略意图（行为语法）、评估规则、演化元规则；任何"如果她说 X 就该回 Y"的清单都是刻板印象，不进入系统
5. **插件形态优先**——状态外置、可迁移、可审计；模型只负责推理与生成

## 八层架构

```
L0 SELF 锚点（锁死，槽位化）              L4 情绪层（OCC → 即时 → 心境 → 高唤醒残留）
L1 大五基线（可选层，只调策略权重）         L5 策略库（candidate → trial → stable → rolled_back）
L2 中期累积层（关系账户/张力/假设）         L6 反思层（事件级批量微调 + 叙事级假设修订）
L3 内部叙事层（her_read / threat_type）    L7 关系账本（跨会话连续性）
                                          + tick 循环（四重衰减）
```

## 仓库结构

```
persona-evolution/
├── plugin.json               # 清单：入口、模型能力依赖、宿主契约
├── SKILL.md                  # 技能与逻辑描述（Frontmatter + 运作逻辑）
├── persona.json              # 她的数据：SELF 槽位 + 演化状态（运行期仅引擎可写）
├── persona.config.json       # 环境配置：模型插槽 / 适配器 / tick 频率
├── state.json                # 运行时快照（自动重建）
├── policies/seed_policies.json   # 8 条通用种子策略（仅作起点，不穷举）
├── schema/                   # persona 与 config 的 JSON Schema
├── src/
│   ├── persona_evo.py        # 引擎（零依赖，标准库实现）
│   ├── run.py                # 会话 CLI（prefill 模式）
│   ├── adapter.py            # 平台/模型适配器（DSH / OpenAI / LangChain）
│   ├── validate_evolution.py # 单维度演化验证
│   └── mcp_persona_evo.py    # MCP 服务器（stdio，零依赖 JSON-RPC）
├── docs/                     # 架构 / 评估规则 / 适配器指南
├── integrations/dsh/         # DSH 挂载与 MCP 接入
├── examples/                 # 填充示例 / prompt 注入示例 / 审计 trace / 验证日志
├── tests/test_smoke.py       # 冒烟测试（全链路 + 防漂移约束）
└── logs/audit_log.jsonl      # 可审计日志（追加式）
```

## 快速开始

```bash
# 1) 填槽位：编辑 persona.json —— {IDENTITY} / {VALUES} / {HYPOTHESIS_*}
#    （examples/persona.example.json 是一份填充示例）
# 2) 引擎自检
python src/persona_evo.py --root .

# 3) 一次互动（宿主 agent / 你本人以 prefill 提供叙事解读）
python src/run.py interact --event-file event.json
# event.json:
# { "relation_id": "R088",
#   "semantic": "冲突次日对方主动提及争执",
#   "narrative_prefill": { "her_read": "对方在递台阶，也可能在记账",
#                          "threat_type": "relational_uncertainty" } }
# → 返回 { policy_id, constraints: { intent, boundary, hard_rules } }
#   你的回复在 constraints 内生成（约束是行为语法，不是台词）

# 4) 反馈与反思
python src/run.py feedback --outcome-file outcome.json
python src/run.py reflect --attribution-file attr.json
python src/run.py tick

# 5) 验证演化（输入前后对话日志，输出 trial→stable 判定依据）
python src/validate_evolution.py \
  --before examples/interaction_log_before.jsonl \
  --after  examples/interaction_log_after.jsonl \
  --dimension policy:P005

# 6) 测试
python tests/test_smoke.py
```

## 演化循环

```python
on_interaction:  narrate → appraise → preload → select_policy → generate_behavior
on_feedback:     evaluate → success 推进 / fail 立即回滚 / uncertain 只记日志
reflect_event:   批量归因 → 参数微调（产物是参数数组，不是散文）
narrative_reflect: 触发式（证伪≥3 / 账户大额变动 / 稳定安全期）→ 可回滚假设修订
tick:            情绪衰减 / 大五回正 / 策略衰减 / 张力老化
```

**评估三分法（系统地基）**：`success`（关系未破裂+边界被承认+代理指标满足）/ `fail`
（立即回滚）/ `uncertain`（数据缺失、信号矛盾、边界事件例外——**不更新任何参数，
不推进 trial**）。宁可演化慢，不可演化错。完整判定标准与理由见
[docs/evaluation_rules.md](docs/evaluation_rules.md)。

## 接入不同模型

| 宿主 | 方式 | 文档 |
|---|---|---|
| DeepSeek Harness | 技能挂载（junction）或 MCP 受控工具面 | [integrations/dsh/README.md](integrations/dsh/README.md) |
| OpenAI 兼容 API | `adapter: "openai"` + model_slot（JSON mode 结构化调用，标准库实现） | [docs/adapter_guide.md](docs/adapter_guide.md) |
| LangChain | LLMHooks 用 Runnable 实现 | [docs/adapter_guide.md](docs/adapter_guide.md) |

换模型 = 改 `persona.config.json`；`persona.json` diff 为空。

## 防漂移硬约束

安全底线（不可违反）：不攻击不可改变特征 / 反思不得改 SELF / 情绪不得跳过叙事 /
大五不得生成行为 / 自写只能产候选策略卡 / 信息不足标 BLINDSPOT / 不写反应清单。

引擎层面的落点：SELF 无写入口（接口无 setter）；写盘前对 SELF 段逐字快照比对，
非法修改即拒绝写回；所有假设修订/策略固化/回滚进 `logs/audit_log.jsonl`（带
rollback_token）；`build_prompt_context` 只注入白名单键。

## 设计文档

- [docs/architecture.md](docs/architecture.md) —— 八层状态机与演化循环
- [docs/evaluation_rules.md](docs/evaluation_rules.md) —— success/fail/uncertain 判定标准
- [docs/adapter_guide.md](docs/adapter_guide.md) —— 统一接口接入不同模型
- [SKILL.md](SKILL.md) —— 供 agent 会话使用的逻辑描述

## 依赖

引擎与 CLI **零第三方依赖**（Python ≥ 3.10，纯标准库）。MCP 服务器为 stdio
JSON-RPC 直接实现，同样零依赖。

## License

MIT —— 见 [LICENSE](LICENSE)
