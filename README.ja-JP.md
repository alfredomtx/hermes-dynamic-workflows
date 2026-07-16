# Hermes Dynamic Workflows

> **[Hermes Agent](https://github.com/NousResearch/hermes-agent) 向けの Claude-Code スタイルの動的ワークフロー。**

[English](./README.md) | [简体中文](./README.zh-CN.md) | 日本語

Hermes で **動的ワークフロー（Dynamic Workflows）** を利用できるようになりました。モデルにサンドボックス化された Python
スクリプトをその場で書かせ、バックグラウンドランタイムで実行し、`agent()/parallel()/pipeline()` を使って
多数の独立したサブエージェントをオーケストレーションできます。コードベースの監査、大規模なマイグレーション、
クロスバリデーションを伴うリサーチに最適です。
[Dynamic Workflows in Claude Code](https://claude.com/blog/introducing-dynamic-workflows-in-claude-code) に着想を得ています。

ゲートウェイのライブ進捗には、実行時に観測された構造（パイプラインの項目数/ステージ数、並列バリアのレーン数、または観測済みの逐次エージェント手順）が表示されます。各構造には所属するエージェントと使用モデル、推論強度が表示され、ネストしたヘルパーも個別に確認できます。

https://github.com/user-attachments/assets/06ef3d0d-4d89-48c4-9851-e1cae690e9b0

## クイックスタート

1 行でインストールして有効化します。

```bash
hermes plugins install lingjiuu/hermes-dynamic-workflows --enable
```

> Gateway 利用者へ: インストール後に `hermes gateway restart` を実行してください。

インストールが完了したら、Hermes に「〜するワークフローを実行して」と伝えるだけで使えます。

### ライブダッシュボード（任意、別途セットアップが必要）

`hermes plugins install` はプラグインをクローンするだけで、コンソールスクリプトはインストールしません。
そのため、ダッシュボードコマンドは一度だけ別途インストールする必要があります。

```bash
python3 "${HERMES_HOME:-$HOME/.hermes}/plugins/dynamic-workflows/scripts/install-hermes-workflows.py"
# ~/.local/bin にインストールされます
```

その後、**別のターミナル**で `hermes-workflows` を実行すると、インタラクティブな
ダッシュボードが開きます。ここでは実行リスト、フェーズ／エージェントごとの進捗、各
サブエージェントのプロンプトと出力をリアルタイムで確認できます。

## 設定（任意）

プラグインは Hermes の `~/.hermes/config.yaml` から以下のセクションを読み込みます（すべてのキーは
`HERMES_DYNAMIC_WORKFLOWS_*` 環境変数でも上書きできます）。

```yaml
plugins:
  entries:
    dynamic-workflows:
      dynamic_workflows:
        concurrency: 8                # エージェントの最大同時実行数（デフォルト: min(16, cpu-2)）
        max_concurrency: 16           # 同時実行数のハードキャップ
        max_agents: 1000              # 1 回の実行あたりのエージェント総数の上限（暴走防止）
        max_nesting_depth: 2          # workflow() の最大ネスト深度（ルート + N 階層）。実行全体の上限は全階層に適用される
        workflow_timeout_seconds: 900 # 実行全体のウォールクロックタイムアウト（一時停止時間を除く）
        child_timeout_seconds: 300    # 単一の子エージェントのタイムアウト
        blocked_child_toolsets: [workflow, delegation, code_execution, memory, messaging, clarify]
                                      # 子エージェントの使用を禁止するツールセット
        default_child_toolsets: [web, file, terminal, skills]
                                      # 子エージェントのデフォルトツールセット（agentType が指定されていない場合に使用）
        keep_worktrees: false         # 各エージェントの git worktree を残すかどうか（デフォルトでは自動クリーンアップ）
        missing_agent_type_policy: error # 明示的な agentType が見つからない場合: error|fallback_warn
        require_launch_approval: true # トップレベルのワークフロー起動前に確認を要求する（オンラインの人がいない場合は拒否）
        child_approval_policy: inherit # 子エージェントの承認ポリシー: inherit|smart|deny|approve|ask
        ask_fallback: smart           # "ask" で連絡先が誰もいない場合のフォールバック: smart|deny|approve
        notify_on_complete: true      # 完了時に起点となった CLI または gateway セッションへ通知する
        notify_result_preview_chars: 2000  # 通知での結果プレビューの切り詰め長（文字数）
        notify_progress_stop_button: true  # ライブ進捗バブルにタップ可能な ⏹ 停止ボタンを表示（Telegram；インラインボタン対応のコアが必要）
        notify_on_launch: true        # 起動時に起点となった gateway チャットへ「workflow 開始」マーカーを送信
        auto_workflow_default_on: false # true の場合、各セッションはデフォルトで ON（/autoflow off を実行するまで）。全チャットのコストが上がる
        auto_workflow_min_chars: 24    # 「実質的」とみなす最小メッセージ長（安価な事前フィルタ、LLM 呼び出しなし）
        orphan_grace_seconds: 900      # 「PID 死亡」シグナルがない場合に古いと判定して回収するまでのアイドル時間窓（PID 再利用への保険）
        auto_resume_on_boot: false     # true の場合、起動時に回収したばかりの孤児ランを再起動する（キャッシュから再開）。出荷時は無効
        auto_resume_max: 3             # 1 回の起動で自動再開する孤児ランの上限（再開ストームを抑制）
        auto_resume_window_seconds: 21600 # 直近のアクティビティがこの時間窓内（6 時間）の孤児ランのみ自動再開
```

## クラッシュ復旧（孤児の回収 + 自動再開）

ランは、それを起動した Hermes プロセス内（gateway デーモンまたは CLI）で実行されます。
ランの進行中にそのプロセスが終了すると——最も多いのは `hermes gateway restart`——ラン
スレッドも一緒に死に、終端ステータスを書き込む前に終わるため、レコードは `running` の
まま永久に凍結され、`/workflows` はそれを生存中として表示し続けます。

次回 manager 起動時、プラグインはこうした孤児を**回収**します。アクティブ状態のまま
所有プロセスが消えたランは、新しい終端ステータス `interrupted` に切り替えられます。
「消えた」の判定は 2 通り——ランの owner PID がもう生きていない（主シグナル。再起動は
まさにこの形で旧 PID を kill します）、またはランが `orphan_grace_seconds` を超えて
アイドル（PID 再利用や owner が解析不能なレコードへの保険）。生存プロセスがまだ保持
しているラン——別の gateway や、独立した `hermes-workflows` TUI——には決して触れません。

`interrupted` にする前に、回収器はランの journal にある完了済み子エージェントの結果を
すべて復旧キャッシュへ**回収（harvest）**します。各エージェントは完了時に結果を journal へ
書き込み、しかも復旧キャッシュと同じフィンガープリントをキーにするため、クラッシュは
完了済みの作業を何も失いません——拾い直すだけで済みます。これにより以降のどんな再開も
安価になります。完了済みエージェントは再利用され、未完了のものだけが再実行されます。

`auto_resume_on_boot`（出荷時は**無効**）はさらに一歩進みます。有効にすると、manager は
回収したばかりのランを再起動し、回収したキャッシュから再開して完了済みエージェントを
スキップします。境界付きです——起動ごとに最大 `auto_resume_max` 件、直近アクティビティが
`auto_resume_window_seconds` 内のもののみ、スクリプトがまだディスク上にある場合のみ、
そして完了メッセージを起点チャットへ返せる gateway ループが存在する場合のみ（ランの
ルーティングコンテキスト——プラットフォーム/チャット/スレッド、認証情報は決して含まない
——はまさにこのためにレコードへ永続化されます）。通常利用では無効のままに（再起動は意図
的なことが多く、再開は token を消費します）。ランを常に完了させたい無人 / ベンチマーク
用途で有効にしてください。

## スクリプト API

ワークフロースクリプトは、最初のステートメントがリテラルの `meta` である非同期 Python のコードに
過ぎません。その後はサンドボックス化されたグローバルを使って子エージェントをオーケストレーションします。

```python
meta = {
    "name": "repo-audit",
    "description": "並列レビューの後に敵対的検証を行う",
    "phases": [{"title": "Review"}, {"title": "Verify"}],
}

# 各ターゲットは review → verify を独立して流れる
# (pipeline にはバリアがない: B がまだ review にいる間に A は verify にいられる)
findings = await pipeline(
    args["targets"],
    lambda t, _o, i: agent(f"バグをレビュー: {t}", {"label": f"review:{i}", "phase": "Review", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "high", "maxTurns": 8, "maxToolCalls": 16, "maxToolOutputChars": 200000}),
    lambda r, _o, i: agent(f"敵対的に検証: {json.dumps(r)}", {"label": f"verify:{i}", "phase": "Verify", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "high", "maxTurns": 8, "maxToolCalls": 16, "maxToolOutputChars": 200000}),
)
return await agent("検証済みの結果を統合する:\n" + json.dumps(findings), {"provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "high", "maxTurns": 6, "maxToolCalls": 8, "maxToolOutputChars": 120000})
```

- `agent(prompt, opts)` は子エージェントを起動します。各呼び出しは `provider`、正規の `model`、
  `reasoningEffort`、`maxTurns`、`maxToolCalls`、`maxToolOutputChars` をインラインで必ず宣言します。欠落または無効な値はエージェント予約・起動前に失敗します。
  preset はロール指示とツール権限だけを定義し、ルーティングや予算を提供できません。Bedrock と `codex_app_server` は workflow reasoning effort を転送しないため、子エージェント起動前に失敗します。
- `pipeline`（デフォルト、バリアなし）／`parallel`（バリアあり）が並行処理を扱います。
  `phase`／`log` は進捗を報告し、`workflow()` は名前付きワークフローをインラインで実行し、`args` /
  `budget` は入力引数とトークン予算にアクセスします。

### エージェントタイプ

子エージェントのタイプはスクリプト内で `agentType` を使って指定します。省略した場合は
`general-purpose`（フルツールセット）がデフォルトになります。

| タイプ | ツールセット | 説明 |
|------|---------|-------------|
| `general-purpose` | `*`（すべての安全なツール） | デフォルト。コード検索、複雑な問題のリサーチ、複数ステップのタスクに適する |
| `explore` | 読み取り専用（read_file, search_files, terminal） | 高速なコードベース探索。ファイルの特定やキーワード検索に適する |
| `plan` | 読み取り専用（read_file, search_files, terminal） | ソフトウェアアーキテクチャ設計。ステップバイステップの実装計画を出力する |
| `verification` | web + file + terminal + browser | 実装の正しさを検証。build/test/lint を実行して PASS/FAIL を出力する |

Claude Code 風に、workflow 内の `meta["agents"]` で runtime agents も定義できます。外部 `.md` agent ファイルは必須ではありません。

```python
meta = {
    "name": "review-matrix",
    "description": "Review and verify",
    "agents": {
        "read-only-reviewer": {
            "instructions": "読み取り専用でコードをレビューし、編集しない。",
            "toolsets": ["file", "terminal"],
            "allowedTools": ["read_file", "search_files", "terminal", "process"],
        },
        "synthesizer": {"instructions": "結果を統合する。", "toolsets": []},
    },
}

findings = await agent("Review diff", {"agentType": "read-only-reviewer", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "high", "maxTurns": 8, "maxToolCalls": 16, "maxToolOutputChars": 200000})
return await agent("Synthesize: " + json.dumps(findings), {"agentType": "synthesizer", "provider": "openai-codex", "model": "gpt-5.6-luna", "reasoningEffort": "high", "maxTurns": 6, "maxToolCalls": 8, "maxToolOutputChars": 120000})
```

解決順序は `meta["agents"]` → project `.hermes/dynamic-workflows/agents` → user `~/.hermes/dynamic-workflows/agents` → plugin built-ins です。明示された `agentType` が見つからない場合はデフォルトでエラーです。`missing_agent_type_policy: fallback_warn` なら警告を記録して `general-purpose` にフォールバックします。`toolsets` 省略は継承、`toolsets: []` はツールなし、inline/runtime の `toolsets` は discoverable MCP/plugin toolsets で拡張されません。`allowedTools` は preset と交差し、空リストは通常ツールを拒否します。

ファイル型のエージェントタイプは優先順位順に 3 つの場所から解決されます（名前が衝突した場合は、
前方の場所が後方の場所を上書きします）。

1. `<project>/.hermes/dynamic-workflows/agents/*.md`  — プロジェクトレベル。現在のプロジェクトにのみ適用
2. `~/.hermes/dynamic-workflows/agents/*.md`          — ユーザーレベル。グローバルに適用
3. `<plugin>/hermes_dynamic_workflows/agents/*.md`    — 組み込みデフォルト（general-purpose/explore/plan/verification）

カスタムタイプを追加するには、上記 1 または 2 のディレクトリに次の形式で新しい `.md` ファイルを作成します。

```markdown
---
name: my-agent
description: "このエージェントが何のためのものかの短い説明。モデルがこれを使って適切なエージェントを自動選択します。"
toolsets: [web, file, terminal]
---

ここにエージェントのシステムプロンプトを記述し、その挙動、スタイル、制約を指示します。
```

`name` と `description` は必須です。preset は `toolsets`、`allowed_tools`、`disallowed_tools`、`isolation` を定義できます。
`provider`、`model`、`reasoning_effort`、子エージェント予算フィールドは preset では拒否され、各 `agent()` 呼び出しでインライン宣言する必要があります。

実行時、プラグインはスクリプトとすべての子エージェントの完全な実行トレース（トランスクリプト）を
永続化し、完了時に `<task-notification>` を会話に注入します。ポーリングは不要です。
履歴と詳細を表示するには `/workflows` を使用してください。

## ディープダイブ

実装の詳細（コア実行パス、ツールと完全な呼び出し結果、プロンプトキャッシュ、並行処理と制限、
権限ガバナンス、`state.db` からのトランスクリプトの再構築、サンドボックス化、レジューム…）については、
[TECHNICAL.md](./TECHNICAL.md) を参照してください。

## ライセンス

[MIT](./LICENSE)
