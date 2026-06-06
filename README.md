# Hermes Dynamic Workflows

你现在可以在 Hermes 里用 **dynamic workflows** 了：让模型现写一段受限 Python 脚本，
后台运行时执行它、用 `agent()/parallel()/pipeline()` 编排大量独立子代理——适合代码库
审计、大规模迁移、交叉验证的研究。参考自 Claude Code 的 dynamic workflows。

## 快速开始

一行装好并启用：

```bash
hermes plugins install <owner>/hermes-dynamic-workflows --enable && hermes tools enable workflow --platform cli
```

> gateway 平台把 `--platform cli` 换成 `telegram` 等，再 `hermes gateway restart`。

装完直接对 Hermes 说「用 workflow 跑一个 …」即可。

### 实时面板（可选，需单独一步）

`hermes plugins install` 只克隆插件、不安装它的 console 脚本，所以面板命令要单独装一次：

```bash
python scripts/install-hermes-workflows.py   # 装到 ~/.local/bin
```

之后在**另一个终端**运行 `hermes-workflows`，实时查看 run 列表、各 phase/agent 进度、
每个子代理的 prompt 与产出，并用 `x` 停止、`p` 暂停/恢复、`r` 重启、`s` 导出 transcript。

> 完整 JSON Schema 校验需要 `jsonschema`（装进运行 Hermes 的同一 Python 环境；缺失时
> 用内置简易校验器）：`python -m pip install "jsonschema>=4,<5"`。

## 配置（可选）

插件从 Hermes 的 `~/.hermes/config.yaml` 读下面这一节（每个键也支持
`HERMES_DYNAMIC_WORKFLOWS_*` 环境变量覆盖）：

```yaml
plugins:
  entries:
    dynamic-workflows:
      dynamic_workflows:
        concurrency: 8                  # max agents running at once (default: min(16, cpu-2))
        max_concurrency: 16             # hard ceiling for concurrency
        max_agents: 1000                # runaway backstop: total agents per run
        workflow_timeout_seconds: 900   # whole-run wall-clock deadline (paused time excluded)
        child_timeout_seconds: 300      # per child-agent timeout
        default_child_toolsets: [web, file, terminal, skills]  # toolsets each child gets by default
        keep_worktrees: false           # keep per-agent git worktrees instead of deleting them
        allow_model_override: true      # allow per-agent model routing via agent(model=...)
        require_launch_approval: true   # confirm before a top-level launch (deny if no channel)
        child_approval_policy: inherit  # flagged cmd, no human present: inherit|smart|deny|approve|ask
        ask_fallback: smart             # what 'ask' degrades to with no human: smart|deny|approve
        notify_on_complete: true        # inject <task-notification> on completion (CLI only)
        notify_result_preview_chars: 2000   # truncate the result shown in the notification
```

## 能力一瞥

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
    lambda t, _o, i: agent(f"Review for bugs: {t}", {"label": f"review:{i}", "phase": "Review"}),
    lambda r, _o, i: agent(f"Verify adversarially: {json.dumps(r)}", {"label": f"verify:{i}", "phase": "Verify"}),
)
return await agent("Synthesize the verified findings:\n" + json.dumps(findings))
```

- `agent(prompt, opts)` 起一个子代理；`opts` 可带 `schema`（强制结构化输出）、`model`、
  `agentType`、`isolation="worktree"`。
- `pipeline`（默认，无栅栏）/ `parallel`（栅栏）做并发；`phase`/`log` 报告进度；
  `workflow()` 内联跑命名工作流；`args` / `budget` 取入参与 token 预算。

运行时持久化脚本与每个子代理的完整执行链路（transcript），并在完成时把
`<task-notification>` 注入对话——无需轮询。用 `/workflows` 看历史与详情。

## 深入

实现细节（核心链路、工具与完整调用结果、prompt cache、并发与限额、权限治理、从
state.db 重建 transcript、沙箱、resume…）见 [TECHNICAL.md](./TECHNICAL.md)。

## License

[MIT](./LICENSE)
