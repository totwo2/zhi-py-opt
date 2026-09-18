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
import math
import os as _os
import platform
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
try:
    DATA.mkdir(parents=True, exist_ok=True)
except Exception:
    # 目录已存在（重跑）或只读环境：本模块读多写少，建目录失败不该让整个
    # 工具崩掉。真正的写入才报错，此处静默继续。
    pass
LIB = DATA / "library.jsonl"      # 已认证优化记录
CAND = DATA / "candidates.jsonl"  # 未匹配候选（成长素材）
RETRACT = DATA / "retracted.jsonl"  # 作废记录（审计保留：不删库，显式标注）
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

# 多语言扫描器（2026-09-12 新增：不限于 .py，覆盖所有代码）
# .py 走 analyzer.py 的 AST 高精度路径；ts/js/go/java/c/cs/rb/php/rs/sh
# 走 polyglot.py 的语言无关路径。两者产出同构 finding，polyglot 的发现
# 额外带 lang 与 gate 字段（gate="unverified" = 本工具不为它背书加速比）。
try:
    import importlib.util as _ilu2
    _pspec = _ilu2.spec_from_file_location(
        "_selfopt_polyglot", BASE / "scripts" / "polyglot.py")
    _polyglot = _ilu2.module_from_spec(_pspec)
    _pspec.loader.exec_module(_polyglot)
    analyze_polyglot_file = _polyglot.analyze_polyglot_file
    iter_code_files = _polyglot.iter_code_files
    POLYGLOT_EXTS = set(_polyglot.EXT_LANG)
except Exception:
    analyze_polyglot_file = iter_code_files = None
    POLYGLOT_EXTS = set()

DEFAULT_MIN_SPEEDUP = 1.05
# 数学升级（v2 闸门）：符号检验显著性水平。
# 经验采样域的入库条件 = 中位加速比 ≥ 门槛 且 sign-test p < SIGN_ALPHA，
# 把"噪声被当真加速"的假阳性挡在库外（配对测量 + 分布检验，替代 mean 点估计）。
SIGN_ALPHA = 0.05
# 版本感知（数学升级）：加速比是 CPython 版本的函数（3.11+ 自适应特化、
# 3.12 优化 str+=、3.13/3.14 tier-2 JIT 会系统性吃掉传统技巧收益）。
# 入库记录带 py_version，report 时旧版本记录提示"可能已失效"。
_CUR_PY_VERSION = platform.python_version()


# ---------- Layer 2: 验证 ----------

def make_witnesses(spec, n_samples=50, seed=42):
    """按域证人规格生成验证输入。
    binary_seq 是零一原理的引子：对该域而言证人集是完备的。
    其余 kind 是经验采样——随机采样 + 边界样本（空/单元素/极值/负值/重复等）。
    边界样本（数学升级）：纯随机覆盖不到边界行为，而多数 bug 在边界触发；
    配合 verify_equivalence 的异常语义比较，非法输入下"两者都抛同类异常"也判等价，
    不会因边界样本误拒。"""
    rng = random.Random(seed)
    kind = spec.get("kind", "rand_int_list")
    if kind == "binary_seq":
        n = spec["n"]
        return [[(m >> k) & 1 for k in range(n)] for m in range(1 << n)]
    if kind == "rand_int_list":
        n = spec.get("n", 8)
        rands = [[rng.randint(0, 99) for _ in range(n)] for _ in range(n_samples)]
        return rands + [[], [0], [-1], [99] * n, [0] * n, [100, -1, 0, 99]]
    if kind == "rand_str":
        n = spec.get("n", 12)
        alpha = string.ascii_letters + string.digits
        rands = ["".join(rng.choice(alpha) for _ in range(n)) for _ in range(n_samples)]
        return rands + ["", "a", "A" * n, "a" * n, "中文abc123", "a b\tc\n"]
    if kind == "rand_int":
        lo = spec.get("lo", 0)
        hi = spec.get("hi", 2 ** 31)
        rands = [rng.randint(lo, hi) for _ in range(n_samples)]
        return rands + [lo, hi, 0, 1, -1]
    if kind == "rand_rows":
        r, c = spec.get("rows", 20), spec.get("cols", 6)
        rands = [["".join(rng.choice(string.ascii_lowercase) for _ in range(4))
                  for _ in range(c)] for _ in range(r)]
        return rands + [[], [""], ["a" * 50] * c]
    raise ValueError(f"unknown witness kind: {kind}")


def _call(fn, s):
    """调用并捕获结果/异常。等价性比较的是行为（含异常语义）。"""
    try:
        return ("ok", fn(s))
    except Exception as e:
        return ("err", type(e).__name__)


def verify_equivalence(fn_old, fn_new, samples):
    """所有证人输入上行为一致才算等价：返回值相同，且异常语义也相同
    （两者都抛同类型异常 = 等价，例如对非法输入都抛 ValueError）。
    返回 (ok, counterexample)：
    - ok=True  → counterexample=None
    - ok=False → counterexample=第一个反例样本（数学升级：验证失败不只回 False，
      回反例让 LLM 直接定位错在哪个输入上，一次改对，不用盲猜重写）。
    修复点：旧实现把"任一函数抛异常"一律判不等价，
    会把"两者都抛同类异常"的合法重写误杀。"""
    for s in samples:
        if _call(fn_old, s) != _call(fn_new, s):
            return False, s
    return True, None


# ---------- Layer 3: 代价评估 ----------

def benchmark(fn, samples, rounds=30):
    """实测平均单次耗时（ms）。不信预测，只信秒表。
    注：adopt 闸门已改用 benchmark_pair（配对+中位数+符号检验）。
    此单测函数保留供外部自行测量使用，不参与入库决策。"""
    ts = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        for s in samples:
            fn(s)
        ts.append((time.perf_counter() - t0) * 1000)
    return statistics.mean(ts) / max(len(samples), 1)


def benchmark_pair(fn_old, fn_new, samples, rounds=9):
    """配对交替测量（数学升级）：同轮内 old/new 交替计时，控制环境漂移。
    返回 (med_speedup, p_value, wins, n_pairs, med_old_ms, med_new_ms)。
    - 中位数加速比：抗噪（mean 对 GC/调度异常值敏感）
    - sign test（配对符号检验，纯 stdlib 二项分布）：
      H0 = 新旧无差异，p = P(Bin(rounds, 0.5) >= wins)。
      rounds=9 时全胜 p≈0.002，噪声翻不出显著性，假阳性被统计检验挡住。"""
    pairs = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        for s in samples:
            fn_old(s)
        to = (time.perf_counter() - t0) * 1000 / max(len(samples), 1)
        t0 = time.perf_counter()
        for s in samples:
            fn_new(s)
        tn = (time.perf_counter() - t0) * 1000 / max(len(samples), 1)
        pairs.append((to, tn))
    wins = sum(1 for a, b in pairs if b < a)
    p = sum(math.comb(rounds, k) for k in range(wins, rounds + 1)) / (2 ** rounds)
    med_old = statistics.median(a for a, _ in pairs)
    med_new = statistics.median(b for _, b in pairs)
    return med_old / max(med_new, 1e-9), round(p, 4), wins, len(pairs), med_old, med_new


def witness_at_scale(kind, spec, n, n_samples=10, seed=42):
    """按给定规模生成随机样本（跨规模采样用）。
    rand_int / binary_seq 无规模维度，返回 None（不可跨规模分析）。"""
    rng = random.Random(seed)
    if kind == "rand_int_list":
        return [[rng.randint(0, 99) for _ in range(n)] for _ in range(n_samples)]
    if kind == "rand_str":
        alpha = string.ascii_letters + string.digits
        return ["".join(rng.choice(alpha) for _ in range(n)) for _ in range(n_samples)]
    if kind == "rand_rows":
        c = spec.get("cols", 6)
        return [["".join(rng.choice(string.ascii_lowercase) for _ in range(4)) for _ in range(c)]
                for _ in range(n)]
    return None


def crossing_analysis(fn_old, fn_new, kind, spec, sizes=None, rounds=9,
                      threshold=DEFAULT_MIN_SPEEDUP, sample_fn=None):
    """跨规模配对测量，找交叉点 n*（数学升级：规模维度）。
    n* = 中位加速比首次 ≥ threshold 的最小规模；测过规模全不达标 → None
    （说明该优化在当前样本形态下不赢——可能只在大得多的规模生效，或根本不赢）。
    返回 {"curve": [(n, speedup), ...], "n_star": n* 或 None, "scalable": bool}
    scalable=False 表示该 witness kind 无规模维度。curve 可直接入库，report 展示。
    sample_fn：可选自定义规模样本生成器 sample_fn(n) -> 样本列表。
    默认证人类型可能与被测函数不匹配（如 str-join 域是 int 列表，
    拼接函数需要字符串列表）——SKILL.md 明确"默认证人往往不够用，
    必须传真实负载样本"，跨规模分析同理，用 sample_fn 传真实形态。"""
    if sizes is None:
        sizes = [1, 2, 4, 8, 16, 32, 64, 128, 256]
    curve, n_star = [], None
    for n in sizes:
        samples = sample_fn(n) if sample_fn is not None else witness_at_scale(kind, spec, n)
        if samples is None:
            return {"curve": curve, "n_star": n_star, "scalable": False}
        med_sp = benchmark_pair(fn_old, fn_new, samples, rounds=rounds)[0]
        curve.append((n, round(med_sp, 3)))
        if n_star is None and med_sp >= threshold:
            n_star = n
    return {"curve": curve, "n_star": n_star, "scalable": True}


# ---------- 统一入口：adopt ----------

def adopt(name, fn_old, fn_new, domain_id, samples=None,
          min_speedup=DEFAULT_MIN_SPEEDUP, analyze_crossing=False):
    """
    把关流程：查域 → 取证人（随机+边界）→ 等价验证（含异常语义，返回反例）
    → 配对实测 + 符号检验 → 入库。
    数学升级（v2 闸门）：
    - 等价性：随机+边界混合证人；异常语义比较（都抛同类异常=等价）；
      失败时返回首个反例样本（counterexample），LLM 据此一次改对
    - 性能：配对交替测量 + 中位数加速比 + sign-test 显著性
    - 双条件入库：中位加速比 ≥ 门槛，且经验域 sign-test p < SIGN_ALPHA
      （零一完备域 complete=true 正确性已有数学保障，豁免显著性——
       性能门槛的意义仅是"别倒退太多"，无噪声假阳性污染风险）
    - 规模维度：analyze_crossing=True 时跨规模测交叉点 n*（何时才开始赢）
    - 版本感知：入库记录带 py_version（加速比是 CPython 版本的函数）
    - 统计背书：入库记录带 speedup_med / p_value / n_pairs / wins
    samples 参数允许 LLM 在域证人之外补充正例（证人集也可进化）；
    传了 samples 则边界由调用方负责。
    """
    dom = next((d for d in DOMAINS["domains"] if d["id"] == domain_id), None)
    if dom is None:
        with CAND.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "name": name,
                                "domain_id": domain_id,
                                "outcome": "candidate"}, ensure_ascii=False) + "\n")
        return {"ok": False, "stage": "domain",
                "note": f"未知域 '{domain_id}'，已记候选，攒证据后蒸馏新域"}

    # 域内门槛优先（零一完备域可设更低的 1.02，因为正确性已有数学保障）
    min_speedup = dom.get("min_speedup", min_speedup)

    used_custom = samples is not None
    if samples is None:
        samples = make_witnesses(dom["witness"])
    ok, cx = verify_equivalence(fn_old, fn_new, samples)
    if not ok:
        note = "等价性验证未过（随机+边界证人，含异常语义）"
        if cx is not None:
            note += f"，首个反例: {cx!r}"
        return {"ok": False, "stage": "verify", "counterexample": cx, "note": note}

    med_sp, p_val, wins, n_pairs, med_old, med_new = benchmark_pair(
        fn_old, fn_new, samples)
    complete = dom.get("complete", False)
    if med_sp < min_speedup:
        return {"ok": False, "stage": "bench", "speedup": round(med_sp, 3),
                "p_value": p_val,
                "note": f"中位加速 {med_sp:.2f}x 未达 {min_speedup}x 门槛，拒绝入库"}
    if not complete and p_val >= SIGN_ALPHA:
        return {"ok": False, "stage": "bench", "speedup": round(med_sp, 3),
                "p_value": p_val,
                "note": f"中位加速 {med_sp:.2f}x 但 sign-test p={p_val} 不显著"
                        f"（{wins}/{n_pairs} 轮新更快），噪声风险，拒绝入库"}

    witness_label = "custom" if used_custom else dom["witness"]["kind"]
    rec = {"ts": time.time(), "name": name, "domain": domain_id,
           "speedup": round(med_sp, 3),
           "speedup_med": round(med_sp, 3), "p_value": p_val,
           "n_pairs": n_pairs, "wins": wins,
           "old_ms": round(med_old, 5), "new_ms": round(med_new, 5),
           "witness": witness_label,
           "complete": complete,
           "samples": len(samples),
           "py_version": _CUR_PY_VERSION}
    ret = {"ok": True, "speedup": round(med_sp, 3), "p_value": p_val,
           "wins": wins, "n_pairs": n_pairs,
           "old_ms": round(med_old, 5), "new_ms": round(med_new, 5),
           "witness": witness_label, "complete": complete,
           "py_version": _CUR_PY_VERSION}
    if analyze_crossing and not used_custom:
        ca = crossing_analysis(fn_old, fn_new, dom["witness"]["kind"], dom["witness"])
        if ca.get("scalable"):
            rec["n_star"] = ca["n_star"]
            rec["curve"] = ca["curve"]
            ret["n_star"] = ca["n_star"]
            ret["curve"] = ca["curve"]
    with LIB.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return ret


# ---------- 报告 ----------

def report():
    lib = [json.loads(l) for l in LIB.read_text(encoding="utf-8").splitlines() if l.strip()] if LIB.exists() else []
    cand = [json.loads(l) for l in CAND.read_text(encoding="utf-8").splitlines() if l.strip()] if CAND.exists() else []
    retr = [json.loads(l) for l in RETRACT.read_text(encoding="utf-8").splitlines() if l.strip()] if RETRACT.exists() else []
    retr_names = {r["name"] for r in retr}
    print(f"已认证优化: {len(lib)} 条")
    for r in lib:
        mark = " [零一完备]" if r.get("complete") else ""
        if r.get("p_value") is not None:
            stat = f" p={r['p_value']} ({r.get('wins', '?')}/{r.get('n_pairs', '?')}轮)"
        else:
            stat = " [无统计背书·旧闸门记录，待复核]"
        if r.get("n_star") is not None:
            stat += f" 交叉点n*={r['n_star']}"
        if r.get("py_version") and r["py_version"] != _CUR_PY_VERSION:
            stat += f" [版本{r['py_version']}，当前{_CUR_PY_VERSION}，加速比可能已失效]"
        if r["name"] in retr_names:
            stat += " [已作废，见下]"
        print(f"  {r['name']} ({r['domain']}): {r['speedup']}x  "
              f"{r['old_ms']}ms→{r['new_ms']}ms  证人={r['witness']}×{r['samples']}{stat}{mark}")
    if retr:
        print(f"已作废记录: {len(retr)} 条（审计保留，不删库）")
        for r in retr:
            extra = f" 复核={r.get('recheck_speedup')}x" if r.get("recheck_speedup") else ""
            print(f"  {r['name']} ({r.get('domain', '?')}): 作废原因={r['reason']}{extra}")
    print(f"待蒸馏候选: {len(cand)} 条")
    for r in cand:
        oc = r.get("outcome", "candidate")
        print(f"  {r['name']} → 声称域 '{r['domain_id']}'（{oc}）")
    return {"certified": len(lib), "candidates": len(cand), "retracted": len(retr)}


def retract(name, reason, domain=None, recheck_speedup=None):
    """把库中某条记录作废（审计保留：不删 library.jsonl，只追加作废清单）。
    用途：v2.1 新闸门复核实测为假阳性（如旧闸门噪声假加速、版本失效导致重写倒退）。
    reason 必填（作废依据）；recheck_speedup 为复核实测值（如 0.896 = 反而更慢）。"""
    with RETRACT.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "name": name,
                            "domain": domain, "reason": reason,
                            "recheck_speedup": recheck_speedup},
                           ensure_ascii=False) + "\n")
    return {"ok": True, "name": name, "reason": reason}


# ---------- 成长机制（引子） ----------

DISTILL_THRESHOLD = 1  # 首次候选即触发立域信号（不等攒3次；下次遇到就进成长机制）


def reload_domains():
    """重新加载 domains.json。LLM 编辑域库后调用，使新域在当前进程生效。"""
    global DOMAINS
    DOMAINS = json.loads((BASE / "domains.json").read_text(encoding="utf-8"))


# ---------- 自动扫描（去手动路径） ----------

_SKIP_DIRS = {".venv", "venv", "node_modules", "__pycache__",
              ".git", "site-packages", ".workbuddy"}
# 扫描面扩大到全语言后，第三方库/构建产物必须一起屏蔽，否则热点会被
# dist、vendor、.next 里的第三方代码淹没（polyglot 侧已有一份更全的表）
try:
    _SKIP_DIRS |= set(_polyglot.SKIP_DIRS)
except Exception:
    pass


# 单文件扫描上限：超大文件（生成物/日志/数据）既不可能手写优化，
# 也会把扫描拖成分钟级。超过上限直接跳过并在统计里体现。
MAX_FILE_BYTES = 2_000_000


def _iter_all_code(root, exts, max_bytes=MAX_FILE_BYTES):
    """统一文件遍历：.py + 所有 polyglot 支持的语言。

    用 scandir 手写递归而非 rglob("*")：后者会钻进 node_modules、.git 这类
    巨型目录再逐个判后缀，实测在 1.8 万文件的目录上把扫描拖到分钟级。
    这里改成**命中屏蔽目录即剪枝（不再进入）**，并跳过隐藏目录与超大文件。
    """
    import os
    stack = [Path(root)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if e.name in _SKIP_DIRS or e.name.startswith("."):
                                continue
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            if not e.name.lower().endswith(tuple(exts)):
                                continue
                            st = e.stat()
                            if st.st_size <= max_bytes:
                                yield Path(e.path)
                    except OSError:
                        continue
        except (PermissionError, OSError):
            continue


def _scan_one(p, min_confidence):
    """按扩展名分流到 AST 分析器 / 多语言扫描器，并补齐 lang、gate 字段。"""
    suf = p.suffix.lower()
    if suf == ".py":
        if analyze_file is None:
            return []
        try:
            fds = analyze_file(str(p), min_confidence=min_confidence)
        except Exception:
            return []
        for f in fds:
            f.setdefault("lang", "py")
            # 只有 Python 能进 adopt 数学闸门（闸门要成对可调用对象）
            f.setdefault("gate", "python")
        return fds
    if analyze_polyglot_file is None:
        return []
    try:
        return analyze_polyglot_file(str(p), min_confidence=min_confidence)
    except Exception:
        return []


def auto_scan(root=None, max_files=50, min_confidence="high", target=None,
              langs=None):
    """扫描目录下的源码文件并分析热点，无需手动指定文件。**不限于 .py**。

    覆盖范围（2026-09-12 扩展）：.py 走 AST 分析器（带类型推断，精度高）；
    .ts/.tsx/.js/.jsx/.go/.java/.c/.cpp/.cs/.rb/.php/.rs/.sh 走 polyglot
    语言无关扫描器。实测本机代码构成 py 1668 / tsx 292 / ts 200 / go 110
    —— 只扫 .py 会漏掉约 38% 的代码面。

    **要求不变（需求方原话）**：范围扩大但验证标准不降。非 Python 发现一律
    带 gate="unverified"，表示 selfopt **不为它背书任何加速比** —— 硬套
    adopt 闸门等于编数字。能不能提速、提速多少，必须在该语言环境里实测。

    默认扫描当前工作目录；返回聚合的发现清单。
    min_confidence 默认 'high'：自动模式保守。
    target：模糊目标，可填路径片段或函数名（匹配不到文件时按函数名过滤）。
    langs：可选语言白名单，如 {'py','ts','go'}；None=全部支持的语言。
    """
    if analyze_file is None:
        return []
    root = Path(root) if root else Path.cwd()
    t = target.lower().strip() if target else None

    exts = set(langs) if langs else ({".py"} | POLYGLOT_EXTS)
    exts = {e if e.startswith(".") else f".{e}" for e in exts}

    files, matched = [], []
    for p in _iter_all_code(root, exts):
        if len(files) >= max_files and not t:
            break
        files.append(p)
        if t and (t in p.as_posix().lower() or t in p.stem.lower()):
            matched.append(p)

    if t and matched:
        files = matched[:max_files]
        findings = []
        for p in files:
            findings.extend(_scan_one(p, min_confidence))
        return findings
    if t and not matched:
        # 目标可能是函数名：扫描后按函数名/文件路径过滤
        findings = []
        for p in files:
            findings.extend(_scan_one(p, min_confidence))
        return [f for f in findings
                if t in f.get("function", "").lower() or t in f.get("file", "").lower()]

    findings = []
    for p in files:
        findings.extend(_scan_one(p, min_confidence))
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
    """扫描候选池，返回各域的候选计数、ready 标志及 outcome 分布。
    ready=True 的域 = '值得专门造一个证人集'的信号。
    outcomes 记录每次尝试的结果（candidate=仅入池；立域后实际验证结果
    会补记 pass/fail），供立域决策参考成功率（数学升级：不看次数，看成功率）。"""
    if not CAND.exists():
        return []
    cands = [json.loads(l) for l in CAND.read_text(encoding="utf-8").splitlines() if l.strip()]
    agg = {}
    for c in cands:
        did = c.get("domain_id", "?")
        a = agg.setdefault(did, {"count": 0, "outcomes": {}})
        a["count"] += 1
        oc = c.get("outcome", "candidate")
        a["outcomes"][oc] = a["outcomes"].get(oc, 0) + 1
    return [{"domain_id": k, "count": v["count"], "ready": v["count"] >= DISTILL_THRESHOLD,
             "outcomes": v["outcomes"]}
            for k, v in sorted(agg.items(), key=lambda x: -x[1]["count"])]


# ---------- Demo：三个真实场景实测 ----------

def demo():
    import re

    print("=" * 60)
    print("  selfopt demo：LLM 生成重写，selfopt 把关入库")
    print("=" * 60)

    # 场景1: 循环字符串拼接 → join（注意：边界样本 [] 会抓住不等价！
    #   join_old([]) 返回 ''，join_new([]) 返回 ',' —— 纯随机证人无空列表，
    #   旧闸门会放行这个"看似等价"的重写；v2 边界证人在空输入上现形。）
    def join_old(row):
        s = ""
        for x in row:
            s += x + ","
        return s

    def join_new(row):
        return ",".join(row) + ","

    r1 = adopt("build_csv_row", join_old, join_new, "str-join")
    print(f"\n[场景1] 字符串拼接→join: {r1}")
    print("        ↑ 边界样本 [] 抓到真实不等价：旧返回 '', 新返回 ','")
    print("          （旧纯随机闸门会放行；v2 边界证人正确拒绝）")

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


def sweep(root=None, max_files=100000, min_confidence="low", langs=None):
    """全量扫描并输出结构化汇总（回答「这份代码里到底有多少热点、能验多少」）。
    返回 dict，含按语言/按域/按置信度/按可验证性的四张分布表。
    **诚实边界**：gate="unverified" 的发现只有形态信号，selfopt 不给加速比。"""
    from collections import Counter
    root = str(Path(root)) if root else str(Path.cwd())
    exts = set(langs) if langs else ({".py"} | POLYGLOT_EXTS)
    exts = {e if e.startswith(".") else f".{e}" for e in exts}

    scanned = Counter()
    files = []
    for p in _iter_all_code(Path(root), exts):
        if len(files) >= max_files:
            break
        files.append(p)
        scanned[p.suffix.lower()] += 1

    findings = []
    for p in files:
        findings.extend(_scan_one(p, min_confidence))

    by_lang = Counter(f.get("lang", "?") for f in findings)
    by_domain = Counter(f.get("domain", "?") for f in findings)
    by_conf = Counter(f.get("confidence", "?") for f in findings)
    by_gate = Counter(f.get("gate", "?") for f in findings)
    top_files = Counter(f.get("file", "?") for f in findings).most_common(10)
    return {
        "root": root,
        "files_scanned": len(files),
        "files_by_ext": dict(scanned.most_common()),
        "findings": len(findings),
        "by_language": dict(by_lang.most_common()),
        "by_domain": dict(by_domain.most_common()),
        "by_confidence": dict(by_conf.most_common()),
        "by_gate": dict(by_gate.most_common()),
        "top_files": top_files,
        "all": findings,
    }


def print_sweep(sw):
    print("=" * 68)
    print(f"  selfopt 全语言扫描：{sw['root']}")
    print("=" * 68)
    print(f"\n扫描文件 {sw['files_scanned']} 个")
    for ext, n in sorted(sw["files_by_ext"].items(), key=lambda x: -x[1]):
        print(f"  {ext or '(无扩展名)':<8} {n}")
    print(f"\n热点合计 {sw['findings']} 条")
    if sw["by_language"]:
        print("  按语言: " + "  ".join(f"{k}={v}" for k, v in sw["by_language"].items()))
    if sw["by_domain"]:
        print("  按域  : " + "  ".join(f"{k}={v}" for k, v in sw["by_domain"].items()))
    if sw["by_confidence"]:
        print("  按置信: " + "  ".join(f"{k}={v}" for k, v in sw["by_confidence"].items()))
    print("\n  【可验证性——这是能不能谈'提速'的分界线】")
    for k, v in sw["by_gate"].items():
        tag = ("可进 adopt 数学闸门实测（配对+中位数+符号检验）"
               if k == "python" else "仅形态信号，selfopt 不背书加速比")
        print(f"    {k:<12} {v:>5} 条  {tag}")
    if sw["top_files"]:
        print("\n  热点最多的文件:")
        for path, n in sw["top_files"]:
            print(f"    {n:>3}  {path}")
    print("=" * 68)


def selftest():
    """端到端自证：auto_scan 必须同时覆盖 .py 与非 .py，且 gate 标注正确。"""
    import tempfile
    ok = fail = 0

    def check(name, cond):
        nonlocal ok, fail
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)

    print("[selfopt 自测 — 全语言扫描]")
    d = Path(tempfile.mkdtemp())
    (d / "a.py").write_text(
        "def f(row):\n    s = ''\n    for x in row:\n        s += x + ','\n    return s\n",
        encoding="utf-8")
    (d / "b.ts").write_text(
        "function g(rows: string[]) {\n  let s = '';\n  for (const r of rows) { s += r; }\n"
        "  return s;\n}\n", encoding="utf-8")
    (d / "c.go").write_text(
        "package main\nfunc h(rows []string) string {\n  s := \"\"\n"
        "  for _, r := range rows { s += r }\n  return s\n}\n", encoding="utf-8")
    (d / "d.md").write_text("# doc\n", encoding="utf-8")

    sw = sweep(str(d))
    langs = set(sw["by_language"])
    check("扫描覆盖 py+ts+go 三种语言", {"py", "ts", "go"} <= langs)
    check("markdown 未被扫入", "md" not in langs)
    check("py 发现标记为 gate=python（可实测）",
          sw["by_gate"].get("python", 0) >= 1)
    check("ts/go 发现标记为 gate=unverified（不背书）",
          sw["by_gate"].get("unverified", 0) >= 2)
    check("三种语言的 str-join 都被抓到",
          sw["by_domain"].get("str-join", 0) >= 3)

    r = auto_scan(str(d), max_files=10, min_confidence="low")
    check("auto_scan 返回非空且含非 py 文件",
          any(not f["file"].endswith(".py") for f in r))

    print(f"\n[自测结果] {ok} 通过 / {fail} 失败")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        report()
    elif len(sys.argv) > 1 and sys.argv[1] == "scan":
        # 友好交互式扫描：先问目标/全量，再问高频是否优化
        interactive_scan()
    elif len(sys.argv) > 1 and sys.argv[1] == "sweep":
        root = sys.argv[2] if len(sys.argv) > 2 else None
        print_sweep(sweep(root))
    elif len(sys.argv) > 1 and sys.argv[1] == "selftest":
        sys.exit(selftest())
    else:
        demo()
