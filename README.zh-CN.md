# Codex Mail Workbench

本仓库是一个给 Codex 和其他 coding agent 使用的本地邮件工作台。

它把 IMAP 邮箱同步到本机 SQLite raw EML store，在原始证据之上维护可审核的
私有人物/项目记忆，并可把预配置的 Obsidian 目录作为只读知识源；同时提供
read-first CLI 和受审核约束的 Apple Mail 草稿闭环。真实账号配置、
邮件库、记忆、知识索引、草稿 ledger、同步游标和个人规则不进入 Git。

它是一个通用、独立的本地 Python CLI 应用包，不是带窗口的 macOS `.app`。
Apple Mail 是草稿审核界面，Workbench 负责稳定身份、审批指纹和发送凭证。

## 适用场景

- 让 Codex 查询本地邮箱，而不是优先依赖 Apple Mail UI 自动化。
- 按账号、文件夹、发件人、主题、收件人、message id 或正文搜索本地邮件。
- 通过稳定的 `email-store://...` 引用读取选定邮件。
- 通过带来源和状态的 `mail-memory://...` 记忆维持人物、关系和项目连续性。
- 预配置并索引 Obsidian Markdown，只把相关片段放入本次起草上下文。
- 用 `context build` 组装已批准记忆、近期邮件证据和外部知识。
- 通过稳定的 `mail-draft://...` 引用创建或接管 Apple Mail 草稿。
- 用户审核后，以当前内容指纹执行至多一次发送并回读 Sent 凭证。
- 为 agent 邮件 triage 提供一个可复用、可发布、隐私边界清楚的工具层。

## 快速开始

安装本地命令：

```bash
git clone https://github.com/gaofeng21cn/codex-mail-workbench.git
cd codex-mail-workbench
make install-local
```

创建本地私有 profile：

```bash
mkdir -p local/sync-state
cp config/accounts.example.toml local/accounts.toml
```

编辑 `local/accounts.toml`，写入真实账号元数据。密码或 app password 放入
macOS Keychain，不写进 TOML：

```bash
security add-generic-password -s codex-mail-workbench -a keychain.work.imap -w '<app-password>'
```

检查配置：

```bash
CODEX_MAIL_HOME=./local codex-mail --json doctor
CODEX_MAIL_HOME=./local codex-mail --json accounts
```

同步并读取：

```bash
CODEX_MAIL_HOME=./local codex-mail --json sync --account work --mode incremental
CODEX_MAIL_HOME=./local codex-mail --json recent --account work --limit 20
CODEX_MAIL_HOME=./local codex-mail --json recent --account work --since 2026-06-13T00:00:00+08:00 --until 2026-06-17T00:00:00+08:00 --limit 100
CODEX_MAIL_HOME=./local codex-mail --json search "invoice" --account work --limit 10
CODEX_MAIL_HOME=./local codex-mail --json read 'email-store://work/INBOX/12345/abcdef1234567890'
```

配置知识源并建立索引：

```bash
cp config/sources.example.toml local/sources.toml
CODEX_MAIL_HOME=./local codex-mail --json sources list
CODEX_MAIL_HOME=./local codex-mail --json sources index
```

登记人物、提出带证据的候选，并在审核后批准：

```bash
CODEX_MAIL_HOME=./local codex-mail --json memory entity upsert \
  --kind person --name '示例教授' --email 'person@example.test'
CODEX_MAIL_HOME=./local codex-mail --json memory propose \
  --entity '示例教授' --category event \
  --content '我们在年度学术会议见过面。' \
  --source 'email-store://work/INBOX/12345/abcdef1234567890'
CODEX_MAIL_HOME=./local codex-mail --json memory candidates --entity '示例教授'
CODEX_MAIL_HOME=./local codex-mail --json memory approve 'mail-memory://fact/UUID'
```

起草前组装最小上下文：

```bash
CODEX_MAIL_HOME=./local codex-mail --json context build \
  --person '示例教授' --project '示例协作项目' --query '年度邀请'
```

## 本地状态目录

默认状态目录：

```text
~/.codex-mail-workbench/
  accounts.toml
  mail.sqlite
  mail.sqlite-shm
  mail.sqlite-wal
  drafts.sqlite
  drafts.sqlite-shm
  drafts.sqlite-wal
  memory.sqlite
  memory.sqlite-shm
  memory.sqlite-wal
  sources.toml
  sync-state/
```

开发时可以显式使用仓库内 ignored 的 `./local`：

```bash
CODEX_MAIL_HOME=./local
```

`local/` 中可以放真实 `accounts.toml`、`profile.md`、邮件库和同步状态；这些内容
不应提交到公开仓库。

## CLI

```bash
codex-mail --json doctor
codex-mail --json accounts
codex-mail --json sync --account <account> --mode incremental
codex-mail --json recent --account <account> --limit 20
codex-mail --json recent --account <account> --since <start-iso> --until <end-iso> --limit 100
codex-mail --json search "<query>" --account <account> --limit 20
codex-mail --json search "<query>" --account <account> --since <start-iso> --until <end-iso> --limit 20
codex-mail --json read 'email-store://...'
codex-mail --json memory entity upsert --kind person --name <姓名> --email <地址>
codex-mail --json memory propose --entity <姓名或引用> --category <类别> --content <内容> --source 'email-store://...'
codex-mail --json memory candidates
codex-mail --json memory inspect 'mail-memory://fact/...'
codex-mail --json memory approve 'mail-memory://fact/...'
codex-mail --json memory reject 'mail-memory://fact/...'
codex-mail --json memory forget 'mail-memory://fact/...'
codex-mail --json memory search "<查询>"
codex-mail --json sources list
codex-mail --json sources index
codex-mail --json sources search "<查询>"
codex-mail --json context build --person <人物> --project <项目> --query <任务>
codex-mail --json draft create --account <account> --to <address> --subject <subject> --body-file <path>
codex-mail --json draft adopt --account <account> --apple-mail-uuid <uuid>
codex-mail --json draft inspect 'mail-draft://...'
codex-mail --json draft open 'mail-draft://...'
codex-mail --json draft send 'mail-draft://...' --approval 'sha256:...'
```

推荐用 UTF-8 纯文本文件创建草稿。`draft create` 默认在 Apple Mail 中显示草稿，
并返回稳定的 `draft_ref` 和 `approval_fingerprint`。用户在 Apple Mail 审核后，
再次运行 `draft inspect`；只有用户明确确认该指纹，才可把它传给 `draft send`。

记忆写入只通过 CLI：候选不会进入起草上下文，`approve/reject/forget` 都保留
状态和来源；`forget` 不会无痕物理删除。任一账号、发件人、To/Cc/Bcc、主题、
正文或附件变化都会使旧指纹失效。发送开始前
会原子占位，未知结果不会自动重试；只有 Sent 邮箱回读成功才记录为 `sent`。草稿
ledger 不保存正文或收件人。删除、归档、移动、标记仍不开放。

## 给 Agent 的使用方式

推荐流程：

1. 运行 `codex-mail --json doctor`。
2. 运行 `codex-mail --json accounts`，以输出作为当前账号真相。
3. 需要新鲜性时显式 sync。
4. 对“最近三天”等时间窗口，使用明确的 `--since` / `--until` ISO 边界。
5. 先搜索 metadata，再读取少量选定正文。
6. 起草前先运行 `context build`；只使用 approved 记忆。
7. 高风险事实仍按来源引用回读原文。
8. 汇报每个账号的覆盖范围和 freshness gap。

伴随 skill 位于
[`skills/codex-mail-workbench/SKILL.md`](skills/codex-mail-workbench/SKILL.md)，
UI discovery 元数据位于
[`skills/codex-mail-workbench/agents/openai.yaml`](skills/codex-mail-workbench/agents/openai.yaml)。

## 隐私边界

不要提交：

- 真实 `accounts.toml`
- `local/profile.md`
- `mail.sqlite`、`mail.sqlite-shm`、`mail.sqlite-wal`
- `drafts.sqlite`、`drafts.sqlite-shm`、`drafts.sqlite-wal`
- `memory.sqlite`、`memory.sqlite-shm`、`memory.sqlite-wal`
- 真实 `sources.toml`、Obsidian 路径、索引内容或人物关系记忆
- `sync-state/`
- raw EML、MBOX、Maildir 导出、`.env`、密码或 app password
- 真实账号相关示例

更多本地 profile 说明见 [docs/local-profile.md](docs/local-profile.md)。

## 验证

```bash
python -m pytest
detect-secrets scan --all-files
```

发布前还应执行一次针对本机个人标识的 grep，确认公开树中没有本地隐私信息。
