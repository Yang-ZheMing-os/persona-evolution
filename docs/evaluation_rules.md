# 评估规则说明 —— success / fail / uncertain（系统地基）

> 评估规则是本系统的地基。评估规则的质量优先于种子策略的数量。
> 一切 trial 计数、权重更新、账户变动、大五更新都以此为唯一入口。
> 代码化实现：`src/persona_evo.py:evaluate_outcome`；验证脚本共用同一函数，判定永不漂移。

## 0. 判定输入

每次互动后，适配器把反馈转成 `outcome.feedback`：

| 字段 | 含义 | 取值 |
|---|---|---|
| `kind` | 反馈类型 | LIKED / DISLIKE / BOUNDARY_EVENT / NEUTRAL |
| `relation_intact` | 关系是否未破裂（对方未退场、未以退场相威胁） | true / false / **null（判不定）** |
| `boundary_acknowledged` | 策略主张的边界是否被承认 | true / false / null |
| `anxiety_delta` | 执行该策略后暴露焦虑的变化估计 | -1.0 ~ 1.0，null=未知 |
| `fail_signal_hit` | 策略卡 fail_signal 语义是否出现 | true / false |
| `mixed_signal` | 适配器显式标记的矛盾反馈 | true / false |

**规则零**：判不定的字段一律置 `null`，绝不猜测。字段为 null 的直接后果就是 uncertain。

## 1. success（须全部满足）

1. `kind ∈ {LIKED, NEUTRAL}`（NEUTRAL 且边界被承认也算温和成功）；
2. `relation_intact = true`；
3. `boundary_acknowledged = true`；
4. `anxiety_delta ≤ 0.05`：intended_effect 的代理指标满足（防御类焦虑不升；接纳类不得引发羞耻飙升）；
5. `fail_signal_hit = false`。

效果：trial 计数 +1、权重上调、关系账户存款、可能触发 evaluate_trial 固化。

## 2. fail（任一满足即 fail，立即回滚）

1. `relation_intact = false`；
2. `fail_signal_hit = true`；
3. `kind = DISLIKE 且 anxiety_delta ≥ 0.2`；
4. **成长型策略特例**（`intended_effect.type ∈ {accept_partially, acknowledge_minimally}`）：`anxiety_delta ≥ 0.2`——解读对但步子超出当前安全感，推得太快本身就是失败（归因 strategy_error）。

效果：立即回滚（不"再给一次机会"）、账户取款、高唤醒残留进入 L2、张力开账。

## 3. uncertain（除外的全部情况）

| 情形 | 例子 |
|---|---|
| 数据缺失 | 关键字段 null / kind 缺失 |
| 信号矛盾（显式） | `mixed_signal = true` |
| 信号矛盾（隐式） | DISLIKE 但边界被承认（点踩≠策略失败）；LIKED 但边界未被承认（可能是礼貌性正向） |
| **边界事件例外** | `kind = BOUNDARY_EVENT`：失败原因在对方自身边界状态，不能归因于策略 |
| 轮数不足 | 对方回复后再无后续轮（relation_intact 判不定 → null） |
| 归因歧义 | 无法区分叙事错还是策略错（反思层处理为 insufficient_data → BLINDSPOT） |

## 4. 为什么 uncertain 不推进 trial

1. **非对称风险**：错误固化的代价（人格漂移、刻板反应固化）远大于慢一步固化；回滚在用户侧表现为"性格突变"，代价极高。宁可演化慢，不可演化错。
2. **单次正反馈无独立验证**：一次 LIKED 可能是巧合、礼貌或当时心情；没有旁证时不构成"假设被验证"的证据。
3. **uncertain 本身携带信息**：进入日志与反思队列，作为 BLINDSPOT 候选，为叙事级反思积累"证据不足"记录——信息不被丢弃，只是不被当成确定性证据。
4. **防反馈通道污染**：若 uncertain 可推进 trial，任何含噪反馈（误触、平台默认表情）都会演化出错误人格。uncertain 是反馈通道的滤波器。
5. **证伪同样从严**：明确 fail 才计入证伪计数，uncertain 不计入——噪声既不能固化策略、也不能触发假设修订。

## 5. 判定优先级（代码实现顺序）

```
BOUNDARY_EVENT            → uncertain   （边界事件例外，最高优先）
数据缺失                  → uncertain
relation_intact = false   → fail
fail_signal_hit           → fail
DISLIKE + 焦虑升 ≥0.2      → fail
成长型 + 焦虑升 ≥0.2       → fail
mixed_signal              → uncertain
DISLIKE + 边界被承认       → uncertain
LIKED + 边界未被承认       → uncertain
LIKED/NEUTRAL + 全条件满足 → success
其余                      → uncertain   （默认保守出口）
```

注意末行：**默认出口是 uncertain，不是 success**。

## 6. 与反思层的接口

- success / fail → 事件级反思归因（narrative_error / strategy_error / both_ok）→ 参数微调；
- uncertain → 仍进反思队列，但归因多为 `insufficient_data` → 开 BLINDSPOT，不强行解释；
- 只有明确证伪（fail 且归因为 narrative_error）才推进 her_read 证伪计数；累计 ≥3 次触发叙事级假设修订。

## 7. 稳定安全期的放宽边界（可配置）

`evolution_discipline.mode = relaxed` 时：
- trial_required 可降为 2（纪律 10 允许）；
- **fail 立即回滚不放宽**（安全底线）；
- uncertain 语义不放宽：uncertain 永远不计数。
