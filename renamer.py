#!/usr/bin/env python3
import argparse
import sys
import tempfile
from datetime import datetime
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(
        prog="renamer",
        usage="renamer <directory> [--prefix 前缀] [--suffix 后缀] (--seq 位数 [--begin 起始序号] | --time)",
        description="批量重命名文件",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  python renamer ./photos --prefix trip --seq 3
    trip_001.jpg, trip_002.png, trip_003.txt ...

  python renamer ./photos --seq 2 --begin 5 --suffix "_thumb"
    05_thumb.jpg, 06_thumb.png, 07_thumb.txt ...

  python renamer ./photos --time
    20201112-155909.jpg, 20201112-155909-1.png, 20201112-160015.txt ...

  python renamer ./photos --prefix trip --time --suffix "_backup"
    trip_20201112-155909_backup.jpg, trip_20201112-155909-1_backup.png ...""",
    )

    parser.add_argument("directory", type=Path, help="目标目录")
    parser.add_argument("--prefix", default="", help="文件名前缀，默认为空")
    parser.add_argument("--suffix", default="", help="文件名后缀（扩展名前），默认为空")

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--seq", type=int, help="顺序编号模式，指定位数（1-8）")
    mode.add_argument("--time", action="store_true", help="时间格式模式，输出 yyyyMMdd-HHmmss")

    parser.add_argument("--begin", type=int, default=1, help="起始序号，默认1")

    args = parser.parse_args()

    if args.seq is not None:
        if args.seq < 1 or args.seq > 8:
            parser.error("--seq 必须在 1-8 之间")
        if args.begin < 1:
            parser.error("--begin 必须 >= 1")
        if args.begin >= 10 ** args.seq:
            parser.error(f"--begin ({args.begin}) 必须 < 10^{args.seq} ({10 ** args.seq})")
    else:
        if args.begin != 1:
            parser.error("--begin 仅在 --seq 模式下可用")

    return args


def get_birth_time(path: Path) -> datetime:
    st = path.stat()
    ts = getattr(st, "st_birthtime", None)
    if ts is None:
        ts = st.st_mtime
    return datetime.fromtimestamp(ts)


def build_name(prefix: str, body: str, dup_suffix: str, suffix: str, ext: str) -> str:
    if prefix:
        name = f"{prefix}_{body}"
    else:
        name = body
    name += dup_suffix + suffix + ext
    return name


def rename_seq(files: list[Path], directory: Path, prefix: str, suffix: str, seq: int, begin: int):
    tmp_names = []
    for f in files:
        tmp = f.parent / f"{tempfile.gettempprefix()}_rename_{f.name}"
        f.rename(tmp)
        tmp_names.append(tmp)

    for i, tmp in enumerate(tmp_names):
        num = begin + i
        ext = Path(tmp.name).suffix
        body = str(num).zfill(seq)
        new_name = build_name(prefix, body, "", suffix, ext)
        tmp.rename(directory / new_name)


def rename_time(files: list[Path], directory: Path, prefix: str, suffix: str):
    time_counts: dict[str, int] = {}

    tmp_names = []
    for f in files:
        dt = get_birth_time(f)
        tmp = f.parent / f"{tempfile.gettempprefix()}_rename_{f.name}"
        f.rename(tmp)
        tmp_names.append((tmp, dt))

    for tmp, dt in tmp_names:
        time_str = dt.strftime("%Y%m%d-%H%M%S")
        count = time_counts.get(time_str, 0) + 1
        time_counts[time_str] = count

        dup = f"-{count - 1}" if count > 1 else ""
        ext = Path(tmp.name).suffix
        new_name = build_name(prefix, time_str, dup, suffix, ext)
        tmp.rename(directory / new_name)


def main():
    args = parse_args()

    directory = args.directory.resolve()
    if not directory.is_dir():
        print(f"错误：{directory} 不是有效目录", file=sys.stderr)
        sys.exit(1)

    entries = sorted(directory.iterdir(), key=lambda e: e.name)
    files = [e for e in entries if e.is_file()]
    dirs = [e for e in entries if e.is_dir()]

    if not files:
        print("目录中没有文件", file=sys.stderr)
        sys.exit(0)

    if args.seq is not None:
        rename_seq(files, directory, args.prefix, args.suffix, args.seq, args.begin)
    else:
        rename_time(files, directory, args.prefix, args.suffix)

    print(f"重命名 {len(files)} 个文件；{len(dirs)} 个文件夹已忽略。")


if __name__ == "__main__":
    main()
