#!/usr/bin/env python3
"""
查找指定目录下的重复文件（基于 MD5 计算）

用法:
    python find_duplicates.py <目录路径>

示例:
    python find_duplicates.py /home/user/documents
    python find_duplicates.py .

原理:
    1. 递归遍历目录下所有文件（不跟随符号链接）
    2. 先按文件大小分组，仅对大小相同的文件计算 MD5
    3. 大文件分块读取（8KB），避免内存溢出
    4. 按 MD5 值分组，输出重复文件组及可回收空间
"""
import argparse
import hashlib
import os
import sys
from collections import defaultdict
from pathlib import Path


def compute_md5(filepath, chunk_size=8192):
    h = hashlib.md5()
    try:
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
    except (PermissionError, OSError) as e:
        print(f"[WARN] Cannot read {filepath}: {e}", file=sys.stderr)
        return None
    return h.hexdigest()


def find_duplicates(directory):
    size_map = defaultdict(list)
    for root, _, files in os.walk(directory, followlinks=False):
        for name in files:
            filepath = os.path.join(root, name)
            try:
                size = os.path.getsize(filepath)
            except OSError as e:
                print(f"[WARN] Cannot stat {filepath}: {e}", file=sys.stderr)
                continue
            size_map[size].append(filepath)

    md5_map = defaultdict(list)
    for size, paths in size_map.items():
        if len(paths) < 2:
            continue
        for filepath in paths:
            md5 = compute_md5(filepath)
            if md5:
                md5_map[(size, md5)].append(filepath)

    duplicates = {k: v for k, v in md5_map.items() if len(v) >= 2}
    return duplicates


def main():
    parser = argparse.ArgumentParser(description="Find duplicate files by MD5")
    parser.add_argument("directory", type=str, help="Directory to scan")
    args = parser.parse_args()

    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"Error: {args.directory} is not a directory", file=sys.stderr)
        sys.exit(1)

    print(f"Scanning: {directory.resolve()}")
    duplicates = find_duplicates(str(directory))

    if not duplicates:
        print("No duplicate files found.")
        return

    total_wasted = 0
    for idx, ((size, md5), paths) in enumerate(duplicates.items(), 1):
        total_wasted += size * (len(paths) - 1)
        print(f"\n--- Group {idx} (MD5: {md5}, size: {size} bytes) ---")
        for p in paths:
            print(f"  {p}")

    print(f"\n{len(duplicates)} group(s) of duplicates found.")
    print(f"Wasted space: {total_wasted} bytes (~{total_wasted / 1024 / 1024:.2f} MB)")


if __name__ == "__main__":
    main()
