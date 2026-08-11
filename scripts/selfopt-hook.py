#!/usr/bin/env python3
"""
selfopt 触发器（示例）：把"手动喂 py"变成"做完工作自动跑"。

这个脚本本身不依赖任何宿主 Agent 的私有机制——宿主只需在
合适的事件上调用它即可：
  - WorkBuddy / OpenClaw：挂到 'session end' 或 'after tool write' hook
  - LangChain/CrewAI：在工具执行后的 callback 里调用 selfopt.auto_scan()
  - 最轻量：写入 Agent 的常驻记忆，让它每次产出 Python 代码后主动跑

两种用法：
  [自动模式] python selfopt-hook.py
      → 非交互，auto_scan 默认只看 'high'（自动挡误报），把热点清单交回 Agent
        决定。SELFOPT_SCAN_ALL=1 看全部（含 low 待研判）；SELFOPT_SCAN_ROOT=路径 指定根。
  [友好交互模式] python selfopt-hook.py --ask
      → 走"两句话"流程：先问"目标还是全量"（模糊路径/函数名），扫完把热点按
        高频聚合，再问"要优化哪几个"。适合人也在场的手动触发。
        （宿主 Agent 应把两处 input() 换成自家对话框，问法不变。）

无论哪种，都只"标热点 + 交回"，绝不未经 adopt 把关就改源码。
"""
import os
import sys

SKILL_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SKILL_SCRIPTS)
import selfopt


def main():
    root = os.environ.get("SELFOPT_SCAN_ROOT")  # 可指定扫描根，默认 cwd

    # 友好交互模式：先问目标/全量，再问高频是否优化
    if "--ask" in sys.argv:
        selfopt.interactive_scan(root)
        return

    # 默认 high（自动模式只报高置信，降误报）；SELFOPT_SCAN_ALL=1 看全部
    min_conf = "low" if os.environ.get("SELFOPT_SCAN_ALL") else "high"
    findings = selfopt.auto_scan(root, min_confidence=min_conf)
    if not findings:
        print("[selfopt] 未扫描到可优化热点（或仅 low 置信）。")
        return
    print(f"[selfopt] 扫描到 {len(findings)} 个热点（{min_conf} 置信），建议优化：")
    for f in findings:
        print(f"  {f['file']}:{f['line']} [{f['domain']}/{f['confidence']}] "
              f"{f['function']} —— {f['hint']}")
    print("[selfopt] 在以上位置生成更快版本后，用 selfopt.adopt() 把关入库。")


if __name__ == "__main__":
    main()
