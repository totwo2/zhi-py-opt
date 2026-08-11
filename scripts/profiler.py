#!/usr/bin/env python3
"""
selfopt profiler：简易 cProfile 包装，输出热点函数 + 建议优化域。

用法：
  python profiler.py your_script.py          # 直接跑脚本
  python profiler.py -m your_module          # 跑模块
  python profiler.py your_script.py --args   # 传参给目标脚本

输出：
  - 累计耗时 >5% 的热点函数
  - 每hot函数附带建议域（基于函数名/调用模式猜测）
"""
import argparse
import cProfile
import pstats
import sys
import re
from pathlib import Path


# 函数名 → 建议域的启发式映射
_NAME_HINTS = {
    "join": "str-join",
    "concat": "str-join",
    "build_url": "str-join",
    "format_html": "str-join",
    "compile": "regex-precompile",
    "search": "regex-precompile",
    "match": "regex-precompile",
    "findall": "regex-precompile",
    "sort": "sort-small-net",
    "sorted": "sort-small-net",
    "bisect": "sort-small-net",
    "dispatch": "dict-dispatch",
    "handler": "dict-dispatch",
    "lookup": "dict-dispatch",
    "cache": "lru-cache-pure",
    "memoize": "lru-cache-pure",
    "compute": "lru-cache-pure",
    "calculate": "lru-cache-pure",
}


def _suggest_domain(func_name: str) -> str | None:
    """根据函数名猜测建议域。"""
    fn = func_name.lower()
    for keyword, domain in _NAME_HINTS.items():
        if keyword in fn:
            return domain
    return None


def _parse_args():
    parser = argparse.ArgumentParser(description="selfopt profiler")
    parser.add_argument("target", help="脚本路径或模块名（-m 时）")
    parser.add_argument("-m", "--module", action="store_true", help="把 target 当模块名")
    parser.add_argument("--args", nargs=argparse.REPLACE, default=[], help="传给目标脚本的参数")
    parser.add_argument("--threshold", type=float, default=5.0, help="只显示累计耗时占比 >= 阈值（默认 5%%）")
    return parser.parse_args()


def main():
    args = _parse_args()

    if args.module:
        # python -m module
        sys.argv = [args.target] + (args.args or [])
        target_spec = f"-m {args.target}"
    else:
        target_path = Path(args.target)
        if not target_path.exists():
            print(f"错误：文件不存在 {args.target}", file=sys.stderr)
            sys.exit(1)
        sys.argv = [str(target_path)] + (args.args or [])
        target_spec = str(target_path)

    print(f"[profiler] 目标: {target_spec}")
    print(f"[profiler] 阈值: 累计耗时 >= {args.threshold}%")
    print()

    profiler = cProfile.Profile()
    profiler.enable()

    try:
        if args.module:
            import runpy
            runpy.run_module(args.target, run_name="__main__", alter_sys=True)
        else:
            exec(compile(Path(args.target).read_text(encoding="utf-8"), args.target, "exec"), {})
    except SystemExit:
        pass
    except Exception as e:
        print(f"[profiler] 运行出错: {e}", file=sys.stderr)
    finally:
        profiler.disable()

    stats = pstats.Stats(profiler)
    stats.strip_dirs()
    stats.sort_stats("cumulative")

    # 收集热点
    hot = []
    total_time = None
    for func, (cc, nc, tt, ct, callers) in stats.stats.items():
        if total_time is None:
            total_time = tt  # 总耗时（近似）
        if total_time and total_time > 0:
            pct = (tt / total_time) * 100
        else:
            pct = 0.0
        if pct >= args.threshold:
            func_name = f"{func[2]}({func[1]}:{func[0]})"
            domain = _suggest_domain(func[2])
            hot.append((pct, func_name, domain, tt, ct))

    if not hot:
        print("没有函数达到阈值。试试 --threshold 1.0 看更全的结果。")
        return

    print(f"{'累计%':>8}  {'自身ms':>10}  {'调用次数':>8}  函数  [建议域]")
    print("-" * 80)
    for pct, fn, domain, tt, ct in sorted(hot, key=lambda x: -x[0]):
        domain_str = domain or "-"
        print(f"{pct:>7.1f}%  {tt*1000:>9.2f}ms  {ct:>8}  {fn}  [{domain_str}]")

    print()
    print(f"[profiler] 总计 {len(hot)} 个热点")
    print("[profiler] 建议：对高累计%函数运行 analyzer.py 定位具体优化域")


if __name__ == "__main__":
    main()
