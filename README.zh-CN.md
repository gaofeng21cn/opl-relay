# OPL Relay

OPL Relay 是面向 Codex 与 OPL 的 local-first 个人通信中继。它把 IMAP 邮件同步到
本机 raw EML SQLite，以邮件原文作为证据维护人物/关系记忆，把选定 Obsidian
Markdown 作为只读知识源，并通过 Apple Mail 完成“起草、人工审核、指纹批准、
至多一次发送”的闭环。

本仓库直接由 Codex Mail Workbench 升级而来。Python 模块名和 `codex-mail`
命令继续兼容，新的产品、CLI 与插件身份是 `opl-relay`。

## 三个独立边界

- 安装目录：代码、插件 manifest、Skills，可随时替换或升级。
- 用户数据根：账号、邮件、草稿 ledger、记忆、同步游标和派生索引。
- 工作空间：profile、policies、context、templates、notes 和 exports。

新合同使用 `OPL_RELAY_HOME` 与 `OPL_RELAY_WORKSPACE`。已有
`CODEX_MAIL_HOME` 和 `~/.codex-mail-workbench` 数据继续可读，不做隐式搬迁。

```bash
make install-local
opl-relay --json doctor
opl-relay --json workspace init
```

兼容命令仍然有效：

```bash
codex-mail --json doctor
```

## 基本使用

```bash
opl-relay --json accounts
opl-relay --json sync --account work --mode incremental
opl-relay --json recent --account work --limit 20
opl-relay --json search "人物或项目" --account work
opl-relay --json read 'email-store://...'
opl-relay --json context build --person "示例教授" --query "年度邀请"
```

记忆必须带来源提出候选，经用户明确批准后才会进入起草上下文。Obsidian 只读。

Apple Mail 草稿流程：

```bash
opl-relay --json draft create \
  --account work \
  --to 'Recipient <recipient@example.test>' \
  --subject 'Subject' \
  --body-file ./draft.txt
opl-relay --json draft inspect 'mail-draft://apple-mail/work/UUID'
opl-relay --json draft open 'mail-draft://apple-mail/work/UUID'
```

用户在 Apple Mail 审核并确认后，再次 `inspect`，只使用当前返回的指纹发送。
任一内容变化都会使旧批准失效；未知发送结果不会自动重试。

## 插件与 OPL App

可安装 Codex Plugin 位于 [`plugins/opl-relay`](plugins/opl-relay)。插件携带能力
说明与 carrier-root [`opl-package.json`](plugins/opl-relay/opl-package.json) owner
descriptor，但不拥有用户数据或运行态真相。OPL Package 通过角色无关的 App
contributions 把相同能力交给 OPL App；OPL App 负责统一入口和可视化，不重写第二套
邮件引擎。

## 隐私与安全

真实账号、SQLite、raw mail、同步游标、关系记忆、Obsidian 路径、私人规则和
凭据都不得进入 Git。为兼容现有安装，Keychain service 仍为
`codex-mail-workbench`。

Relay 默认 read-first。目前不开放删除、归档、移动或标记；唯一外部写入是受
审核约束的 Apple Mail 草稿发送。

## 文档

- [整体架构](docs/architecture.md)
- [数据与 workspace](docs/local-profile.md)
- [运行合同](docs/workspace-contract.md)
- [Relay、Persona 与 OPL App](docs/product-architecture.md)

## 验证

```bash
make test
python3 /path/to/plugin-creator/scripts/validate_plugin.py plugins/opl-relay
```
