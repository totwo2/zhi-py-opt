#!/usr/bin/env python3
"""
端到端成长测试：验证引子能否健康发芽
完整生命周期：未知域出现 → 候选积累 → 蒸馏信号触发 → 新域入库 → 后续成功复用 → 坏重写被拦
"""
import os
import sys
import time as _time
import shutil
import tempfile

# 测试用临时数据目录隔离，避免污染真实库
_TMP = tempfile.mkdtemp(prefix="selfopt_test_")
os.environ["SELFOPT_DATA_DIR"] = _TMP

sys.path.insert(0, os.path.dirname(__file__))
import selfopt

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOM_FILE = os.path.join(SKILL, "domains.json")
BAK_FILE = os.path.join(SKILL, "domains.json.bak")

# ---- 被测函数：epoch → 时间字符串 ----

def ts_format_old(epoch):
    # 冗余：localtime 调 6 次（每次取一个字段）
    return (f"{_time.localtime(epoch).tm_year:04d}-"
            f"{_time.localtime(epoch).tm_mon:02d}-"
            f"{_time.localtime(epoch).tm_mday:02d} "
            f"{_time.localtime(epoch).tm_hour:02d}:"
            f"{_time.localtime(epoch).tm_min:02d}:"
            f"{_time.localtime(epoch).tm_sec:02d}")

def ts_format_new(epoch):
    # 优化：localtime 只调 1 次
    t = _time.localtime(epoch)
    return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d} {t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d}"

def ts_format_bad(epoch):
    # 格式不等价（用 / 不用 -，丢时分秒）
    t = _time.localtime(epoch)
    return f"{t.tm_year}/{t.tm_mon}/{t.tm_mday}"


def main():
    # 清理上次崩溃残留的 .bak
    if os.path.exists(BAK_FILE):
        shutil.move(BAK_FILE, DOM_FILE)

    shutil.copy2(DOM_FILE, BAK_FILE)
    selfopt.LIB.unlink(missing_ok=True)
    selfopt.CAND.unlink(missing_ok=True)

    try:
        _run_test()
    finally:
        shutil.move(BAK_FILE, DOM_FILE)
        shutil.rmtree(_TMP, ignore_errors=True)
        print("\n  (domains.json 已恢复，临时数据目录已清理)")


def _run_test():
    print("=" * 60)
    print("  成长测试：ts-format 域从无到有")
    print("=" * 60)

    # Phase 1: 未知域，连续 3 次 → 候选积累
    print("\n▶ Phase 1: 未知域，连续 3 次 adopt → 候选积累")
    for i in range(3):
        r = selfopt.adopt(f"ts_fmt_v{i+1}", ts_format_old, ts_format_new, "ts-format")
        print(f"  尝试 {i+1}: stage={r.get('stage','ok')}, note={r.get('note','')}")
        assert r["stage"] == "domain", f"Phase 1 失败：期望 stage=domain，得到 {r}"

    # Phase 2: 蒸馏信号
    print("\n▶ Phase 2: growth_signals 检查蒸馏信号")
    sigs = selfopt.growth_signals()
    for s in sigs:
        flag = " ← 达到阈值，该蒸馏了" if s["ready"] else ""
        print(f"  {s['domain_id']}: 候选 {s['count']} 次, ready={s['ready']}{flag}")
    assert any(s["ready"] for s in sigs), "蒸馏信号未触发！"

    # Phase 3: 蒸馏新域
    print("\n▶ Phase 3: 蒸馏新域 → add_domain + 热重载")
    new_domain = {
        "id": "ts-format",
        "name": "时间戳格式化",
        "scenario": "把 epoch 整数格式化成可读时间字符串",
        "witness": {"kind": "rand_int", "lo": 0, "hi": 2000000000},
        "complete": False,
        "rewrite_hint": "消除冗余 localtime 调用，只调一次",
        "min_speedup": 1.05
    }
    r = selfopt.add_domain(new_domain)
    print(f"  {r}")
    assert r["ok"], "add_domain 失败！"
    assert r["total_domains"] == 7, f"域总数应为 7，得到 {r['total_domains']}"

    # 幂等
    r2 = selfopt.add_domain(new_domain)
    print(f"  幂等测试: {r2}")
    assert not r2["ok"], "幂等保护失效！"

    # Phase 4: 新域生效，重试 → 应通过验证 + benchmark
    print("\n▶ Phase 4: 新域生效后重试 adopt")
    r = selfopt.adopt("ts_fmt_final", ts_format_old, ts_format_new, "ts-format")
    print(f"  结果: ok={r.get('ok')}, speedup={r.get('speedup')}, "
          f"stage={r.get('stage','ok')}, witness={r.get('witness')}")
    assert r.get("ok"), f"新域 adopt 未通过！详情: {r}"

    # Phase 5: 坏重写 → 等价验证拦住
    print("\n▶ Phase 5: 坏重写测试（格式不等价）")
    r = selfopt.adopt("ts_fmt_bad", ts_format_old, ts_format_bad, "ts-format")
    print(f"  结果: stage={r['stage']}, note={r['note']}")
    assert r["stage"] == "verify", "坏重写没被拦住！"

    # Phase 6: 报告
    print("\n▶ Phase 6: 最终报告")
    selfopt.report()

    print("\n" + "=" * 60)
    print("  全部断言通过：引子健康发芽 ✓")
    print("=" * 60)


if __name__ == "__main__":
    main()
