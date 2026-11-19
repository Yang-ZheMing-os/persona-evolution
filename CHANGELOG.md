# Changelog

## 2.0.0 (2026-11-19)

通用化重构（性格无关版）：

- **SELF 层与种子策略全部槽位化**：`{IDENTITY}` / `{VALUES}` / `{HARD_RULES}` /
  `{HYPOTHESIS}` 由使用者填入；系统不含任何性格标签。
- **三文件分离**：`persona.json`（她）/ `persona.config.json`（环境，模型插槽+适配器+tick）/
  `state.json`（运行时快照，自动重建）。换模型 = 改 config，人格状态零迁移。
- 引擎零第三方依赖（纯标准库）；MCP 服务器改为 stdio JSON-RPC 直接实现，同样零依赖。
- CLI 增加 `self_slots_unfilled` 提示；`context`/`state` 支持运行时根覆盖（config.runtime）。
- 冒烟测试覆盖全链路 + 防漂移约束（SELF 快照守卫 / uncertain 纪律 / 大五 clamp /
  self_write 边界审核 / 假设修订可回滚）。
- 评估规则与引擎/验证器共用同一 `evaluate_outcome`，判定永不漂移。

## 1.0.0

初版：八层状态机、trial→stable 演化循环、双层反思、DSH 技能与 MCP 接入。
