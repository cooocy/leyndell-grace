#!/usr/bin/env python3
"""
token_usage.py — 按工具 / 按模型 汇总 coding agent 的 token 消耗与花费。

底层调用全局 `ccusage`(需 `npm i -g ccusage`)，做两件 ccusage 默认表格不直接给的事：
  1. 跨所有日期，按「模型」汇总 token 与花费；
  2. 跨所有日期，按「工具」(Claude Code / Codex / OpenCode ...) 汇总。

为什么要分别取数：
  - 合并视图 (`ccusage --json`) 的 modelBreakdowns 里花费(cost)按定价表算得最准，
    但它不告诉你某个模型属于哪个工具；
  - 各工具子命令 (`ccusage <tool> --json`) 能给出准确的「工具 -> 模型」归属和 token 总量。
  本脚本因此：token 归属用子命令，cost 用合并视图按模型换算。

用法:
    python3 token_usage.py                       # 全部历史
    python3 token_usage.py --since 2026-06-01    # 限定起始日期 (跑得快)
    python3 token_usage.py --since 2026-06-01 --until 2026-06-09
    python3 token_usage.py --offline             # 离线：只用本地缓存价格，查不到则不显示花费
    python3 token_usage.py -v                    # 回显 ccusage 原始输出 (写到 stderr)
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import unicodedata
from collections import defaultdict

CCUSAGE = ["ccusage"]
TOOL_NAMES = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode"}
SMALL_TOKEN_THRESHOLD = 50_000  # 低于此 token 量的模型并入「其它」行


# ──────────────────────────── ccusage 调用 ────────────────────────────
def run_ccusage(extra: list[str], common: list[str], verbose: bool = False) -> dict:
    """调用 ccusage 并解析 JSON。stdout 是 JSON，警告在 stderr。
    verbose=True 时把命令、stderr 警告、原始 stdout 回显到本进程 stderr。"""
    cmd = CCUSAGE + extra + common + ["--json"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if verbose:
        print(f"\n$ {' '.join(cmd)}", file=sys.stderr)
        if proc.stderr.strip():
            print("--- ccusage stderr ---", file=sys.stderr)
            print(proc.stderr.rstrip(), file=sys.stderr)
        print("--- ccusage stdout ---", file=sys.stderr)
        print(proc.stdout.rstrip(), file=sys.stderr)
        print("--- end ---", file=sys.stderr)
    if not proc.stdout.strip():
        sys.exit(f"ccusage 没有输出 JSON。命令: {' '.join(cmd)}\nstderr:\n{proc.stderr}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        sys.exit(f"解析 ccusage JSON 失败: {e}\n原始输出前 500 字符:\n{proc.stdout[:500]}")


# ──────────────────────────── 数据聚合 ────────────────────────────
def aggregate_by_model(combined: dict) -> dict:
    """从合并视图按模型汇总（cost 在这里最准）。"""
    by_model: dict[str, dict] = {}
    for day in combined.get("daily", []):
        for m in day.get("modelBreakdowns", []):
            name = m.get("modelName", "unknown")
            b = by_model.setdefault(name, {"total": 0, "cost": 0.0})
            b["total"] += ((m.get("inputTokens", 0) or 0) + (m.get("outputTokens", 0) or 0)
                           + (m.get("cacheCreationTokens", 0) or 0) + (m.get("cacheReadTokens", 0) or 0))
            b["cost"] += m.get("cost", 0) or 0
    return by_model


def detect_tools(combined: dict) -> list[str]:
    tools: set[str] = set()
    for day in combined.get("daily", []):
        tools.update(day.get("metadata", {}).get("agents", []))
    return sorted(tools)


def collect_tool_models(day: dict) -> set[str]:
    """从一条子命令 daily 记录取出用到的模型名。各工具 schema 不同，三种字段都兼容。"""
    models: set[str] = set(day.get("modelsUsed", []) or [])
    for m in day.get("modelBreakdowns", []) or []:
        if m.get("modelName"):
            models.add(m["modelName"])
    if isinstance(day.get("models"), dict):
        models.update(day["models"].keys())
    return models


def aggregate_by_tool(common, tools, by_model, verbose=False):
    """token 取各工具子命令的 totals（最准）；cost 从 by_model 归属到工具。
    返回 (by_tool, model_to_tools)。"""
    tool_token_totals: dict[str, int] = {}
    tool_models: dict[str, set[str]] = {}
    for tool in tools:
        data = run_ccusage([tool], common, verbose)
        tool_token_totals[tool] = data.get("totals", {}).get("totalTokens", 0) or 0
        models: set[str] = set()
        for day in data.get("daily", []):
            models |= collect_tool_models(day)
        tool_models[tool] = models

    model_to_tools: dict[str, set[str]] = defaultdict(set)
    for tool, models in tool_models.items():
        for m in models:
            model_to_tools[m].add(tool)

    by_tool = {t: {"total": tool_token_totals[t], "cost": 0.0} for t in tools}
    by_tool["(未归属)"] = {"total": 0, "cost": 0.0}
    for model, bucket in by_model.items():
        owners = model_to_tools.get(model)
        if not owners:
            by_tool["(未归属)"]["cost"] += bucket["cost"]
            continue
        share = bucket["cost"] / len(owners)
        for t in owners:
            by_tool[t]["cost"] += share

    by_tool = {t: v for t, v in by_tool.items() if v["total"] or v["cost"]}
    return by_tool, model_to_tools


# ──────────────────────────── 格式化 ────────────────────────────
def human_tokens(n: int) -> str:
    """token 量转人类可读，并在缩写后保留千分位原始值。"""
    raw = f"{n:,}"
    if n >= 100_000_000:
        return f"{n / 100_000_000:.2f}亿({raw})"
    if n >= 10_000_000:
        return f"{n / 10_000_000:.2f}千万({raw})"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万({raw})"
    return raw


def cost_cell(cost: float, tokens: int, offline: bool, name: str = "") -> str:
    if cost > 0:
        return f"${cost:,.2f}"
    if tokens > 0 and "free" not in name.lower():
        return "-" if offline else "$0.00 ⚠️"   # 缺定价：离线不显示，在线标记
    return "$0.00"


def tool_disp(t: str) -> str:
    return TOOL_NAMES.get(t, t.capitalize())


def model_disp(m: str) -> str:
    return re.sub(r"-\d{8}$", "", m)  # 去掉日期后缀，如 claude-haiku-4-5-20251001


def disp_width(s: str) -> int:
    """显示宽度：CJK 宽字符 / 常用 emoji 记 2，变体选择符记 0。"""
    w = 0
    for ch in s:
        if ch == "️":
            continue
        if unicodedata.east_asian_width(ch) in ("W", "F") or ch in "⚠✅📊":
            w += 2
        else:
            w += 1
    return w


def render_table(headers, aligns, rows) -> str:
    cols = len(headers)
    widths = [disp_width(headers[i]) for i in range(cols)]
    for r in rows:
        for i in range(cols):
            widths[i] = max(widths[i], disp_width(r[i]))

    def hline(left, mid, right):
        return left + mid.join("─" * (widths[i] + 2) for i in range(cols)) + right

    def fmt_row(cells, center=False):
        out = []
        for i in range(cols):
            c = cells[i]
            pad = widths[i] - disp_width(c)
            if center:
                lft = pad // 2
                s = " " * lft + c + " " * (pad - lft)
            elif aligns[i] == "r":
                s = " " * pad + c
            else:
                s = c + " " * pad
            out.append(" " + s + " ")
        return "│" + "│".join(out) + "│"

    lines = [hline("┌", "┬", "┐"), fmt_row(headers, center=True), hline("├", "┼", "┤")]
    for idx, r in enumerate(rows):
        lines.append(fmt_row(r))
        lines.append(hline("├", "┼", "┤") if idx != len(rows) - 1 else hline("└", "┴", "┘"))
    return "\n".join(lines)


# ──────────────────────────── 主流程 ────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description="按工具 / 按模型 汇总 ccusage token 消耗")
    ap.add_argument("--since", help="起始日期 YYYY-MM-DD")
    ap.add_argument("--until", help="结束日期 YYYY-MM-DD (含)")
    ap.add_argument("--offline", action="store_true",
                    help="离线模式：只用本地缓存价格，查不到则花费显示为 -")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="回显每次 ccusage 调用的命令、stderr 警告与原始 stdout (写到 stderr)")
    args = ap.parse_args()

    common: list[str] = []
    if args.since:
        common += ["--since", args.since]
    if args.until:
        common += ["--until", args.until]
    if args.offline:
        common += ["--offline"]

    combined = run_ccusage([], common, args.verbose)
    by_model = aggregate_by_model(combined)
    tools = detect_tools(combined)
    by_tool, model_to_tools = aggregate_by_tool(common, tools, by_model, args.verbose)

    # ── 按工具 ──
    tool_items = sorted(by_tool.items(), key=lambda kv: kv[1]["cost"], reverse=True)
    tot_tok = sum(v["total"] for v in by_tool.values())
    tot_cost = sum(v["cost"] for v in by_tool.values())
    tool_rows = []
    for t, v in tool_items:
        pct = (v["cost"] / tot_cost * 100) if tot_cost else 0
        tool_rows.append([tool_disp(t), human_tokens(v["total"]), f"${v['cost']:,.2f}", f"{pct:.0f}%"])
    tool_rows.append(["合计", human_tokens(tot_tok), f"${tot_cost:,.2f}", "100%"])
    print("✅ 按工具汇总 (by tool)")
    print(render_table(["工具", "总 Token", "花费 (USD)", "占比"], ["l", "r", "r", "r"], tool_rows))

    # ── 按模型 ──
    def tool_of(names: set[str]) -> str:
        ts: set[str] = set()
        for n in names:
            ts |= model_to_tools.get(n, set())
        return "/".join(sorted(tool_disp(t) for t in ts)) if ts else "-"

    items = sorted(by_model.items(), key=lambda kv: (kv[1]["cost"], kv[1]["total"]), reverse=True)
    big = [(m, v) for m, v in items if v["total"] >= SMALL_TOKEN_THRESHOLD]
    small = [(m, v) for m, v in items if v["total"] < SMALL_TOKEN_THRESHOLD]
    m_tok = sum(v["total"] for _, v in items)
    m_cost = sum(v["cost"] for _, v in items)

    model_rows = []
    for m, v in big:
        model_rows.append([model_disp(m), tool_of({m}), human_tokens(v["total"]),
                           cost_cell(v["cost"], v["total"], args.offline, m)])
    if small:
        shown = [model_disp(m) for m, _ in small[:3]]
        label = "其它(" + "/".join(shown) + ("…" if len(small) > 3 else "") + ")"
        s_tok = sum(v["total"] for _, v in small)
        s_cost = sum(v["cost"] for _, v in small)
        model_rows.append([label, tool_of({m for m, _ in small}), human_tokens(s_tok),
                           cost_cell(s_cost, s_tok, args.offline)])
    model_rows.append(["合计", "", human_tokens(m_tok), f"${m_cost:,.2f}"])
    print("✅ 按模型汇总 (by model)")
    print(render_table(["模型", "工具", "总 Token", "花费"], ["l", "l", "r", "r"], model_rows))


if __name__ == "__main__":
    main()
