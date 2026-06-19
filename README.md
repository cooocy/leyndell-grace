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

## find-duplicates

`find_duplicates.py` 基于 MD5 查找指定目录下的重复文件，无需安装第三方依赖。

```bash
python find_duplicates.py /home/user/documents
python find_duplicates.py .
```

原理：递归遍历所有文件，先按大小分组，仅对大小相同的文件计算 MD5，最后输出重复文件组及可回收空间。

## renamer

`renamer.py` 批量重命名文件，支持顺序编号和时间格式两种模式。

```bash
# 顺序编号
python renamer ./photos --prefix trip --seq 3
# trip_001.jpg, trip_002.png, trip_003.txt ...

python renamer ./photos --seq 2 --begin 5 --suffix "_thumb"
# 05_thumb.jpg, 06_thumb.png, 07_thumb.txt ...

# 时间格式（取文件创建时间，输出 yyyyMMdd-HHmmss）
python renamer ./photos --time
# 20201112-155909.jpg, 20201112-155909-1.png, 20201112-160015.txt ...

python renamer ./photos --prefix trip --time --suffix "_backup"
# trip_20201112-155909_backup.jpg, trip_20201112-155909-1_backup.png ...
```

| 参数 | 说明 |
|------|------|
| `directory` | 目标目录 |
| `--prefix` | 文件名前缀，默认为空 |
| `--suffix` | 文件名后缀（扩展名前），默认为空 |
| `--seq` | 顺序编号模式，指定位数（1-8） |
| `--begin` | 起始序号（仅 --seq 模式），默认 1 |
| `--time` | 时间格式模式，输出 yyyyMMdd-HHmmss |

时间戳重复时自动追加 `-1`, `-2`, ... 后缀；子目录会被忽略并在末尾提示。
