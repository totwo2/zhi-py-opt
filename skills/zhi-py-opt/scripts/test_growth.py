#!/usr/bin/env python3
"""
端到端成长测试：验证引子能否健康发芽
完整生命周期：未知域出现 → 候选积累 → 蒸馏信号触发 → 新域入库 → 后续成功复用 → 坏重写被拦
"""
import json
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


def test_math_gate_v2():
    """v2 数学闸门回归测试：边界样本、异常语义、反例返回、
    配对符号检验、交叉点、版本感知。"""
    print("=" * 60)
    print("  数学闸门 v2 回归测试")
    print("=" * 60)

    # T1: 洞1 —— 边界样本抓住"空列表才炸"的错误重写
    print("\n▶ T1: 边界样本抓空列表错误")
    def sum_old(xs):
        s = 0
        for x in xs:
            s += x
        return s
    def sum_buggy(xs):
        return sum(xs) if xs else xs[0]
    r = selfopt.adopt("t1_sum_buggy", sum_old, sum_buggy, "list-to-set-membership")
    print(f"  {r}")
    assert r["stage"] == "verify", f"T1 失败：边界样本没抓住空列表错误 {r}"
    assert r.get("counterexample") == [], f"T1 失败：反例应为空列表 []，得到 {r.get('counterexample')}"

    # T2: 洞2 —— 两个相同函数，噪声翻不出显著性
    print("\n▶ T2: 相同函数噪声拒绝")
    def same_a(xs):
        s = 0
        for x in xs:
            s += x
        return s
    same_b = same_a
    r = selfopt.adopt("t2_same", same_a, same_b, "str-join",
                      samples=[list(range(300)) for _ in range(20)])
    print(f"  {r}")
    assert r["ok"] is False and r["stage"] == "bench", f"T2 失败：相同函数不该入库 {r}"

    # T3: 异常语义 —— 都抛同类异常 = 等价
    print("\n▶ T3: 异常语义等价")
    def old_raises(x):
        if x < 0:
            raise ValueError("neg")
        return x * 2
    def new_raises(x):
        if x < 0:
            raise ValueError("neg")
        return x + x
    ok, cx = selfopt.verify_equivalence(old_raises, new_raises, [-5, 3])
    assert ok and cx is None, f"T3 失败：都抛 ValueError 应判等价 {ok} {cx}"
    def new_diff_exc(x):
        if x < 0:
            raise TypeError("different")
        return x + x
    ok, cx = selfopt.verify_equivalence(old_raises, new_diff_exc, [-5, 3])
    assert not ok and cx == -5, f"T3 失败：异常类型不同应判不等价且反例=-5 {ok} {cx}"
    print("  都抛同类异常=等价 ✓  异常类型不同=不等价且反例=-5 ✓")

    # T4: 交叉点 —— str-join 大规模才赢，n* 应存在
    # 注：str-join 域默认证人是 int 列表，与拼接函数类型不匹配，
    # 用 sample_fn 传真实形态（字符串列表）——SKILL.md"默认证人不够用"同理。
    print("\n▶ T4: 交叉点（跨规模）")
    def join_old(row):
        s = ""
        for x in row:
            s += x + ","
        return s
    def join_new(row):
        return ",".join(row) + ","
    def str_rows(n):
        return [["x" + str(i % 10) for i in range(n)] for _ in range(10)]
    ca = selfopt.crossing_analysis(
        join_old, join_new, "rand_int_list", {}, threshold=1.0,
        sizes=[1, 4, 16, 64, 256, 1024], sample_fn=str_rows)
    print(f"  曲线: {ca['curve']}, n*={ca['n_star']}, scalable={ca['scalable']}")
    assert ca["scalable"], "T4 失败：rand_int_list 应可跨规模分析"
    assert ca["n_star"] is not None, (
        f"T4 失败：join vs += 在大规模应至少不吃亏（CPython {selfopt._CUR_PY_VERSION}）")

    # T5: 版本感知 —— 入库记录带 py_version
    print("\n▶ T5: 版本感知")
    import platform as _pl
    recs = [json.loads(l) for l in selfopt.LIB.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert all(r.get("py_version") == _pl.python_version() for r in recs), \
        f"T5 失败：入库记录应带当前版本 {_pl.python_version()}"
    print(f"  所有入库记录 py_version = {_pl.python_version()} ✓")

    print("\n▶ 数学闸门 v2 全部通过 ✓")



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
    # 动态断言：加域后总数 = 当前域数 + 1（域库会随成长增长，不硬编码数字）
    import json as _json
    with open(DOM_FILE, encoding="utf-8") as f:
        _cur_n = len(_json.load(f)["domains"]) - 1
    assert r["total_domains"] == _cur_n + 1, \
        f"域总数应为 {_cur_n + 1}，得到 {r['total_domains']}"

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


def main():
    # 清理上次崩溃残留的 .bak
    if os.path.exists(BAK_FILE):
        shutil.move(BAK_FILE, DOM_FILE)

    shutil.copy2(DOM_FILE, BAK_FILE)
    selfopt.LIB.unlink(missing_ok=True)
    selfopt.CAND.unlink(missing_ok=True)

    try:
        _run_test()
        test_math_gate_v2()
    finally:
        shutil.move(BAK_FILE, DOM_FILE)
        shutil.rmtree(_TMP, ignore_errors=True)
        print("\n  (domains.json 已恢复，临时数据目录已清理)")


if __name__ == "__main__":
    main()
