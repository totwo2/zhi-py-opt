#!/usr/bin/env python3
"""
selfopt 真实负载实测（bench_real）— 回答「到底能不能提速、提速多少」
====================================================================
【思路笔记 / 立此存照】

## 一、解决什么问题
需求方问（2026-09-12）：「对所有代码扫描，出结果，
**到底能不能提速，提速是多少**」。

静态扫描只能回答"哪里像热点"，不能回答"快多少"——1018 条热点里绝大多数
函数根本不在热路径上，改了也不产生可观测收益。要回答"提速多少"必须实测，
且必须用 selfopt 自己的闸门（配对交替 + 中位数 + 符号检验），否则测出来的
数字跟库里已有记录不可比。

## 二、核心机制
1. **语料真实**：输入取自真实项目里的真实 .py 源文件行，
   不造随机串。同一形态在不同规模下结论可能相反，所以每个域都跑多个规模。
2. **实现真实**：old/new 对从真实代码里抄，不是教科书 toy。
   - regex-precompile ← 2026-07-23-09-16-53/selfopt_pilot.py 的
     naive_extract / opt_extract（该文件本身就写好了成对实现，直接用）
   - list-to-set ← 2026-08-29-09-58-32/interrupt_lab/s13c_llm_segment.py
     parse_lines 里的 `st in ('思维过程','出了结果','自揉')`（**真实是小元组**，
     这是关键：小容器转 set 未必赚，实测就是要把这种"想当然"打掉）
   - str-join / dict-dispatch ← 工作区里的通用形态
3. **判定不变（需求方要求）**：一律走 adopt 闸门，不自己算加速比。
   闸门拒绝 = 这笔优化不成立，报告里如实写"不通过"，不粉饰。
4. **负结果同样是结果**：CPython 3.13 已把 str += 做了原地优化、小元组
   in 有短路，很多"经典技巧"在新版本上已经不赚钱。测出来 <1.0x 就写 <1.0x。

## 三、我的适配点
- 全程 SELFOPT_DATA_DIR 指临时目录，不污染真实 library.jsonl（测量负载不等于真实负载）。
- 输出表 + JSON 双份，便于直接贴进报告。
- 自带 --test：先验等价性，等价性不过的实现对根本不该进 benchmark。
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# 测量隔离：绝不写真实库
_TMP = os.environ.get("SELFOPT_DATA_DIR") or tempfile.mkdtemp(prefix="selfopt-bench-")
os.environ["SELFOPT_DATA_DIR"] = _TMP
import selfopt  # noqa: E402

CORPUS_ROOT = Path(os.environ.get("SELFOPT_CORPUS_ROOT", Path.cwd()))
SIZES = [50, 500, 5000]


# ----------------------------------------------------------------------
# 真实语料
# ----------------------------------------------------------------------

def load_corpus():
    """取真实 Python 源码行作为输入语料（不造随机串）。"""
    cands = [
        CORPUS_ROOT / "2026-08-20-08-58-44/AI-Infra-Guard/services/api_checker/algorithms/bayes_score.py",
        CORPUS_ROOT / "2026-08-20-08-58-44/learn-repos/nanobot/nanobot/webui/transcript.py",
        HERE / "selfopt.py",
        HERE / "analyzer.py",
    ]
    lines = []
    for p in cands:
        if p.exists():
            try:
                lines.extend(p.read_text(encoding="utf-8", errors="replace").splitlines())
            except Exception:
                continue
    if not lines:
        lines = ["def f(x):", "    return x + 1", "class A:", "    pass"] * 100
    return lines


def chunks(lines, n, k=5):
    """切成 k 份、每份 n 行的样本列表（不足则循环补足）。"""
    out = []
    for i in range(k):
        start = (i * n) % max(len(lines) - n, 1)
        out.append(lines[start:start + n])
    return out


# ----------------------------------------------------------------------
# 域 1：regex-precompile（真实来源 selfopt_pilot.py 的成对实现）
# ----------------------------------------------------------------------

_RX = re.compile(r"^\s*(def|class)\s+(\w+)")


def re_naive(lines):
    """反模式：循环内 re.compile（抄自 selfopt_pilot.py:26-33）"""
    names = []
    for line in lines:
        m = re.compile(r"^\s*(def|class)\s+(\w+)").match(line)
        if m:
            names.append((m.group(1), m.group(2)))
    return names


def re_opt(lines):
    names = []
    for line in lines:
        m = _RX.match(line)
        if m:
            names.append((m.group(1), m.group(2)))
    return names


# ----------------------------------------------------------------------
# 域 2：str-join
# ----------------------------------------------------------------------

def join_naive(rows):
    s = ""
    for r in rows:
        s += r + ","
    return s


def join_opt(rows):
    # 等价改写：空输入时 old 返回 ''，这里必须也返回 ''，否则闸门会拒
    return ",".join(rows) + "," if rows else ""


# ----------------------------------------------------------------------
# 域 3：list-to-set-membership（真实形态：s13c_llm_segment.parse_lines）
# ----------------------------------------------------------------------

def _mk_membership(n_cands):
    """造一个含 n_cands 个候选的查表函数对（批量版：一次调用查多个 probe）。
    对应"循环内反复查同一容器"的用法——set 构造成本可被摊薄。"""
    base = ["思维过程", "出了结果", "自揉", "待确认", "已放弃", "重试中"]
    cands = tuple((base * (n_cands // len(base) + 1))[:n_cands])

    def old(pair):
        cands_, probes = pair
        out = []
        for p in probes:
            if p in cands_:
                out.append(p)
        return out

    def new(pair):
        cands_, probes = pair
        s = set(cands_)
        out = []
        for p in probes:
            if p in s:
                out.append(p)
        return out

    return old, new, cands


def _mk_membership_single(n_cands):
    """单次查询版（**这才是 s13c_llm_segment.parse_lines 的真实用法**）：
    每次调用只查一个元素，set 的构造成本无法被摊薄。

    必须分开测：批量版里 set 构造被 N 次查询摊掉，小容器也能"看起来很赚"，
    直接拿那个数字去说'3 元素元组也该转 set'就是误导——真实调用模式下
    set 每次都要重建，反而更慢。这一组专门打掉这种想当然（自我纠错，勿删）。"""
    base = ["思维过程", "出了结果", "自揉", "待确认", "已放弃", "重试中"]
    cands = tuple((base * (n_cands // len(base) + 1))[:n_cands])
    cands_set = set(cands)      # 模块级预构造，相当于"提到循环外"的正确改法

    def old(probe):
        return probe in cands

    def new(probe):
        return probe in cands_set

    return old, new, cands


# ----------------------------------------------------------------------
# 域 4：dict-dispatch
# ----------------------------------------------------------------------

def _mk_dispatch(n_branches):
    """真实 if-elif 展开链 vs dict.get。

    注意：不能用 `for i in range(...)` 去模拟 if-elif 链——那会额外付循环
    开销，把 old 人为做慢，测出来的加速比是假的（第一版就犯了这个错）。
    这里用 exec 生成与真实代码同形的展开 if/elif。"""
    keys = [f"k{i}" for i in range(n_branches)]
    tbl = dict(zip(keys, range(n_branches)))

    src = "def _old(k):\n"
    for i, kk in enumerate(keys):
        kw = "if" if i == 0 else "elif"
        src += f"    {kw} k == {kk!r}:\n        return {i}\n"
    src += "    else:\n        return -1\n"
    ns = {}
    exec(src, ns)
    old = ns["_old"]

    def new(k):
        return tbl.get(k, -1)

    return old, new, keys


# ----------------------------------------------------------------------
# 跑测
# ----------------------------------------------------------------------

def run():
    lines = load_corpus()
    print(f"语料: {len(lines)} 行真实 Python 源码（取自 {CORPUS_ROOT}）")
    print(f"判定: selfopt.adopt 闸门（配对交替×9 + 中位数 + 符号检验 α={selfopt.SIGN_ALPHA}）")
    print(f"Python: {selfopt._CUR_PY_VERSION}\n")
    results = []

    # --- 域1 regex-precompile ---
    for n in SIZES:
        samples = chunks(lines, n)
        r = selfopt.adopt(f"regex-precompile@{n}", re_naive, re_opt,
                          "regex-precompile", samples=samples)
        results.append(("regex-precompile", f"{n}行", r))

    # --- 域2 str-join ---
    for n in SIZES:
        samples = chunks(lines, n)
        r = selfopt.adopt(f"str-join@{n}", join_naive, join_opt,
                          "str-join", samples=samples)
        results.append(("str-join", f"{n}行", r))

    # --- 域3a list-to-set 批量（循环内反复查同一容器，set 成本可摊薄）---
    for n_cands in (3, 20, 200):
        old, new, cands = _mk_membership(n_cands)
        probes = [l.split("(")[0][:8] for l in lines[:200]]
        probes += ["思维过程", "自揉", "不存在的值"]
        samples = [(cands, probes)] * 5
        r = selfopt.adopt(f"list-to-set-batch@{n_cands}", old, new,
                          "list-to-set-membership", samples=samples)
        results.append(("list-to-set[循环内批量]", f"候选{n_cands}个", r))

    # --- 域3b list-to-set 单次（真实用法：一次调用只查一个，set 摊不掉）---
    for n_cands in (3, 20, 200):
        old, new, cands = _mk_membership_single(n_cands)
        probes = [l.split("(")[0][:8] for l in lines[:200]]
        probes += ["思维过程", "自揉", "不存在的值"]
        samples = probes * 3
        r = selfopt.adopt(f"list-to-set-single@{n_cands}", old, new,
                          "list-to-set-membership", samples=samples)
        results.append(("list-to-set[单次查询]", f"候选{n_cands}个", r))

    # --- 域4 dict-dispatch（规模=分支数）---
    for n_b in (3, 6, 12):
        old, new, keys = _mk_dispatch(n_b)
        samples = (keys + ["miss"]) * 20
        r = selfopt.adopt(f"dict-dispatch@{n_b}", old, new,
                          "dict-dispatch", samples=samples)
        results.append(("dict-dispatch", f"{n_b}分支", r))

    return results


def show(results):
    print("=" * 96)
    print(f"{'域':<24}{'规模':<12}{'中位加速':>10}{'p值':>9}{'胜出轮':>9}{'旧ms':>11}{'新ms':>11}  闸门")
    print("-" * 96)
    wins = 0
    for dom, scale, r in results:
        if r.get("ok"):
            tag = "通过"
            wins += 1
            sp = f"{r['speedup']}x"
            p = str(r.get("p_value"))
            w = f"{r.get('wins')}/{r.get('n_pairs')}"
            o, nn = f"{r['old_ms']}", f"{r['new_ms']}"
        else:
            tag = f"拒绝({r.get('stage')})"
            sp = f"{r.get('speedup', '-')}" if r.get("speedup") else "-"
            p = str(r.get("p_value", "-"))
            w = "-"
            o = nn = "-"
        print(f"{dom:<24}{scale:<12}{sp:>10}{p:>9}{w:>9}{o:>11}{nn:>11}  {tag}")
    print("-" * 96)
    print(f"通过闸门 {wins}/{len(results)} 组")
    print("=" * 96)


def _test():
    """自证：等价性先过，才配进 benchmark。"""
    ok = fail = 0

    def check(name, cond):
        nonlocal ok, fail
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)

    print("[bench_real 自测]")
    lines = load_corpus()[:200]
    check("语料非空且为真实源码行", len(lines) > 50 and any(
        l.strip().startswith(("def ", "class ", "import ")) for l in lines))
    ok_eq, cx = selfopt.verify_equivalence(re_naive, re_opt, chunks(lines, 50))
    check("regex old/new 等价", ok_eq)
    ok_eq2, cx2 = selfopt.verify_equivalence(join_naive, join_opt, chunks(lines, 50))
    check("str-join old/new 等价（含空输入边界）", ok_eq2)
    check("str-join 空输入行为一致",
          join_naive([]) == join_opt([]) == "")
    o3, n3, c = _mk_membership(3)
    eq3, _ = selfopt.verify_equivalence(o3, n3, [(c, ["思维过程", "x"])] * 3)
    check("list-to-set(批量) old/new 等价", eq3)
    s3, t3, c3 = _mk_membership_single(3)
    eq3b, _ = selfopt.verify_equivalence(s3, t3, ["思维过程", "x", "自揉"])
    check("list-to-set(单次) old/new 等价", eq3b)
    o4, n4, k4 = _mk_dispatch(6)
    eq4, _ = selfopt.verify_equivalence(o4, n4, k4 + ["miss"])
    check("dict-dispatch old/new 等价", eq4)
    check("测量目录已隔离（不写真实库）",
          str(selfopt.LIB).startswith(_TMP))
    print(f"\n[自测结果] {ok} 通过 / {fail} 失败")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        sys.exit(_test())
    res = run()
    show(res)
    out = Path("/tmp/selfopt-sweep/bench_real.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        [{"domain": d, "scale": s, **r} for d, s, r in res],
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细已存: {out}")
