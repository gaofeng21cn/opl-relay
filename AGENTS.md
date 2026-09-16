# OPL Relay

本仓持有 OPL Relay 的 local-first 邮件引擎、Codex Plugin、SQLite raw EML store、read-first CLI 和受审核约束的 Apple Mail 草稿实现。

- 真实账户、邮件、同步游标、私有 profile 和凭据不得进入 Git；运行态唯一属于
  `OPL_PROFILE_WORKSPACE`，Relay 使用其中的 `data/relay`，不得使用旧目录、
  兼容环境变量、插件或 checkout 作为数据 authority。
- 当前仓库不提供永久 delete 或 mark；受控 `mailbox move` 只接受精确
  `storage_ref`，只移动到已存在的 Archive/Trash，并要求显式 `--apply`、实时校验、操作回执和测试边界。
- 只使用 `opl-relay` 作为 CLI；不要绕过稳定的 `email-store://` identity 直接拼接数据库事实。
- 默认验证运行 `make test` 或 `python -m pytest`；运行态结论还须使用明确的
  `OPL_PROFILE_WORKSPACE` 做 fresh CLI readback。

## 文档生命周期

- 双语 README 是公开安装与使用入口；`docs/architecture.md` 负责内部实现，
  `product-architecture.md` 负责跨仓交接，`distribution.md` 负责 carrier 与分发，
  `workspace-contract.md` 负责数据不变量，`local-profile.md` 负责配置操作。
- 每个主题只有一个正文 owner，其他文档只摘要和链接；源码、命令或合同改变时同步更新正文与
  全部引用，删除被替代段落，不按日期追加旧状态、测试数量或完成清单。
- 已退役接口与模块的使用说明直接移除，不建立文档兼容页。历史默认留在 Git；只保留仍解释
  当前安全或设计决定的独立记录，并明确其历史身份。未完成目标不能伪装成实现或误删为历史。
- 本仓不保存他仓 installed/runtime/release 快照，不读取私有邮件来证明文档正确性。
  文档变更检查相对链接、引用资源和 `git diff --check`，涉及运行合同再运行受影响测试。

<!-- CODEGRAPH_START -->
## CodeGraph

- 本仓库使用本地 `.codegraph/` 索引；该目录不得纳入 Git。
- 定义、调用、影响范围和代码路径等结构检索优先使用 CodeGraph；字面文本检索使用 `rg`。
- 索引缺失或过期时运行 `codegraph init .` 或 `codegraph sync .`。
<!-- CODEGRAPH_END -->

- GitHub 上自己新建的对外文本用英文书写：commit subject/body、PR 标题与正文、Issue、comment、Release 正文与 Release Notes。产品名、代码标识、路径、命令与原始引用除外。他人写的 Issue、PR 或 comment，无论对方用什么语言，回复沿用对方的语言；历史中已有的非英文 commit 保持原样。

- 本 Package 的唯一发布机制是 OCI：Framework projection 声明的 `publication_ref` 与 `latest-stable`。不要创建 GitHub Release 页面或附件，也不要新增 ZIP、wheel 等平行发布脚本；annotated tag 只用于绑定源码，版本说明写在仓库文档与 Git 历史里。共享规则由 Framework 的 `docs/delivery/artifact-package-lifecycle-boundary.md` 持有。
