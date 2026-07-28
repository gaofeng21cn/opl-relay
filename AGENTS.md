# OPL Relay

本仓持有 OPL Relay 的 local-first 邮件引擎、Codex Plugin、SQLite raw EML store、read-first CLI 和受审核约束的 Apple Mail 草稿实现。

- 真实账户、邮件、同步游标、私有 profile 和凭据不得进入 Git；运行态唯一属于
  `OPL_PROFILE_WORKSPACE`，Relay 使用其中的 `data/relay`，不得使用旧目录、
  兼容环境变量、插件或 checkout 作为数据 authority。
- 当前仓库不提供 delete、archive、move 或 mark。新增邮箱写操作必须有独立授权、合同和测试边界。
- 优先复用 `opl-relay`，保留 `codex-mail` 兼容；不要绕过稳定的 `email-store://` identity 直接拼接数据库事实。
- 默认验证运行 `make test` 或 `python -m pytest`；运行态结论还须使用明确的
  `OPL_PROFILE_WORKSPACE` 做 fresh CLI readback。

<!-- CODEGRAPH_START -->
## CodeGraph

- 本仓库使用本地 `.codegraph/` 索引；该目录不得纳入 Git。
- 定义、调用、影响范围和代码路径等结构检索优先使用 CodeGraph；字面文本检索使用 `rg`。
- 索引缺失或过期时运行 `codegraph init .` 或 `codegraph sync .`。
<!-- CODEGRAPH_END -->
