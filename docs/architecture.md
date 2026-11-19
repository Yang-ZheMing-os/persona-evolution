# 架构说明 —— 八层状态机与演化循环

## 0. 核心哲学

人格 = 稳定的 SELF 内核 + 一套关于"我如何保护自己、如何靠近他人"的假设系统。
场景只是检验假设的入口。成长 = 旧假设在关系反馈中被修订，但内核不漂移。

五条元原则：
1. 大五是仪表盘，不是方向盘（可选层，只调策略概率）；
2. 情绪不先于叙事；
3. 反思是唯一能改中期假设的手，权限止步于 SELF 之前；
4. 不写反应清单（系统只存策略语法、评估规则、演化元规则）；
5. 插件形态优先（状态外置、可迁移、可审计，模型可插拔）。

## 1. 八层架构

```
L0 SELF锚点（锁死，槽位化）            persona.json / self
L1 大五基线（可选层）                   persona.json / big5（enabled=false 可延后）
L2 中期累积层（账户/张力/假设）          persona.json / midterm
L3 内部叙事层（her_read/threat_type）    persona.json / narrative
L4 情绪层（OCC→即时→心境→残留）          persona.json / emotion
L5 策略库（candidate→trial→stable）     persona.json / policy_lib
L6 反思层（事件级批量+叙事级触发）        persona.json / reflection
L7 关系账本（跨会话连续性）              persona.json / relations
+ tick循环（四重衰减）                  state.json / tick_count
```

层间权限表：

| 层 | 可写者 | 粒度 | 可回滚 |
|---|---|---|---|
| L0 SELF | 仅使用者初始化（运行期无写入口） | 整体 | 人工 |
| L1 current | 事件级反思（clamp） | 数值 | 回正自动 |
| L2 | 叙事级反思（假设修订须可回滚） | 结构化 | 是（rollback_token） |
| L3 | 每次交互即时生成 | 生成物 | 不适用 |
| L4 | tick + on_feedback | 数值 | 自然衰减 |
| L5 | trial 机制 + self_write | 状态机 | rolled_back |
| L7 | 事件级反思 + ledger.update | 数值+张力 | 部分 |

## 2. 演化循环

```
on_interaction:  narrate → appraise(OCC→Plutchik) → preload → select_policy → generate_behavior
on_feedback:     evaluate → {success: 推进 / fail: 立即回滚 / uncertain: 只记日志}
reflect_event:   批量归因 → 参数微调（非散文）→ BLINDSPOT（证据不足）
maybe_reflect:   触发式（证伪≥3/大额变动/安全期）→ 边界审核 → 可回滚假设修订
tick:            情绪衰减 / 大五回正 / 策略衰减 / 张力老化 → 刷新 state.json
```

## 3. 防漂移双级约束

安全底线（7 条，不可违反）：不攻击不可改变特征 / 反思不碰 SELF / 情绪不跳过叙事 /
大五不生成行为 / self_write 只产候选卡 / 信息不足标 BLINDSPOT / 不写反应清单。

演化纪律（7 条，可配置）：SELF 一次互动不变 / 大五 clamp / trial_required（relaxed 可降 2）/
fail 立即回滚 / 反思产物入参数 / 假设修订可回滚留日志 / prompt 只注入必要状态。

对应落点见 SKILL.md §9 对照表。

## 4. 文件与读写权

| 文件 | 归属 | 谁写 | 说明 |
|---|---|---|---|
| persona.json | 她 | 仅引擎（SELF 段除外，写前逐字快照比对） | 持久演化状态 |
| persona.config.json | 环境 | 仅使用者 | 模型插槽/适配器/tick |
| state.json | 运行时 | 引擎自动重建 | 快照，可随时删除 |
| logs/audit_log.jsonl | 审计 | 引擎追加 | 只增不改，迁移可 diff |

## 5. SELF 守卫机制

引擎构造时冻结 SELF 段快照；每次写盘前逐字比对，不一致即抛错拒绝写回
（`_guard_self_immutable`）。任何通过 `self_write_policy` / 反思 / 叙事级修订触碰
SELF 的提案都会被 `boundary_check` 拒绝并留审计记录。
