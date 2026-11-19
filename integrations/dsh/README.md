# DSH（DeepSeek Harness）接入指南

本包接入 DSH 有两条路，都满足"模型即能力提供方"（her_read/归因由宿主 agent 生成后传入，引擎只做确定性逻辑）。

## 方式一：技能挂载（零改动）

DSH 的技能子系统扫描用户级根 `~/.dsh/skills/<name>/SKILL.md`（目录包，一层深）：

```powershell
# Windows：junction 挂载（0 数据字节落在 C 盘，实体留在本目录）
New-Item -ItemType Junction -Path "$env:USERPROFILE\.dsh\skills\persona-evolution" -Target "D:\plugins\persona-evolution"
```

挂载后任意 DSH 会话的技能目录出现 `persona-evolution`（热发现，无需重启）。
agent 按 SKILL.md §10 的流程用 `python src/run.py` 操作引擎。

## 方式二：MCP 服务器（受控工具面）

DSH / 任何 MCP 宿主可以 spawn 本包的 MCP 服务器（零依赖 stdio JSON-RPC）：

```yaml
# 宿主的 MCP 客户端配置（以 DSH cordis patch 行为例；command 为本机 python）
- id: mcp-persona-evo
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: persona-evo
    transport: stdio
    command: 'C:/path/to/python.exe'
    args:
      - 'D:/plugins/persona-evolution/src/mcp_persona_evo.py'
```

工具面（模型只能调这 6 个结构化工具，无任意命令执行）：

| 工具 | 作用 |
|---|---|
| `persona_interact` | 叙事+情绪+策略选择 → 返回 intent/boundary/hard_rules 约束包 |
| `persona_feedback` | success/fail/uncertain（判不定的字段不传 → uncertain） |
| `persona_reflect` | 事件级归因 → 参数微调 |
| `persona_narrative_reflect` | 中期假设修订（边界审核 + rollback_token） |
| `persona_context` | 注入切片（只读） |
| `persona_state` | 状态摘要（只读） |

tick 衰减由 MCP 服务进程内部定时器按 `persona.config.json` 的 `tick.interval_ms` 驱动，不给模型。

## 安全注意

- 若宿主会面向不可信用户（如群聊），请确认宿主侧已有工具白名单（本引擎的工具面
  本身不暴露本地执行能力：spawn 固定参数数组 + JSON 临时文件）。
- MCP 用的解释器路径在 `persona.config.json` 的 `runtime.python` 配置（空则回退当前解释器）。
