# leyndell-grace

休息，补充能量，运行脚本。

## token-usage

`token_usage.py` 直接读取本地 Claude Code、Codex 和 OpenCode 的使用数据，
不依赖 `ccusage`，也不需要安装第三方 Python 依赖。

```bash
token-usage
token-usage --offline
token-usage --since 2026-06-01 --until 2026-06-09
token-usage -v
```

默认模式会从 LiteLLM 和 models.dev 刷新模型价格，最多每 24 小时刷新一次。
`--offline` 只使用 `~/.cache/token-usage/pricing.json` 中的本地缓存；
没有缓存价格的模型会显示 `-`。

支持通过以下环境变量覆盖数据目录：

- `CLAUDE_CONFIG_DIR`
- `CODEX_HOME`
- `OPENCODE_DATA_DIR`
