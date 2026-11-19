---
name: persona-evolution
version: 2.0.0
description: 通用人格演化引擎：SELF 内核（槽位化，使用者填入）+ 可修订的关系假设系统；八层状态 + trial→stable 演化循环。性格无关——具体性格由 persona.json 的槽位定义，系统在运行中自行演化。
---

# Persona Evolution —— 通用人格演化引擎

## 0. 五条元原则（先读，决定所有实现取舍）

1. **大五是仪表盘，不是方向盘。** L1 为可选层（前期可延后）；启用时只调策略被选中的概率，绝不生成行为。
2. **情绪不先于叙事。** 固定流程：事件 → her_read / threat_type → OCC 评估 → 情绪 → 策略。禁止跳过叙事直接给情绪。
3. **反思是唯一被允许修改中期假设的手，但权限止步于 SELF 锚点之前。**
4. **不写反应清单。** 系统存的是策略意图（语法）、评估规则和演化元规则。任何"如果她说 X 就该回 Y"的清单都是刻板印象，不进入系统。种子策略是演化起点，不穷举；新增策略只能经 trial→stable 机制产生，不得人工补写。
5. **插件形态优先。** 人格状态（persona.json）与环境配置（persona.config.json）分离；底层模型可插拔，状态可迁移、可审计。

## 1. 文件职责（三文件分离）

| 文件 | 归属 | 谁能改 |
|---|---|---|
| `persona.json` | 她的数据（SELF槽位/账本/策略库/张力/假设/反思日志） | 仅引擎（trial/反思/tick）；SELF 段无写入口 |
| `persona.config.json` | 环境的配置（模型插槽/密钥路径/适配器/tick频率） | 仅使用者 |
| `state.json` | 运行时快照（情绪/生效策略/最近反思） | 引擎自动重建；build_prompt_context 读这里 |

换模型 = 改 config；persona.json 零改动（这是"状态与配置分离"的验收标准）。

## 2. 八层架构

```
L0 SELF锚点（锁死，槽位化：{IDENTITY}/{VALUES}/{HARD_RULES}）
L1 大五基线（可选层；只调策略权重，不生成行为）
L2 中期累积层（关系账户 + 未解决张力 + 意义累积假设 {HYPOTHESIS}）
L3 内部叙事层（her_read / threat_type / active_hypothesis）
L4 情绪层（OCC评估 → 即时情绪 → 心境 → 高唤醒残留）
L5 策略库（candidate → trial → stable → rolled_back）
L6 反思层（事件级参数微调[支持批量] + 叙事级假设修订[触发式]）
L7 关系账本（跨会话连续性）
+ tick循环（情绪衰减 / 大五回正 / 策略衰减 / 张力老化）
```

## 3. 演化循环

```
on_interaction(event):
    narrative = narrate(event, relation, midterm.preload())   # L3 叙事先行
    emotion   = appraise(narrative)                            # L4 OCC→Plutchik，读数不是输入
    preload   = midterm.preload(narrative)                     # L2 张力/账户调防御初始权重
    policy    = select_policy(narrative, emotion, preload, big5?)  # 叙事匹配，非场景匹配
    return generate_behavior(policy)                           # 返回意图+边界+hard_rules 约束包，不产台词

on_feedback(outcome):
    signal = evaluate(outcome)          # success / fail / uncertain
    if signal == "uncertain":
        log_only(); return              # 不更新权重，不推进 trial
    ledger.update(signal); policies.update_weight(signal)
    midterm.update(signal); big5?.update(signal)
    if fail and not boundary_exception: policies.rollback(立即，不给第二次机会)

reflect_event(outcomes):                # 批量：归因 → 参数微调（非散文）
maybe_reflect_narrative():              # 触发式：证伪≥3 / 账户大额变动 / 稳定安全期
tick():                                 # 四重衰减 + 刷新 state.json 快照
build_prompt_context():                 # 只注入必要状态（白名单键）
```

## 4. 评估规则（系统地基，完整定义见 docs/evaluation_rules.md）

| signal | 判定要点 | 效果 |
|---|---|---|
| success | 关系未破裂 + 边界被承认 + intended_effect 代理指标满足 + 无 fail 信号 | trial 计数 +1，权重上调，账户存款 |
| fail | fail_signal 语义 / 关系破裂 / 成长型策略推太快 | 立即回滚，账户取款或透支 |
| uncertain | 数据缺失 / 信号混合 / BOUNDARY_EVENT 例外 / 归因歧义 | **只记日志，不更新任何参数，不推进 trial** |

判定不确定时一律 uncertain。**宁可演化慢，不可演化错。** 评估规则的质量优先于种子策略的数量。

## 5. 策略生命周期：candidate → trial → stable → rolled_back

- 种子策略初始化后以 **trial** 挂载；`trial_success_count >= trial_required` 且无 fail 记录 → **stable**。
- 稳定安全期（discipline = relaxed）下 trial_required 可降为 2；fail 回滚在任何模式下不可放宽。
- 触发 fail_signal → 立即回滚，唯一例外是元认知明确标记的 **BOUNDARY_EVENT**（→ uncertain 处理）。
- 长期未强化的 stable 策略，权重在 tick 中向基线衰减。
- verbal_pattern 只约束**意图与边界**，不约束句式；宿主模型在此约束内自由生成。
- 大五（若启用）只进入 select_policy 的权重计算，不出现在行为生成路径上。

## 6. 反思层（双层）

**事件级（每次或批量）**：归因 `narrative_error | strategy_error | both_ok | insufficient_data`。产物是**参数微调数组**（权重/账户/张力/衰减速率），不是散文；`insufficient_data` → 开 BLINDSPOT，不强行解释。

**叙事级（触发式）**：同类 her_read 证伪 ≥3 次 / 账户大额变动 / 稳定安全期 → 提出可回滚的中期假设修订，经边界审核后带 rollback_token 写入 revision_history。不得触碰 SELF 与 hard_rules。

## 7. 边界审核与 self_write_policy

自写插件只能产出**候选策略卡**：不得写 SELF/hard_rules；必经 boundary_check；候选卡仍走 trial→stable。

## 8. SELF 守卫

引擎无 SELF setter；每次写盘前对 SELF 段做逐字快照比对，非法修改即拒绝写回并抛错。

## 9. 防漂移约束对照

| 约束 | 落点 |
|---|---|
| 底线1 不攻击不可改变特征 | hard_rules 注入每次行为生成 |
| 底线2 反思不得改 SELF | 无写入口 + boundary_check + SELF 快照守卫 |
| 底线3 情绪不跳过叙事 | on_interaction 固定顺序，无旁路 |
| 底线4 大五不生成行为 | big5 仅在 select_policy 权重计算 |
| 底线5 self_write 只产候选卡 | 强制 candidate + 审核 |
| 底线6 信息不足标 BLINDSPOT | insufficient_data 分支 |
| 底线7 不写反应清单 | 种子仅 8 条且不穷举；新增只能走 trial |
| 纪律9 大五 clamp | baseline±max_drift |
| 纪律11 fail 立即回滚 | on_feedback fail 分支 |
| 纪律12 反思写入参数 | param_changes 数组，非文本 |
| 纪律13 假设修订可回滚 | rollback_token + revision_history |
| 纪律14 只注入必要状态 | build_prompt_context 白名单键 |

## 10. 快速使用

```bash
# 1) 填槽位：编辑 persona.json 的 {IDENTITY}/{VALUES}/{HYPOTHESIS_*}
# 2) 引擎自检
python src/persona_evo.py --root .
# 3) CLI 一次互动（宿主 agent 以 prefill 提供叙事/归因）
python src/run.py interact --event-file event.json
# 4) 验证演化
python src/validate_evolution.py --before a.jsonl --after b.jsonl --dimension policy:P005
```

接入 DSH / OpenAI / LangChain 见 `docs/adapter_guide.md`；DSH 技能挂载与 MCP 接入见 `integrations/dsh/README.md`。
