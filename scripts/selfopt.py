#!/usr/bin/env python3
"""
selfopt — AI 智能体自优化种子包（运行时，仅依赖标准库）

三层分工：
  Layer 1 候选生成 = LLM 智能体本身（读代码、生成优化重写）
  Layer 2 验证门   = 本模块 verify/adopt（域证人集 + 等价性检查）
  Layer 3 代价评估 = 本模块 benchmark（实测耗时，达标才入库）

自我进步机制（引子）：
  - 未知域的优化尝试 → 记入 candidates.jsonl，攒证据
  - LLM 定期回顾候选 → 蒸馏成新域条目追加进 domains.json
  - 每个用户的库随自己的负载生长，不追求大而全
"""
import json
import os as _os
import random
import statistics
import string
import tempfile
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
# 数据目录可被环境变量覆盖（测试用临时目录隔离，避免污染真实库）
_DATA_OVERRIDE = _os.environ.get("SELFOPT_DATA_DIR")
DATA = Path(_DATA_OVERRIDE) if _DATA_OVERRIDE else (BASE / "data")
DATA.mkdir(exist_ok=True)
LIB = DATA / "library.jsonl"      # 已认证优化记录
CAND = DATA / "candidates.jsonl"  # 未匹配候选（成长素材）
DOMAINS = json.loads((BASE / "domains.json").read_text(encoding="utf-8"))

# 分析器（静态扫描热点）随包发，消费方一次 import 即可用
try:
    import importlib.util as _ilu
    _aspec = _ilu.spec_from_file_location(
        "_selfopt_analyzer", BASE / "scripts" / "analyzer.py")
    _analyzer = _ilu.module_from_spec(_aspec)
    _aspec.loader.exec_module(_analyzer)
    analyze_file = _analyzer.analyze_file
    analyze_source = _analyzer.analyze_source
except Exception:
    analyze_file = analyze_source = None

DEFAULT_MIN_SPEEDUP = 1.05


# ---------- Layer 2: 验证 ----------

def make_witnesses(spec, n_samples=50, seed=42):
    """按域证人规格生成验证输入。
    binary_seq 是零一原理的引子：对该域而言证人集是完备的。
    其余 kind 是经验采样——够用，但不声称完备。"""
    rng = random.Random(seed)
    kind = spec.get("kind", "rand_int_list")
    if kind == "binary_seq":
        n = spec["n"]
        return [[(m >> k) & 1 for k in range(n)] for m in range(1 << n)]
    if kind == "rand_int_list":
        n = spec.get("n", 8)
        return [[rng.randint(0, 99) for _ in range(n)] for _ in range(n_samples)]
    if kind == "rand_str":
        n = spec.get("n", 12)
        alpha = string.ascii_letters + string.digits
        return ["".join(rng.choice(alpha) for _ in range(n)) for _ in range(n_samples)]
    if kind == "rand_int":
        lo = spec.get("lo", 0)
        hi = spec.get("hi", 2 ** 31)
        return [rng.randint(lo, hi) for _ in range(n_samples)]
    if kind == "rand_rows":
        r, c = spec.get("rows", 20), spec.get("cols", 6)
        return [["".join(rng.choice(string.ascii_lowercase) for _ in range(4))
                 for _ in range(c)] for _ in range(r)]
    raise ValueError(f"unknown witness kind: {kind}")


def verify_equivalence(fn_old, fn_new, samples):
    """所有证人输入上输出一致才算等价。有一个反例就拒绝。"""
    for s in samples:
        try:
            if fn_old(s) != fn_new(s):
                return False
        except Exception:
            return False
    return True


# ---------- Layer 3: 代价评估 ----------

def benchmark(fn, samples, rounds=30):
    """实测平均单次耗时（ms）。不信预测，只信秒表。"""
    ts = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        for s in samples:
            fn(s)
        ts.append((time.perf_counter() - t0) * 1000)
    return statistics.mean(ts) / max(len(samples), 1)


# ---------- 统一入口：adopt ----------

def adopt(name, fn_old, fn_new, domain_id, samples=None,
          min_speedup=DEFAULT_MIN_SPEEDUP):
    """
    把关流程：查域 → 取证人 → 等价验证 → 实测加速 → 入库。
    任何一关不过都不污染库；未知域记入候选池（生长素材）。
    samples 参数允许 LLM 在域证人之外补充正例（证人集也可进化）。
    """
    dom = next((d for d in DOMAINS["domains"] if d["id"] == domain_id), None)
    if dom is None:
        with CAND.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "name": name,
                                "domain_id": domain_id}, ensure_ascii=False) + "\n")
        return {"ok": False, "stage": "domain",
                "note": f"未知域 '{domain_id}'，已记候选，攒证据后蒸馏新域"}

    # 域内门槛优先（零一完备域可设更低的 1.02，因为正确性已有数学保障）
    min_speedup = dom.get("min_speedup", min_speedup)

    used_custom = samples is not None
    if samples is None:
        samples = make_witnesses(dom["witness"])
    if not verify_equivalence(fn_old, fn_new, samples):
        return {"ok": False, "stage": "verify",
                "note": "等价性验证未过（存在反例），拒绝入库"}

    t_old = benchmark(fn_old, samples)
    t_new = benchmark(fn_new, samples)
    speedup = t_old / max(t_new, 1e-9)
    witness_label = "custom" if used_custom else dom["witness"]["kind"]
    if speedup < min_speedup:
        return {"ok": False, "stage": "bench", "speedup": round(speedup, 3),
                "note": f"加速 {speedup:.2f}x 未达 {min_speedup}x 门槛，拒绝入库"}

    rec = {"ts": time.time(), "name": name, "domain": domain_id,
           "speedup": round(speedup, 3),
           "old_ms": round(t_old, 5), "new_ms": round(t_new, 5),
           "witness": witness_label,
           "complete": dom.get("complete", False),
           "samples": len(samples)}
    with LIB.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"ok": True, "speedup": round(speedup, 3),
            "old_ms": round(t_old, 5), "new_ms": round(t_new, 5),
            "witness": witness_label, "complete": dom.get("complete", False)}


# ---------- 报告 ----------

def report():
    lib = [json.loads(l) for l in LIB.read_text(encoding="utf-8").splitlines() if l.strip()] if LIB.exists() else []
    cand = [json.loads(l) for l in CAND.read_text(encoding="utf-8").splitlines() if l.strip()] if CAND.exists() else []
    print(f"已认证优化: {len(lib)} 条")
    for r in lib:
        mark = " [零一完备]" if r.get("complete") else ""
        print(f"  {r['name']} ({r['domain']}): {r['speedup']}x  "
              f"{r['old_ms']}ms→{r['new_ms']}ms  证人={r['witness']}×{r['samples']}{mark}")
    print(f"待蒸馏候选: {len(cand)} 条")
    for r in cand:
        print(f"  {r['name']} → 声称域 '{r['domain_id']}'（攒证据中）")
    return {"certified": len(lib), "candidates": len(cand)}


# ---------- 成长机制（引子） ----------

DISTILL_THRESHOLD = 3  # 同域候选达到此数 → 蒸馏信号


def reload_domains():
    """重新加载 domains.json。LLM 编辑域库后调用，使新域在当前进程生效。"""
    global DOMAINS
    DOMAINS = json.loads((BASE / "domains.json").read_text(encoding="utf-8"))


# ---------- 自动扫描（去手动路径） ----------

_SKIP_DIRS = {".venv", "venv", "node_modules", "__pycache__",
              ".git", "site-packages", ".workbuddy"}


def auto_scan(root=None, max_files=50, min_confidence="high", target=None):
    """扫描目录下的 .py 文件并分析热点，无需手动指定文件。
    默认扫描当前工作目录；返回聚合的发现清单。
    这是把'喂我一个 py'变成'我自己去找'的关键 —— 配合触发器即自动优化。
    min_confidence 默认 'high'：自动模式保守，只报高置信发现，
    把误报挡在自动层之外，避免 LLM 在假阳性上白费功夫。
    人工审查时可传 'low' 看全部（含 low 待 LLM 自行研判）。
    target：模糊目标，可填路径片段或函数名。
      - 能匹配到文件路径/文件名 → 只扫这些文件（定向扫描）
      - 匹配不到文件但可能是函数名 → 全扫后按函数名过滤
      - 留空(None) → 全量扫描
    """
    if analyze_file is None:
        return []
    root = Path(root) if root else Path.cwd()
    t = target.lower().strip() if target else None

    if t:
        # 第一优先：按文件路径/文件名模糊匹配
        matched = []
        all_py = []
        for p in root.rglob("*.py"):
            if any(part in _SKIP_DIRS for part in p.parts):
                continue
            all_py.append(p)
            if t in p.as_posix().lower() or t in p.stem.lower():
                matched.append(p)
        if matched:
            py_files = matched[:max_files]
        else:
            # 目标可能是函数名：全扫后按函数名/文件路径过滤
            findings = []
            for p in all_py[:max_files]:
                try:
                    findings.extend(analyze_file(str(p), min_confidence=min_confidence))
                except Exception:
                    continue
            return [f for f in findings
                    if t in f["function"].lower() or t in f["file"].lower()]
    else:
        py_files = []
        for p in root.rglob("*.py"):
            if any(part in _SKIP_DIRS for part in p.parts):
                continue
            py_files.append(p)
            if len(py_files) >= max_files:
                break

    findings = []
    for p in py_files:
        try:
            findings.extend(analyze_file(str(p), min_confidence=min_confidence))
        except Exception:
            continue
    return findings


def suggest_targets(findings):
    """把发现按 (域, 函数) 聚合，按出现频率排序，标出高频候选。
    返回的每组含 count（同形态出现次数，越高越'高频'）、confidence、位置。
    宿主可据此挑'高频且高置信'的先问用户要不要优化。"""
    from collections import defaultdict
    agg = defaultdict(lambda: {"count": 0, "confidence": "low",
                               "line": None, "hint": None, "files": set()})
    for f in findings:
        key = (f["domain"], f["function"])
        a = agg[key]
        a["count"] += 1
        a["confidence"] = f.get("confidence", "low")
        a["line"] = f.get("line")
        a["hint"] = f.get("hint")
        a["files"].add(f["file"])
    groups = []
    for (domain, fn), a in agg.items():
        groups.append({
            "domain": domain, "function": fn, "count": a["count"],
            "confidence": a["confidence"], "line": a["line"], "hint": a["hint"],
            "files": sorted(a["files"]),
            "high_freq": a["count"] >= 2,
        })
    # 高频优先，其次高置信
    groups.sort(key=lambda g: (0 if g["high_freq"] else 1,
                                0 if g["confidence"] == "high" else 1))
    return groups


def interactive_scan(root=None, max_files=50):
    """友好交互式扫描（CLI 版）：先问目标/全量，再问高频热点是否优化。
    注意：这是给纯命令行用的参考实现。宿主 Agent 应把两处 input() 换成
    自家对话框（如 WorkBuddy 的 AskUserQuestion），'问什么、按什么过滤'的逻辑不变。
    返回用户选定要优化的组清单（每组含 function/domain/location），Agent 据此
    在对应位置生成更快版本后 selfopt.adopt() 把关入库。"""
    if analyze_file is None:
        print("[selfopt] 分析器不可用。")
        return []

    # 对话1：目标 or 全量
    try:
        ans = input("[selfopt] 扫描范围：输入模糊路径/函数名做定向扫描，"
                    "留空=全量 → ").strip()
    except EOFError:
        ans = ""
    target = ans or None

    findings = auto_scan(root, max_files=max_files, min_confidence="high", target=target)
    if not findings:
        print("[selfopt] 未扫描到高置信热点。")
        return []

    groups = suggest_targets(findings)
    print(f"\n[selfopt] 发现 {len(findings)} 个热点，按高频聚合为 {len(groups)} 组：")
    for i, g in enumerate(groups, 1):
        tag = "●高频" if g["high_freq"] else " "
        print(f"  {i}. {tag} [{g['domain']}/{g['confidence']}] "
              f"{g['function']} ×{g['count']}  {g['files'][0]}"
              + (f" 等{g['count']}处" if g['count'] > 1 else ""))

    # 对话2：确认优化哪些（高频优先问）
    try:
        pick = input("\n[selfopt] 要优化哪几组？(序号逗号分隔, 回车=都不) → ").strip()
    except EOFError:
        pick = ""
    if not pick:
        print("[selfopt] 未选择，跳过。")
        return []
    chosen = []
    for part in pick.replace("，", ",").split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(groups):
                chosen.append(groups[idx])
    if not chosen:
        print("[selfopt] 无有效选择，跳过。")
        return []
    print(f"[selfopt] 已选 {len(chosen)} 组，请在以上位置生成更快版本后 "
          f"用 selfopt.adopt() 把关入库（切勿未验证就改源码）。")
    return chosen


def add_domain(entry):
    """向 domains.json 追加一个新域条目，然后热重载。
    幂等：同 id 已存在则跳过。entry 需含 id/name/scenario/witness/rewrite_hint。"""
    data = json.loads((BASE / "domains.json").read_text(encoding="utf-8"))
    if any(d["id"] == entry["id"] for d in data["domains"]):
        return {"ok": False, "note": f"域 '{entry['id']}' 已存在，跳过"}
    data["domains"].append(entry)
    (BASE / "domains.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    reload_domains()
    return {"ok": True, "domain": entry["id"], "total_domains": len(data["domains"])}


def growth_signals():
    """扫描候选池，返回各域的候选计数及是否达到蒸馏阈值。
    ready=True 的域 = '值得专门造一个证人集'的信号。"""
    if not CAND.exists():
        return []
    cands = [json.loads(l) for l in CAND.read_text(encoding="utf-8").splitlines() if l.strip()]
    counts = {}
    for c in cands:
        did = c.get("domain_id", "?")
        counts[did] = counts.get(did, 0) + 1
    return [{"domain_id": k, "count": v, "ready": v >= DISTILL_THRESHOLD}
            for k, v in sorted(counts.items(), key=lambda x: -x[1])]


# ---------- Demo：三个真实场景实测 ----------

def demo():
    import re

    print("=" * 60)
    print("  selfopt demo：LLM 生成重写，selfopt 把关入库")
    print("=" * 60)

    # 场景1: 循环字符串拼接 → join
    def join_old(row):
        s = ""
        for x in row:
            s += x + ","
        return s

    def join_new(row):
        return ",".join(row) + ","

    r1 = adopt("build_csv_row", join_old, join_new, "str-join")
    print(f"\n[场景1] 字符串拼接→join: {r1}")

    # 场景2: 调用内重复编译正则 → 模块级预编译
    def re_old(s):
        return bool(re.compile(r"^[A-Z]{2}\d{4}$").match(s))

    _RX = re.compile(r"^[A-Z]{2}\d{4}$")

    def re_new(s):
        return bool(_RX.match(s))

    # LLM 补充正例样本（域证人是随机串，几乎不产生命中，正例靠生成器补）
    pos = ["AB1234", "XY0000", "ZZ9999"] * 10
    neg = ["ab1234", "A12345", "ABC123", "12AB34"] * 10
    r2 = adopt("is_code", re_old, re_new, "regex-precompile", samples=pos + neg)
    print(f"[场景2] 正则预编译: {r2}")

    # 场景3: 5 元插入网络(10比较器) → 零一认证网络(9比较器)
    def run_net(net):
        def f(a):
            a = list(a)
            for i, j in net:
                if a[i] > a[j]:
                    a[i], a[j] = a[j], a[i]
            return a
        return f

    INS = ((0, 1), (0, 2), (1, 2), (0, 3), (1, 3), (2, 3),
           (0, 4), (1, 4), (2, 4), (3, 4))
    OPT = ((0, 1), (0, 2), (0, 3), (0, 4), (1, 2),
           (3, 4), (1, 3), (2, 4), (2, 3))
    r3 = adopt("sort5", run_net(INS), run_net(OPT), "sort-small-net")
    print(f"[场景3] 排序网络 10→9 比较器: {r3}")

    # 场景4: 未知域 → 生长钩子演示
    def norm_old(s):
        return " ".join(s.split())

    def norm_new(s):
        return s.strip()

    r4 = adopt("normalize_ws", norm_old, norm_new, "whitespace-normalize")
    print(f"[场景4] 未知域（且重写不等价）: {r4}")

    print("\n" + "-" * 60)
    report()
    print("=" * 60)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        report()
    elif len(sys.argv) > 1 and sys.argv[1] == "scan":
        # 友好交互式扫描：先问目标/全量，再问高频是否优化
        interactive_scan()
    else:
        demo()
