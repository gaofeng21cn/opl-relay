<p align="center">
  <img src="assets/branding/opl-relay-logo.png" alt="OPL Relay 标志" width="132" />
</p>

<p align="center">
  <a href="./README.md">English</a> | <a href="./README.zh-CN.md"><strong>中文</strong></a>
</p>

<h1 align="center">OPL Relay</h1>

<p align="center"><strong>面向 Codex 和 One Person Lab 的私有通信层，以原始证据支撑每一次判断和起草</strong></p>
<p align="center">邮件证据 · 往来上下文 · 审核后记忆 · Obsidian 上下文 · Apple Mail 审核</p>

OPL Relay 帮助 Codex 处理学术和工作邮件，同时把程序安装、插件缓存和个人数据严格
分开。它以邮件原文作为证据，持续整理人与项目的往来上下文，并把草稿交给 Apple Mail
供用户审核；没有单独、明确的发送批准，就不会发送邮件。

OPL Relay 是可以独立安装、独立使用的通信产品。它与 OPL Persona 并列协作，双方只
通过明确、经过审核的交接传递上下文。

产品、Package、插件、Skill 和命令行工具统一使用 `opl-relay` 这一公开身份；内部
Python 模块名不再作为第二个邮件入口。

## 可以怎样使用

- “检查最近三天的邮件，告诉我哪些事情需要决定。”
- “找出我和这位专家过去的往来，结合双方关系起草一封回复。”
- “把回复建立成 Apple Mail 草稿，我在那里审核，先不要发送。”
- “先把这条关系记忆的原始依据给我看，确认后再纳入长期上下文。”
- “把 Persona 已批准的邮件提案交给 Relay 起草，但不要直接发送。”

## 核心能力

**以邮件原文为证据**

Relay 把已配置的 IMAP 文件夹同步到本机 SQLite。检索结果和起草上下文使用稳定的
`email-store://` 引用，草稿中的事实可以追溯到原始邮件。

**经审核的关系记忆**

长期记忆必须同时提供来源证据，并经过用户明确批准，才能进入后续起草上下文。

**只读使用 Obsidian**

可以把指定的 Markdown 路径建立为只读索引。Relay 不会反向修改 Obsidian 仓库。

**在 Apple Mail 中完成审核**

Relay 建立真实的 Apple Mail 草稿，随后重新读取草稿，并把批准绑定到当前内容指纹。
只要草稿内容发生变化，旧批准就自动失效。

**与 Persona 分工协作**

OPL Persona 可以把已批准、带证据的邮件上下文交给 Relay。邮箱账号、收件人、
Apple Mail 草稿和最终发送门仍由 Relay 单独负责。

## 四个边界

| 层次 | 保存什么 | 由谁管理 |
| --- | --- | --- |
| Git 仓库 | 源码、测试、插件文件、Skill 和 Package 描述 | Git 与维护者 |
| Codex 插件快照 | 已安装的通信工作流和载体元数据 | Codex |
| Relay 引擎 | `opl-relay` 命令行工具和本机邮件实现 | 随 Codex Plugin 原生 carrier 提供；Relay 持有邮件语义，carrier 持有物理 installed state |
| 数字分身工作空间 | 邮件数据库、账号引用、已批准记忆、个人规则和 Persona 状态 | 用户 |

用户只需要选择一个数字分身工作空间：

```text
~/OPL/profiles/<profile>/
  profile/
  policies/
  context/
  templates/
  exports/
  data/
    relay/
    persona/
```

Relay 固定使用 `<profile>/data/relay`。重新安装、升级代码或清理插件缓存，都不应该
移动、覆盖或上传这个目录。

## 从 GitHub 安装 Codex 插件

当前公开仓库可以直接作为 Git Marketplace 添加到 Codex：

```bash
codex plugin marketplace add gaofeng21cn/opl-relay --ref main --json
codex plugin list --marketplace opl-relay --available --json
codex plugin add opl-relay@opl-relay --json
codex plugin list --marketplace opl-relay --json
```

刷新 Git Marketplace 并重新安装当前插件快照：

```bash
codex plugin marketplace upgrade opl-relay --json
codex plugin remove opl-relay@opl-relay --json
codex plugin add opl-relay@opl-relay --json
```

安装后请新建一个 Codex 任务，让新任务加载刚安装的插件快照。

> **分发边界：** 上述命令由 Codex 直接从 GitHub 安装插件。OPL App 使用下文说明的
> GHCR 能力包通道；两条路径交付同一套 Relay 载体，但安装、更新和状态权威彼此独立。

## 新用户首次配置

当前需要 macOS、用于审核草稿的 Apple Mail，以及可用的 IMAP 邮箱。

```bash
export OPL_PROFILE_WORKSPACE="$HOME/OPL/profiles/my-profile"
opl-relay --json setup init
opl-relay --json account add \
  --id work --email you@example.com --host imap.example.com
opl-relay --json credential set --account work
opl-relay --json account check --account work --connect
```

`setup init` 可以重复运行，只会创建缺失的 Profile 模板和空配置文件。
`account add` 只写入 IMAP 元数据；密码会通过独立提示录入 macOS 钥匙串，不会进入
对话、命令参数、数字分身工作空间或 Git。

开发者仍可以从源码安装本地命令：

```bash
git clone https://github.com/gaofeng21cn/opl-relay.git
cd opl-relay
make install-local
```

## 一条典型工作流

```bash
opl-relay --json accounts
opl-relay --json sync --account work --mode incremental
opl-relay --json recent --account work --limit 20
opl-relay --json search "人物或项目" --account work
opl-relay --json read 'email-store://...'
opl-relay --json context build --person "示例教授" --query "年度邀请"
```

建立并查看 Apple Mail 草稿：

```bash
opl-relay --json draft create \
  --account work \
  --to 'Recipient <recipient@example.test>' \
  --subject 'Subject' \
  --body-file ./draft.txt

opl-relay --json draft inspect 'mail-draft://apple-mail/work/UUID'
opl-relay --json draft open 'mail-draft://apple-mail/work/UUID'
```

回复 Apple Mail 中已有的多收件人邮件时，由 Mail 原生 Reply All 路由计算收件人和主题，
再将该路由物化为稳定、可回读的审核草稿。命令必须接收本地筛选结果返回的精确
`account`、数字消息 `id` 和 `mailboxPath`：

```bash
opl-relay --json draft reply-all \
  --account work \
  --apple-mail-account 'Work' \
  --apple-mail-id 12345 \
  --mailbox-path 'INBOX/Conference' \
  --body-file ./reply.txt
```

Relay 不会从扁平邮件记录手工重建收件人。若原生路由包含自身地址、重复地址、Bcc、
空收件人集，或来源 tuple 不能唯一确定，命令会失败关闭，不登记稳定审核草稿。最终
保存的草稿是审核面；provider-native Reply All 是路由 authority。

发送是另一项独立操作。用户审核后需要再次读取草稿，并且只能使用这次读取返回的当前
指纹进行发送。内容变化会使旧批准失效；发送结果不明确时，系统不会自动重试。

## OPL App 自动管理通道

Relay 已声明 OPL 能力包和不绑定智能体角色的应用界面贡献；publication 与物理安装是
两个独立 authority 面：

```text
Relay owner descriptor + 不可变 GHCR publication
  -> OPL Base 下载 / 校验 / bytes handoff
  -> 配置的原生 carrier 安装 / 更新 / 修复 / 卸载
  -> 原生 installed readback
  -> Framework discovery / carrier action 委托 / aggregation
  -> OPL App 通用 Package projection
```

约定的稳定通道是
`ghcr.io/gaofeng21cn/one-person-lab-packages/opl-relay:latest-stable`。
Package publication 必须从公开不可变 GHCR digest 回读；installed/current 状态必须
从配置的原生 carrier 单独回读，Framework 只委托 action 并为 App 聚合结果。GitHub
继续承载源码和 Codex 插件市场，不使用 GitHub Release 分发 Relay。完整权威与可用性边界见
[分发与更新说明](docs/distribution.md)。

## 安全边界

- 用户邮件、账号配置、SQLite、原始 EML、同步游标、私人规则、Obsidian 路径和凭据
  都不能进入 Git 或插件缓存。
- Relay 默认只读，目前不开放删除、归档、移动或标记邮件。
- Persona 的提案批准不等于授权发送邮件。
- Apple Mail 草稿审核和基于内容指纹的发送批准始终分开。

## 文档

- [分发与更新](docs/distribution.md)
- [整体架构](docs/architecture.md)
- [数字分身工作空间](docs/local-profile.md)
- [运行合同](docs/workspace-contract.md)
- [Relay、Persona 与 OPL App](docs/product-architecture.md)

## 开发与验证

```bash
python3 -m pip install -e . pytest
make test
make validate-package
```

GitHub CI 还会校验插件结构，并在隔离的 Codex 环境中验证插件发现和安装流程。

## 许可证

[MIT](LICENSE)
