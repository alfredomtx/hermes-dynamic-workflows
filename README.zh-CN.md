# Hermes Dynamic Workflows

> **为 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 带来 Claude Code 式的 Dynamic Workflows。**

现在你可以在 Hermes 里使用 **Dynamic Workflows** 了：让模型现写一段受限 Python 脚本，
后台运行时执行它、用 `agent()/parallel()/pipeline()` 编排大量独立子代理——适合代码库
审计、大规模迁移、交叉验证的研究。参考自 [Dynamic Workflows in Claude Code](https://claude.com/blog/introducing-dynamic-workflows-in-claude-code)。

网关实时进度会显示运行时观察到的执行结构：流水线项目/阶段数、并行屏障通道数，或已观察到的顺序代理步骤。嵌套辅助函数运行时会暂时占用结构行，结束后恢复父级结构。

https://github.com/user-attachments/assets/06ef3d0d-4d89-48c4-9851-e1cae690e9b0

## 快速开始

一行装好并启用：

```bash
hermes plugins install lingjiuu/hermes-dynamic-workflows --enable
```

> gateway 用户装完再 `hermes gateway restart`。

装完直接对 Hermes 说「用 workflow 跑一个 …」即可。

### 实时面板（可选，需单独一步）

`hermes plugins install` 只克隆插件、不安装它的 console 脚本，所以面板命令要单独装一次：

```bash
python3 "${HERMES_HOME:-$HOME/.hermes}/plugins/dynamic-workflows/scripts/install-hermes-workflows.py"
# 装到 ~/.local/bin
```

之后在**另一个终端**运行 `hermes-workflows`，打开交互式面板，可以实时查看 run 列表、各 phase/agent 进度、
每个子代理的 prompt 与产出。

## 配置（可选）

插件从 Hermes 的 `~/.hermes/config.yaml` 读下面这一节（每个键也支持
`HERMES_DYNAMIC_WORKFLOWS_*` 环境变量覆盖）：

```yaml
plugins:
  entries:
    dynamic-workflows:
      dynamic_workflows:
        concurrency: 8                # 最大并发 agent 数（默认 min(16, cpu-2)）
        max_concurrency: 16           # 并发上限硬限制
        max_agents: 1000              # 单个 run 的 agent 总数上限（防逃逸）
        max_nesting_depth: 2          # workflow() 最大嵌套深度（根 + N 层）；run 级别上限仍跨所有层级生效
        workflow_timeout_seconds: 900 # 整个 run 的 wall-clock 超时（不含暂停时间）
        child_timeout_seconds: 300    # 单个子 agent 超时
        blocked_child_toolsets: [workflow, delegation, code_execution, memory, messaging, clarify]
                                      # 子 agent 禁止使用的 toolsets
        default_child_toolsets: [web, file, terminal, skills]
                                      # 子 agent 默认 toolset（不指定 agentType 时生效）
        keep_worktrees: false         # 是否保留 agent 的 git worktree（默认自动清理）
        missing_agent_type_policy: error # 显式缺失 agentType 时: error|fallback_warn
        require_launch_approval: true # 顶层 workflow 启动前需确认（无人在线则拒绝）
        child_approval_policy: inherit # 子 agent 审批策略: inherit|smart|deny|approve|ask
        ask_fallback: smart           # ask 无人可达时的降级: smart|deny|approve
        notify_on_complete: true      # 完成时通知发起的 CLI 或 gateway 会话
        notify_on_launch: true        # 启动时向来源 gateway 聊天发送「workflow 已启动」标记
        notify_result_preview_chars: 2000  # 通知中结果预览的截断长度（字符）
        notify_progress_stop_button: true  # 在实时进度气泡上显示可点击的 ⏹ 停止按钮（Telegram；需要支持内联按钮的核心）
        auto_workflow_default_on: false # 为 true 时每个会话默认 ON，除非运行 /autoflow off（会提高所有聊天的成本）
        auto_workflow_min_chars: 24    # 判定为「实质性」消息的最小长度（廉价预过滤，无 LLM 调用）
        orphan_grace_seconds: 900      # 无「PID 已死」信号时，判定为陈旧并回收的空闲时间窗（兜底 PID 复用）
        auto_resume_on_boot: false     # 为 true 时在启动时重新拉起刚回收的孤儿运行（从缓存恢复）；默认关闭
        auto_resume_max: 3             # 每次启动自动恢复的孤儿运行数上限（防止恢复风暴）
        auto_resume_window_seconds: 21600 # 仅自动恢复最近活动在此时间窗内的孤儿运行（6 小时）
```

## 崩溃恢复（孤儿回收 + 自动恢复）

运行在启动它的 Hermes 进程内执行（gateway 守护进程或 CLI）。如果该进程在运行
进行中退出——最常见的是 `hermes gateway restart`——运行线程随之死亡，来不及写入
终态，因此其记录被永久冻结在 `running`，`/workflows` 会一直把它显示为存活。

下次 manager 启动时，插件会**回收**这些孤儿：任何仍处于活动状态、但其所属进程已
消失的运行，会被翻转为新的终态 `interrupted`。「消失」有两种判定方式——运行的
owner PID 不再存活（主信号；重启正是这样杀掉旧 PID），或运行空闲超过
`orphan_grace_seconds`（兜底 PID 复用以及无可解析 owner 的记录）。仍由存活进程
持有的运行——另一个 gateway，或独立的 `hermes-workflows` TUI——永不触碰。

在标记 `interrupted` 之前，回收器会把运行 journal 中每个已完成子代理的结果**收割**
回其恢复缓存。每个代理在完成时就把结果写入 journal，且使用与恢复缓存相同的指纹键，
因此崩溃不会丢失任何已完成的工作——这些结果只需被重新拾起。这使后续任何恢复都很
廉价：已完成的代理被复用，只有未完成的才重跑。

`auto_resume_on_boot`（出厂**关闭**）更进一步：开启后，manager 会重新拉起它刚回收的
运行，从收割的缓存恢复，从而跳过已完成的代理。它是有界的——每次启动最多
`auto_resume_max` 个、仅最近活动在 `auto_resume_window_seconds` 内、仅当脚本仍在磁盘上、
且仅当存在 gateway 循环可把完成消息路由回来源聊天（运行的路由上下文——平台/聊天/线程，
绝不含凭据——正为此持久化在记录上）。常规使用请保持关闭（重启往往是有意的，恢复会
花费 token）；在运行应始终完成的无人值守 / 基准测试场景再开启。

## Script API

工作流脚本就是一段 async Python，首句是字面量 `meta`，之后用受限全局编排子代理：

```python
meta = {
    "name": "repo-audit",
    "description": "Parallel review, then adversarial verify",
    "phases": [{"title": "Review"}, {"title": "Verify"}],
}

# 每个目标独立流过 review → verify（pipeline 无栅栏：A 可在 verify 时 B 还在 review）
findings = await pipeline(
    args["targets"],
    lambda t, _o, i: agent(f"Review for bugs: {t}", {"label": f"review:{i}", "phase": "Review", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "high", "maxTurns": 8, "maxToolCalls": 16, "maxToolOutputChars": 200000}),
    lambda r, _o, i: agent(f"Verify adversarially: {json.dumps(r)}", {"label": f"verify:{i}", "phase": "Verify", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "high", "maxTurns": 8, "maxToolCalls": 16, "maxToolOutputChars": 200000}),
)
return await agent("Synthesize the verified findings:\n" + json.dumps(findings), {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "high", "maxTurns": 6, "maxToolCalls": 8, "maxToolOutputChars": 120000})
```

- `agent(prompt, opts)` 起一个子代理。每次调用都必须内联声明 `provider`、规范 `model`、
  `reasoningEffort`、`maxTurns`、`maxToolCalls` 和 `maxToolOutputChars`；缺失或无效值会在预留 agent 和启动前失败。
  preset 只定义角色指令和工具权限，不能提供路由或预算。Bedrock 和 `codex_app_server` 当前不会转发 workflow reasoning effort，因此会在子代理启动前失败。
- `pipeline`（默认，无栅栏）/ `parallel`（栅栏）做并发；`phase`/`log` 报告进度；
  `workflow()` 内联跑命名工作流；`args` / `budget` 取入参与 token 预算。

### Agent Type

脚本里通过 `agentType` 指定子代理类型，不填则默认 `general-purpose`（全工具集）:

| 类型 | 工具集 | 说明 |
|------|--------|------|
| `general-purpose` | `*`（全部安全工具） | 默认，适合搜索代码、研究复杂问题、多步任务 |
| `explore` | 只读（read_file, search_files, terminal） | 快速代码库探索，适合找文件、搜关键词 |
| `plan` | 只读（read_file, search_files, terminal） | 软件架构设计，输出分步实现方案 |
| `verification` | web + file + terminal + browser | 验证实现正确性，跑构建/测试/lint 出 PASS/FAIL |

也可以像 Claude Code 一样在脚本内定义 runtime agents，不必为每个 workflow 预先写 `.md` agent 文件：

```python
meta = {
    "name": "review-matrix",
    "description": "Review and verify",
    "agents": {
        "read-only-reviewer": {
            "instructions": "只读审查代码，不要编辑文件。",
            "toolsets": ["file", "terminal"],
            "allowedTools": ["read_file", "search_files", "terminal", "process"],
        },
        "synthesizer": {"instructions": "综合结果。", "toolsets": []},
    },
}

findings = await agent("Review diff", {"agentType": "read-only-reviewer", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "high", "maxTurns": 8, "maxToolCalls": 16, "maxToolOutputChars": 200000})
return await agent("Synthesize: " + json.dumps(findings), {"agentType": "synthesizer", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "high", "maxTurns": 6, "maxToolCalls": 8, "maxToolOutputChars": 120000})
```

解析顺序：`meta["agents"]` → 项目 `.hermes/dynamic-workflows/agents` → 用户 `~/.hermes/dynamic-workflows/agents` → 插件内置。显式写错 `agentType` 默认报错；`missing_agent_type_policy: fallback_warn` 会记录警告并回退到 `general-purpose`。`toolsets` 省略表示继承；`toolsets: []` 表示无工具；内联/runtime `toolsets` 不会被 discoverable MCP/plugin 工具集自动放宽；`allowedTools` 与 preset 取交集，空列表表示拒绝普通工具。

文件型 Agent type 按优先级从三个位置查找（同名时前面的覆盖后面的）:

1. `<项目>/.hermes/dynamic-workflows/agents/*.md`   — 项目级，仅当前项目生效
2. `~/.hermes/dynamic-workflows/agents/*.md`        — 用户级，全局生效
3. `<插件>/hermes_dynamic_workflows/agents/*.md`     — 内置默认（general-purpose/explore/plan/verification）

加自定义类型:在 1 或 2 的目录下新建 `.md`，格式如下:

```markdown
---
name: my-agent
description: "简短描述这个 agent 的用途,模型会根据描述自动选择合适的 agent。"
toolsets: [web, file, terminal]
---

你可以在这里写 agent 的 system prompt,指导它的行为、风格和约束。
```
`name` 和 `description` 必填。preset 可定义 `toolsets`、`allowed_tools`、`disallowed_tools` 和 `isolation`。
`provider`、`model`、`reasoning_effort` 与子代理预算字段在 preset 中会被拒绝，必须在每次 `agent()` 调用中内联声明。

运行时持久化脚本与每个子代理的完整执行链路（transcript），并在完成时把
`<task-notification>` 注入对话——无需轮询。用 `/workflows` 看历史与详情。

## 深入

实现细节（核心链路、工具与完整调用结果、prompt cache、并发与限额、权限治理、从
state.db 重建 transcript、沙箱、resume…）见 [TECHNICAL.md](./TECHNICAL.md)。

## License

[MIT](./LICENSE)
