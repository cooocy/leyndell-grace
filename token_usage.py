#!/usr/bin/env python3
"""
token_usage.py - 按工具 / 按模型汇总 coding agent 的 token 消耗与估算花费。

直接读取 Claude Code、Codex、OpenCode 的本地数据，不依赖 ccusage。

用法:
    python3 token_usage.py
    python3 token_usage.py --since 2026-06-01
    python3 token_usage.py --since 2026-06-01 --until 2026-06-09
    python3 token_usage.py --offline
    python3 token_usage.py -v
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

TOOL_NAMES = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode"}
SMALL_TOKEN_THRESHOLD = 50_000
PRICE_CACHE_TTL_SECONDS = 24 * 60 * 60
LITELLM_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
MODELS_DEV_PRICING_URL = "https://models.dev/api.json"
SEMVER_PREFIX = re.compile(r"^\d+\.\d+\.\d+")


@dataclass(frozen=True)
class UsageRecord:
    timestamp: datetime
    tool: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    provider: str = ""
    recorded_cost: float = 0.0
    cache_creation_1h_tokens: int = 0
    speed: str = ""
    message_id: str = ""
    request_id: str = ""
    is_sidechain: bool = False

    def known_total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_tokens
            + self.cache_read_tokens
        )


@dataclass
class ScanStats:
    files: int = 0
    records: int = 0
    skipped: int = 0
    malformed: int = 0


def verbose_log(enabled: bool, message: str) -> None:
    if enabled:
        print(message, file=sys.stderr)


def as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    return 0


def parse_timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def local_date(timestamp: datetime) -> str:
    return timestamp.astimezone().date().isoformat()


def date_in_range(timestamp: datetime, since: Optional[str], until: Optional[str]) -> bool:
    date = local_date(timestamp)
    return (since is None or date >= since) and (until is None or date <= until)


def expand_path(raw: str) -> Path:
    return Path(os.path.expanduser(raw)).resolve()


def recursive_files(root: Path, suffix: str) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(path for path in root.rglob(f"*{suffix}") if path.is_file())


def claude_config_paths() -> list[Path]:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if configured is not None:
        paths = []
        for raw in configured.split(","):
            if not raw.strip():
                continue
            path = expand_path(raw.strip())
            if path.name == "projects" and path.is_dir():
                path = path.parent
            if (path / "projects").is_dir() and path not in paths:
                paths.append(path)
        if not paths:
            raise RuntimeError(
                "CLAUDE_CONFIG_DIR 中没有有效目录；目录应包含 projects/，"
                "也可以直接指向 projects/"
            )
        return paths

    home = Path.home()
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    return [
        path
        for path in (xdg / "claude", home / ".claude")
        if (path / "projects").is_dir()
    ]


def unwrap_claude_entry(raw: dict[str, Any]) -> dict[str, Any]:
    nested = raw.get("data")
    if isinstance(nested, dict):
        progress = nested.get("message")
        if isinstance(progress, dict) and isinstance(progress.get("message"), dict):
            return progress
    return raw


def valid_claude_entry(entry: dict[str, Any], message: dict[str, Any]) -> bool:
    version = entry.get("version")
    if isinstance(version, str) and not SEMVER_PREFIX.match(version):
        return False
    for value in (
        entry.get("sessionId"),
        entry.get("requestId"),
        message.get("id"),
        message.get("model"),
    ):
        if value == "":
            return False
    return True


def claude_record(raw: dict[str, Any]) -> Optional[UsageRecord]:
    entry = unwrap_claude_entry(raw)
    message = entry.get("message")
    if not isinstance(message, dict) or not valid_claude_entry(entry, message):
        return None
    usage = message.get("usage")
    timestamp = parse_timestamp(entry.get("timestamp"))
    if not isinstance(usage, dict) or timestamp is None:
        return None

    model = message.get("model")
    if not isinstance(model, str) or not model or model == "<synthetic>":
        return None
    cache_detail = usage.get("cache_creation")
    cache_5m = cache_1h = 0
    if isinstance(cache_detail, dict):
        cache_5m = as_int(cache_detail.get("ephemeral_5m_input_tokens"))
        cache_1h = as_int(cache_detail.get("ephemeral_1h_input_tokens"))
        cache_creation = cache_5m + cache_1h
    else:
        cache_creation = as_int(usage.get("cache_creation_input_tokens"))

    record = UsageRecord(
        timestamp=timestamp,
        tool="claude",
        model=model,
        input_tokens=as_int(usage.get("input_tokens")),
        output_tokens=as_int(usage.get("output_tokens")),
        cache_creation_tokens=cache_creation,
        cache_read_tokens=as_int(usage.get("cache_read_input_tokens")),
        recorded_cost=float(entry.get("costUSD") or 0),
        cache_creation_1h_tokens=cache_1h,
        speed=usage.get("speed") if isinstance(usage.get("speed"), str) else "",
        message_id=message.get("id") if isinstance(message.get("id"), str) else "",
        request_id=entry.get("requestId") if isinstance(entry.get("requestId"), str) else "",
        is_sidechain=entry.get("isSidechain") is True,
    )
    return replace(record, total_tokens=record.known_total())


def prefer_claude_record(candidate: UsageRecord, existing: UsageRecord) -> bool:
    if candidate.is_sidechain != existing.is_sidechain:
        return existing.is_sidechain
    if candidate.total_tokens != existing.total_tokens:
        return candidate.total_tokens > existing.total_tokens
    if candidate.recorded_cost != existing.recorded_cost:
        return candidate.recorded_cost > existing.recorded_cost
    return bool(candidate.speed and not existing.speed)


def load_claude_records(verbose: bool) -> tuple[list[UsageRecord], ScanStats]:
    stats = ScanStats()
    records: list[UsageRecord] = []
    exact_indexes: dict[tuple[str, str], int] = {}
    message_indexes: dict[str, list[int]] = defaultdict(list)
    paths = claude_config_paths()
    files = [
        file
        for config in paths
        for file in recursive_files(config / "projects", ".jsonl")
    ]
    stats.files = len(files)

    for path in files:
        try:
            lines = path.open(encoding="utf-8", errors="replace")
        except OSError:
            stats.skipped += 1
            continue
        with lines:
            for line in lines:
                if '"usage":{' not in line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    stats.malformed += 1
                    continue
                record = claude_record(raw)
                if record is None:
                    stats.skipped += 1
                    continue

                index: Optional[int] = None
                if record.message_id:
                    index = exact_indexes.get((record.message_id, record.request_id))
                    if index is None:
                        for candidate_index in message_indexes.get(record.message_id, []):
                            existing = records[candidate_index]
                            if record.is_sidechain or existing.is_sidechain:
                                index = candidate_index
                                break
                if index is not None:
                    if prefer_claude_record(record, records[index]):
                        records[index] = record
                        exact_indexes[(record.message_id, record.request_id)] = index
                    continue

                index = len(records)
                records.append(record)
                if record.message_id:
                    exact_indexes[(record.message_id, record.request_id)] = index
                    message_indexes[record.message_id].append(index)

    stats.records = len(records)
    verbose_log(
        verbose,
        f"[Claude Code] paths={len(paths)} files={stats.files} "
        f"records={stats.records} skipped={stats.skipped} malformed={stats.malformed}",
    )
    return records, stats


def codex_home_paths() -> list[Path]:
    configured = os.environ.get("CODEX_HOME")
    if configured is None:
        return [Path.home() / ".codex"]
    return [expand_path(raw.strip()) for raw in configured.split(",") if raw.strip()]


def codex_files() -> list[Path]:
    files: list[Path] = []
    seen: set[tuple[Path, Path]] = set()
    for home in codex_home_paths():
        roots = [path for path in (home / "sessions", home / "archived_sessions") if path.is_dir()]
        if not roots:
            roots = [home]
        for root in roots:
            for path in recursive_files(root, ".jsonl"):
                relative = path.relative_to(root)
                key = (home.resolve(), relative)
                if key not in seen:
                    seen.add(key)
                    files.append(path)
    return sorted(files)


def subtract_usage(current: dict[str, Any], previous: Optional[dict[str, Any]]) -> dict[str, int]:
    previous = previous or {}
    keys = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    )
    return {key: max(0, as_int(current.get(key)) - as_int(previous.get(key))) for key in keys}


def codex_usage_record(
    timestamp: datetime,
    model: str,
    usage: dict[str, Any],
) -> Optional[UsageRecord]:
    raw_input = as_int(usage.get("input_tokens"))
    cached = min(as_int(usage.get("cached_input_tokens")), raw_input)
    output = as_int(usage.get("output_tokens"))
    reasoning = as_int(usage.get("reasoning_output_tokens"))
    total = as_int(usage.get("total_tokens")) or raw_input + output
    if raw_input == 0 and output == 0 and reasoning == 0:
        return None
    return UsageRecord(
        timestamp=timestamp,
        tool="codex",
        model=model or "gpt-5",
        input_tokens=raw_input - cached,
        output_tokens=output,
        cache_read_tokens=cached,
        reasoning_tokens=reasoning,
        total_tokens=total,
    )


def read_codex_file(path: Path, stats: ScanStats) -> Iterator[UsageRecord]:
    previous_totals: Optional[dict[str, Any]] = None
    current_model = ""
    try:
        lines = path.open(encoding="utf-8", errors="replace")
    except OSError:
        stats.skipped += 1
        return

    with lines:
        for line in lines:
            if '"type":"turn_context"' not in line and '"type":"event_msg"' not in line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                stats.malformed += 1
                continue
            payload = raw.get("payload")
            if not isinstance(payload, dict):
                continue
            if raw.get("type") == "turn_context":
                model = payload.get("model") or payload.get("model_name")
                if isinstance(model, str) and model:
                    current_model = model
                continue
            if raw.get("type") != "event_msg" or payload.get("type") != "token_count":
                continue
            timestamp = parse_timestamp(raw.get("timestamp"))
            info = payload.get("info")
            if timestamp is None or not isinstance(info, dict):
                stats.skipped += 1
                continue
            total_usage = info.get("total_token_usage")
            usage = info.get("last_token_usage")
            if not isinstance(usage, dict):
                if not isinstance(total_usage, dict):
                    continue
                usage = subtract_usage(total_usage, previous_totals)
            if isinstance(total_usage, dict):
                previous_totals = total_usage
            model = payload.get("model") or payload.get("model_name") or info.get("model")
            if isinstance(model, str) and model:
                current_model = model
            record = codex_usage_record(timestamp, current_model, usage)
            if record is not None:
                yield record


def load_codex_records(verbose: bool) -> tuple[list[UsageRecord], ScanStats]:
    stats = ScanStats()
    files = codex_files()
    stats.files = len(files)
    records: list[UsageRecord] = []
    seen: set[tuple[Any, ...]] = set()
    for path in files:
        for record in read_codex_file(path, stats):
            key = (
                record.timestamp,
                record.model,
                record.input_tokens,
                record.cache_read_tokens,
                record.output_tokens,
                record.reasoning_tokens,
                record.total_tokens,
            )
            if key in seen:
                continue
            seen.add(key)
            records.append(record)
    stats.records = len(records)
    verbose_log(
        verbose,
        f"[Codex] homes={len(codex_home_paths())} files={stats.files} "
        f"records={stats.records} skipped={stats.skipped} malformed={stats.malformed}",
    )
    return records, stats


def opencode_paths() -> list[Path]:
    configured = os.environ.get("OPENCODE_DATA_DIR")
    if configured is not None:
        return [expand_path(raw.strip()) for raw in configured.split(",") if raw.strip()]
    return [Path.home() / ".local" / "share" / "opencode"]


def opencode_db_path(root: Path) -> Optional[Path]:
    default = root / "opencode.db"
    if default.is_file():
        return default
    candidates = sorted(
        path
        for path in root.glob("opencode-*.db")
        if re.fullmatch(r"opencode-[A-Za-z0-9_-]+\.db", path.name)
    )
    return candidates[0] if candidates else None


def opencode_record(
    raw: dict[str, Any],
    message_id: str = "",
) -> Optional[UsageRecord]:
    if raw.get("role") not in (None, "assistant"):
        return None
    tokens = raw.get("tokens")
    model = raw.get("modelID")
    provider = raw.get("providerID")
    created = (raw.get("time") or {}).get("created") if isinstance(raw.get("time"), dict) else None
    if not isinstance(tokens, dict) or not isinstance(model, str) or not model:
        return None
    if not isinstance(created, (int, float)):
        return None
    timestamp = datetime.fromtimestamp(created / 1000).astimezone()
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    raw_output = as_int(tokens.get("output"))
    known = (
        as_int(tokens.get("input"))
        + raw_output
        + as_int(cache.get("write"))
        + as_int(cache.get("read"))
    )
    total = as_int(tokens.get("total"))
    extra = max(0, total - known)
    if total == 0:
        total = known
    if total == 0:
        return None
    return UsageRecord(
        timestamp=timestamp,
        tool="opencode",
        model=model,
        provider=provider if isinstance(provider, str) else "",
        input_tokens=as_int(tokens.get("input")),
        output_tokens=raw_output + extra,
        cache_creation_tokens=as_int(cache.get("write")),
        cache_read_tokens=as_int(cache.get("read")),
        reasoning_tokens=as_int(tokens.get("reasoning")),
        total_tokens=total,
        recorded_cost=float(raw.get("cost") or 0),
        message_id=message_id or (raw.get("id") if isinstance(raw.get("id"), str) else ""),
    )


def load_opencode_records(verbose: bool) -> tuple[list[UsageRecord], ScanStats]:
    stats = ScanStats()
    records: list[UsageRecord] = []
    seen: set[str] = set()
    roots = opencode_paths()
    for root in roots:
        db_path = opencode_db_path(root)
        if db_path is not None:
            stats.files += 1
            try:
                uri = f"file:{db_path}?mode=ro"
                with sqlite3.connect(uri, uri=True) as connection:
                    for message_id, data in connection.execute("SELECT id, data FROM message"):
                        try:
                            raw = json.loads(data)
                        except (TypeError, json.JSONDecodeError):
                            stats.malformed += 1
                            continue
                        record = opencode_record(raw, str(message_id))
                        if record is None:
                            stats.skipped += 1
                            continue
                        if record.message_id and record.message_id in seen:
                            continue
                        if record.message_id:
                            seen.add(record.message_id)
                        records.append(record)
            except sqlite3.Error as error:
                raise RuntimeError(f"无法读取 OpenCode 数据库 {db_path}: {error}") from error

        for path in recursive_files(root / "storage" / "message", ".json"):
            stats.files += 1
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                stats.malformed += 1
                continue
            record = opencode_record(raw)
            if record is None:
                stats.skipped += 1
                continue
            if record.message_id and record.message_id in seen:
                continue
            if record.message_id:
                seen.add(record.message_id)
            records.append(record)

    stats.records = len(records)
    verbose_log(
        verbose,
        f"[OpenCode] paths={len(roots)} files={stats.files} records={stats.records} "
        f"skipped={stats.skipped} malformed={stats.malformed}",
    )
    return records, stats


def price_cache_path() -> Path:
    override = os.environ.get("TOKEN_USAGE_PRICING_FILE")
    if override:
        return expand_path(override)
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache_home / "token-usage" / "pricing.json"


def read_json_url(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "token-usage/1"})
    with urllib.request.urlopen(request, timeout=10) as response:
        data = json.load(response)
    if not isinstance(data, dict):
        raise ValueError(f"价格源不是 JSON object: {url}")
    return data


def load_price_cache(path: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_price_cache(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix="pricing-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=True, separators=(",", ":"))
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def load_pricing(offline: bool, verbose: bool) -> dict[str, Any]:
    path = price_cache_path()
    cached = load_price_cache(path)
    fetched_at = float((cached or {}).get("fetched_at", 0) or 0)
    fresh = cached is not None and time.time() - fetched_at < PRICE_CACHE_TTL_SECONDS
    if offline or fresh:
        verbose_log(
            verbose,
            f"[Pricing] {'offline' if offline else 'cache'} path={path} "
            f"available={cached is not None}",
        )
        return cached or {}

    try:
        verbose_log(verbose, "[Pricing] refreshing LiteLLM and models.dev")
        data = {
            "fetched_at": time.time(),
            "litellm": read_json_url(LITELLM_PRICING_URL),
            "models_dev": read_json_url(MODELS_DEV_PRICING_URL),
        }
        write_price_cache(path, data)
        verbose_log(verbose, f"[Pricing] cache updated: {path}")
        return data
    except (OSError, ValueError, urllib.error.URLError) as error:
        verbose_log(verbose, f"[Pricing] refresh failed, using old cache: {error}")
        return cached or {}


def model_candidates(record: UsageRecord) -> list[str]:
    model = record.model
    if record.tool == "opencode":
        model = {"gemini-3-pro-high": "gemini-3-pro-preview", "k2p6": "kimi-k2.6"}.get(
            model, model
        )
    candidates = [model]
    without_date = re.sub(r"-\d{8}$", "", model)
    if without_date != model:
        candidates.append(without_date)
    if record.speed == "fast" and model.endswith("-fast"):
        candidates.append(model[:-5])
    if record.provider:
        provider = record.provider.replace("-", "_")
        candidates.extend(f"{provider}/{candidate}" for candidate in list(candidates))
    return list(dict.fromkeys(candidates))


def models_dev_price(data: dict[str, Any], candidate: str) -> Optional[dict[str, float]]:
    if "/" not in candidate:
        return None
    provider_hint, model = candidate.split("/", 1)
    provider = next(
        (
            value
            for provider_id, value in data.items()
            if provider_id.replace("-", "_") == provider_hint
        ),
        None,
    )
    if not isinstance(provider, dict):
        return None
    raw = (provider.get("models") or {}).get(model)
    if not isinstance(raw, dict) or not isinstance(raw.get("cost"), dict):
        return None
    cost = raw["cost"]
    return {
        "input_cost_per_token": float(cost.get("input") or 0) / 1_000_000,
        "output_cost_per_token": float(cost.get("output") or 0) / 1_000_000,
        "cache_read_input_token_cost": float(cost.get("cache_read") or 0) / 1_000_000,
        "cache_creation_input_token_cost": float(cost.get("cache_write") or 0) / 1_000_000,
    }


def find_price(pricing: dict[str, Any], record: UsageRecord) -> Optional[dict[str, Any]]:
    litellm = pricing.get("litellm")
    models_dev = pricing.get("models_dev")
    for candidate in model_candidates(record):
        if isinstance(litellm, dict) and isinstance(litellm.get(candidate), dict):
            return litellm[candidate]
    if isinstance(models_dev, dict):
        for candidate in model_candidates(record):
            found = models_dev_price(models_dev, candidate)
            if found is not None:
                return found
    return None


def tiered_rate(price: dict[str, Any], base: str, context_tokens: int) -> float:
    rate = float(price.get(base) or 0)
    for key, value in price.items():
        match = re.fullmatch(re.escape(base) + r"_above_(\d+)k_tokens", key)
        if match and context_tokens > int(match.group(1)) * 1000:
            rate = float(value or rate)
    return rate


def calculate_cost(record: UsageRecord, pricing: dict[str, Any]) -> tuple[float, bool]:
    if record.recorded_cost > 0:
        return record.recorded_cost, True
    price = find_price(pricing, record)
    if price is None:
        return 0.0, False

    context = record.input_tokens + record.cache_creation_tokens + record.cache_read_tokens
    cache_creation_rate = tiered_rate(price, "cache_creation_input_token_cost", context)
    one_hour_rate = float(
        price.get("cache_creation_input_token_cost_above_1hr") or cache_creation_rate
    )
    five_minute_creation = max(
        0, record.cache_creation_tokens - record.cache_creation_1h_tokens
    )
    cost = (
        record.input_tokens * tiered_rate(price, "input_cost_per_token", context)
        + record.output_tokens * tiered_rate(price, "output_cost_per_token", context)
        + record.cache_read_tokens * tiered_rate(
            price, "cache_read_input_token_cost", context
        )
        + five_minute_creation * cache_creation_rate
        + record.cache_creation_1h_tokens * one_hour_rate
    )
    if record.speed == "fast":
        provider_specific = price.get("provider_specific_entry")
        if isinstance(provider_specific, dict):
            cost *= float(provider_specific.get("fast") or 1)
    return cost, True


def aggregate_records(
    records: Iterable[UsageRecord],
    pricing: dict[str, Any],
    since: Optional[str],
    until: Optional[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, set[str]]]:
    by_tool: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"total": 0, "cost": 0.0, "priced": True}
    )
    by_model: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"total": 0, "cost": 0.0, "priced": True}
    )
    model_to_tools: dict[str, set[str]] = defaultdict(set)
    price_cache: dict[tuple[str, str, str, str], Optional[dict[str, Any]]] = {}
    for record in records:
        if not date_in_range(record.timestamp, since, until):
            continue
        price_key = (record.tool, record.model, record.provider, record.speed)
        if record.recorded_cost > 0:
            cost, priced = record.recorded_cost, True
        else:
            if price_key not in price_cache:
                price_cache[price_key] = find_price(pricing, record)
            matched_price = price_cache[price_key]
            cost, priced = calculate_cost(
                record,
                {"litellm": {record.model: matched_price}} if matched_price is not None else {},
            )
        for bucket in (by_tool[record.tool], by_model[record.model]):
            bucket["total"] += record.total_tokens
            bucket["cost"] += cost
            bucket["priced"] = bucket["priced"] and priced
        model_to_tools[record.model].add(record.tool)
    return dict(by_tool), dict(by_model), model_to_tools


def human_tokens(n: int) -> str:
    raw = f"{n:,}"
    if n >= 100_000_000:
        return f"{n / 100_000_000:.2f}亿({raw})"
    if n >= 10_000_000:
        return f"{n / 10_000_000:.2f}千万({raw})"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万({raw})"
    return raw


def cost_cell(cost: float, priced: bool) -> str:
    if not priced:
        return "-"
    if cost > 0:
        return f"${cost:,.2f}"
    return "$0.00"


def tool_disp(tool: str) -> str:
    return TOOL_NAMES.get(tool, tool.capitalize())


def model_disp(model: str) -> str:
    return re.sub(r"-\d{8}$", "", model)


def disp_width(value: str) -> int:
    width = 0
    for character in value:
        if character == "️":
            continue
        if unicodedata.east_asian_width(character) in ("W", "F") or character in "⚠✅📊":
            width += 2
        else:
            width += 1
    return width


def render_table(headers: list[str], aligns: list[str], rows: list[list[str]]) -> str:
    columns = len(headers)
    widths = [disp_width(headers[index]) for index in range(columns)]
    for row in rows:
        for index in range(columns):
            widths[index] = max(widths[index], disp_width(row[index]))

    def hline(left: str, middle: str, right: str) -> str:
        return left + middle.join("─" * (widths[i] + 2) for i in range(columns)) + right

    def format_row(cells: list[str], center: bool = False) -> str:
        output = []
        for index in range(columns):
            cell = cells[index]
            padding = widths[index] - disp_width(cell)
            if center:
                left = padding // 2
                rendered = " " * left + cell + " " * (padding - left)
            elif aligns[index] == "r":
                rendered = " " * padding + cell
            else:
                rendered = cell + " " * padding
            output.append(" " + rendered + " ")
        return "│" + "│".join(output) + "│"

    lines = [
        hline("┌", "┬", "┐"),
        format_row(headers, center=True),
        hline("├", "┼", "┤"),
    ]
    for index, row in enumerate(rows):
        lines.append(format_row(row))
        lines.append(
            hline("├", "┼", "┤") if index != len(rows) - 1 else hline("└", "┴", "┘")
        )
    return "\n".join(lines)


def validate_date(value: Optional[str], option: str) -> Optional[str]:
    if value is None:
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as error:
        raise SystemExit(f"{option} 必须是 YYYY-MM-DD: {value}") from error
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="按工具 / 按模型汇总 coding agent token 消耗")
    parser.add_argument("--since", help="起始日期 YYYY-MM-DD")
    parser.add_argument("--until", help="结束日期 YYYY-MM-DD (含)")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="离线模式：只用本地缓存价格，查不到则花费显示为 -",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="输出扫描统计、跳过记录和价格缓存状态 (写到 stderr)",
    )
    args = parser.parse_args()
    since = validate_date(args.since, "--since")
    until = validate_date(args.until, "--until")
    if since and until and since > until:
        raise SystemExit("--since 不能晚于 --until")

    try:
        claude, _ = load_claude_records(args.verbose)
        codex, _ = load_codex_records(args.verbose)
        opencode, _ = load_opencode_records(args.verbose)
    except RuntimeError as error:
        raise SystemExit(str(error)) from error

    pricing = load_pricing(args.offline, args.verbose)
    by_tool, by_model, model_to_tools = aggregate_records(
        [*claude, *codex, *opencode], pricing, since, until
    )

    tool_items = sorted(by_tool.items(), key=lambda item: item[1]["total"], reverse=True)
    total_tokens = sum(bucket["total"] for bucket in by_tool.values())
    total_cost = sum(bucket["cost"] for bucket in by_tool.values())
    total_priced = all(bucket["priced"] for bucket in by_tool.values())
    priced_cost = sum(
        bucket["cost"] for bucket in by_tool.values() if bucket["priced"]
    )
    tool_rows = []
    for tool, bucket in tool_items:
        percentage = (
            f"{bucket['cost'] / priced_cost * 100:.0f}%"
            if bucket["priced"] and priced_cost
            else ("0%" if bucket["priced"] else "-")
        )
        tool_rows.append(
            [
                tool_disp(tool),
                human_tokens(bucket["total"]),
                cost_cell(bucket["cost"], bucket["priced"]),
                percentage,
            ]
        )
    tool_rows.append(
        [
            "合计",
            human_tokens(total_tokens),
            cost_cell(total_cost, total_priced),
            "100%" if total_priced else "-",
        ]
    )
    print("✅ 按工具汇总 (by tool)")
    print(
        render_table(
            ["工具", "总 Token", "花费 (USD)", "占比"],
            ["l", "r", "r", "r"],
            tool_rows,
        )
    )

    def tool_of(models: set[str]) -> str:
        tools: set[str] = set()
        for model in models:
            tools |= model_to_tools.get(model, set())
        return "/".join(sorted(tool_disp(tool) for tool in tools)) if tools else "-"

    model_items = sorted(by_model.items(), key=lambda item: item[1]["total"], reverse=True)
    big = [
        (model, bucket)
        for model, bucket in model_items
        if bucket["total"] >= SMALL_TOKEN_THRESHOLD
    ]
    small = [
        (model, bucket)
        for model, bucket in model_items
        if bucket["total"] < SMALL_TOKEN_THRESHOLD
    ]
    model_rows = []
    for model, bucket in big:
        model_rows.append(
            [
                model_disp(model),
                tool_of({model}),
                human_tokens(bucket["total"]),
                cost_cell(bucket["cost"], bucket["priced"]),
            ]
        )
    if small:
        shown = [model_disp(model) for model, _ in small[:3]]
        label = "其它(" + "/".join(shown) + ("…" if len(small) > 3 else "") + ")"
        small_tokens = sum(bucket["total"] for _, bucket in small)
        small_cost = sum(bucket["cost"] for _, bucket in small)
        small_priced = all(bucket["priced"] for _, bucket in small)
        model_rows.append(
            [
                label,
                tool_of({model for model, _ in small}),
                human_tokens(small_tokens),
                cost_cell(small_cost, small_priced),
            ]
        )
    model_rows.append(
        ["合计", "", human_tokens(total_tokens), cost_cell(total_cost, total_priced)]
    )
    print("✅ 按模型汇总 (by model)")
    print(
        render_table(
            ["模型", "工具", "总 Token", "花费"],
            ["l", "l", "r", "r"],
            model_rows,
        )
    )


if __name__ == "__main__":
    main()
