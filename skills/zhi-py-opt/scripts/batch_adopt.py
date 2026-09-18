#!/usr/bin/env python3
"""
selfopt 批量实测（batch_adopt）— 把扫出来的高置信热点**逐条**过闸门
====================================================================
【思路笔记 / 立此存照】

## 一、解决什么问题
2026-09-12 追问：「有 92 个高可信热点，为啥只选三个，不 92 个挨个跑一遍」。

理由成立——抽 3 条只是样本，不能代表 92 条。本脚本就是把每一条都送进闸门，
不挑、不跳，跑不动的也要给出**跑不动的具体原因**，不许含糊过去。

## 二、核心机制
1. **绝不 exec 用户工作区的原文件**。历史工作区里的脚本可能有写文件/联网/
   起进程等副作用，直接 exec 风险不可控。改为 **AST 静态重建**：
   只把一个模块里的「白名单 import + 常量赋值 + 函数/类定义」装进命名空间
   （这些都是声明，不产生副作用），再把目标函数单独编译出来。
2. **自动改写按域机械变换**（AST 级，不是文本替换）：
   - list-to-set-membership：`x in [字面量...]` → `x in {字面量...}`
   - regex-precompile：`re.compile(常量)` → 预编译变量复用
   - str-join：累加器 `s += e` → `parts.append(e)` + 出口 `''.join(parts)`
   - dict-dispatch：if-elif 链 → dict 查表（仅当分支是「k == 常量: return 常量」）
3. **改写后必须过闸门**：不等价会被 verify_equivalence 直接拒（这是闸门的价值，
   不是脚本的失败）。等价但不够快的，也会被性能门槛拒。
4. **归因不含糊**：每条结果必定落在以下之一，且"不可测"必须带原因：
   通过 / 闸门拒绝(等价性) / 闸门拒绝(性能) / 不可构造(带原因) / 改写未生成。

## 三、我的适配点（诚实边界，勿删）
**默认证人不是该函数的真实负载**。本脚本用域自带证人集（随机+边界），而
SKILL.md 明确写过"默认证人往往不够用，必须传真实负载样本"。所以：
  - 本脚本的结果是**粗筛**：能证伪（拒绝的确实是拒绝），不能证实
    （通过的不代表在真实负载上也这么快）。
  - 想要终审数字，必须用 scripts/bench_real.py 那种真实语料逐域测。
这个降级必须在报告里写明，不许拿本脚本的数字当承诺。
"""
import ast
import builtins
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

_TMP = os.environ.get("SELFOPT_DATA_DIR") or tempfile.mkdtemp(prefix="selfopt-batch-")
os.environ["SELFOPT_DATA_DIR"] = _TMP
import selfopt  # noqa: E402

FINDINGS = Path("/tmp/selfopt-sweep/findings.json")

# 只认这些顶层导入（都是标准库纯声明，导入无副作用）
SAFE_IMPORTS = {
    "re", "os", "sys", "json", "math", "time", "string", "collections",
    "itertools", "typing", "pathlib", "random", "statistics", "functools",
    "datetime", "hashlib", "unicodedata", "abc", "dataclasses", "enum",
}


# ----------------------------------------------------------------------
# 安全重建：只取声明，不执行副作用
# ----------------------------------------------------------------------

def _is_const_node(node):
    """是否是不产生副作用的常量/容器字面量。"""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_is_const_node(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(_is_const_node(k) and _is_const_node(v)
                   for k, v in zip(node.keys, node.values))
    return False


def build_namespace(path):
    """从文件 AST 重建一个安全命名空间：白名单 import + 常量赋值 + 定义。"""
    src = Path(path).read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    ns = {"__name__": "selfopt_batch_probe"}
    prelude = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            if all(a.name.split(".")[0] in SAFE_IMPORTS for a in node.names):
                prelude.append(node)
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in SAFE_IMPORTS:
                prelude.append(node)
        elif isinstance(node, ast.Assign):
            if _is_const_node(node.value):
                prelude.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            prelude.append(node)
    try:
        exec(compile(ast.Module(body=prelude, type_ignores=[]), str(path), "exec"), ns)
    except Exception as e:
        return src, tree, ns, f"命名空间构建失败: {type(e).__name__}: {e}"
    return src, tree, ns, None


def find_function(tree, fname, lineno):
    """按 (函数名, 行号) 定位函数定义节点。行号命中优先，其次同名。"""
    hit = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == fname and node.lineno <= lineno <= (node.end_lineno or node.lineno):
                return node
            if node.name == fname and hit is None:
                hit = node
    return hit


def instantiate(fn_node, ns, new_name, path):
    """把函数节点编译进命名空间，返回可调用对象。"""
    clone = ast.parse(ast.unparse(fn_node)).body[0]
    clone.name = new_name
    mod = ast.Module(body=[clone], type_ignores=[])
    exec(compile(mod, str(path), "exec"), ns)
    return ns[new_name]


# ----------------------------------------------------------------------
# 按域机械改写（AST 级）
# ----------------------------------------------------------------------

class _Rewritten(Exception):
    pass


def rewrite_list_to_set(fn_node):
    """x in [字面量] / (字面量) → x in {字面量}"""
    n = 0
    for node in ast.walk(fn_node):
        if isinstance(node, ast.Compare) and any(isinstance(o, ast.In) for o in node.ops):
            rhs = node.comparators[0]
            if isinstance(rhs, (ast.List, ast.Tuple)) and _is_const_node(rhs):
                node.comparators[0] = ast.Set(elts=list(rhs.elts))
                n += 1
    return n


class _RXTransformer(ast.NodeTransformer):
    """把 `re.compile("常量")` 这个**调用本身**替换成预编译变量。

    注意替换的是 compile 调用节点，不是它的 func——原写法
    `re.compile(p).match(x)` 的 AST 是 Call(Attribute(Call(compile), 'match'), [x])，
    只有把内层 Call 整体换成 Name，外层才能自然变成 `_RX0.match(x)`。
    （第一版错在改 func 并清空 args，会把参数丢掉。）"""

    def __init__(self, ns):
        self.ns = ns
        self.n = 0

    def visit_Call(self, node):
        self.generic_visit(node)
        f = node.func
        if (isinstance(f, ast.Attribute) and f.attr == "compile"
                and isinstance(f.value, ast.Name) and f.value.id == "re"
                and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            var = f"_SELFOPT_RX{self.n}"
            self.ns[var] = __import__("re").compile(node.args[0].value)
            self.n += 1
            return ast.copy_location(ast.Name(id=var, ctx=ast.Load), node)
        return node


def rewrite_regex_precompile(fn_node, ns):
    t = _RXTransformer(ns)
    t.visit(fn_node)
    ast.fix_missing_locations(fn_node)
    return t.n


def rewrite_str_join(fn_node):
    """累加器 `s += e` → parts.append(e)，出口改为 ''.join(parts)"""
    assigns = {}
    for node in fn_node.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)
                and node.value.value == ""):
            assigns[node.targets[0].id] = node
    if not assigns:
        return 0
    acc = next(iter(assigns))
    n = 0
    for node in ast.walk(fn_node):
        if (isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add)
                and isinstance(node.target, ast.Name) and node.target.id == acc):
            node.__class__ = ast.Expr
            node.value = ast.Call(
                func=ast.Attribute(value=ast.Name(id=acc, ctx=ast.Load),
                                   attr="append", ctx=ast.Load),
                args=[node.value], keywords=[])
            n += 1
    if n == 0:
        return 0
    # 初始化：s = "" → s = []
    assigns[acc].value = ast.List(elts=[], ctx=ast.Load)
    # 出口：return s → return ''.join(s)
    for node in ast.walk(fn_node):
        if (isinstance(node, ast.Return) and isinstance(node.value, ast.Name)
                and node.value.id == acc):
            node.value = ast.Call(
                func=ast.Attribute(value=ast.Constant(value=""), attr="join",
                                   ctx=ast.Load),
                args=[ast.Name(id=acc, ctx=ast.Load)], keywords=[])
    return n


def rewrite_dict_dispatch(fn_node):
    """顶层 if-elif 链（k == 常量: return 常量）→ dict 查表"""
    body = fn_node.body
    idx = None
    for i, st in enumerate(body):
        if isinstance(st, ast.If):
            idx = i
            break
    if idx is None:
        return 0
    pairs, node = [], body[idx]
    ok = True
    while True:
        test = node.test
        if (isinstance(test, ast.Compare) and len(test.ops) == 1
                and isinstance(test.ops[0], ast.Eq)
                and isinstance(test.comparators[0], ast.Constant)
                and len(node.body) == 1 and isinstance(node.body[0], ast.Return)
                and _is_const_node(node.body[0].value)):
            key = test.left
            pairs.append((test.comparators[0].value, node.body[0].value.value))
        else:
            ok = False
            break
        if node.orelse and len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
            node = node.orelse[0]
        else:
            break
    if not ok or len(pairs) < 3:
        return 0
    tbl_name = "_SELFOPT_TBL"
    tbl = ast.Dict(keys=[ast.Constant(value=k) for k, _ in pairs],
                   values=[ast.Constant(value=v) for _, v in pairs])
    default = ast.Constant(value=-1)
    assign = ast.Assign(targets=[ast.Name(id=tbl_name, ctx=ast.Store)], value=tbl)
    fn_node.body[idx:idx + 1] = [assign, ast.Return(value=ast.Call(
        func=ast.Attribute(value=ast.Name(id=tbl_name, ctx=ast.Load),
                           attr="get", ctx=ast.Load),
        args=[body[idx].test.left if hasattr(body[idx], "test") else ast.Constant(value=""),
              default], keywords=[]))]
    return len(pairs)


REWRITERS = {
    "list-to-set-membership": lambda fn, ns: rewrite_list_to_set(fn),
    "regex-precompile": lambda fn, ns: rewrite_regex_precompile(fn, ns),
    "str-join": lambda fn, ns: rewrite_str_join(fn),
    "dict-dispatch": lambda fn, ns: rewrite_dict_dispatch(fn),
}


# ----------------------------------------------------------------------
# 安全护栏 + 证人自适应
# ----------------------------------------------------------------------

_real_open = builtins.open


def _readonly_open(file, mode="r", *a, **k):
    """批量实测期间的写保护：热点函数来自用户工作区，无法静态判断有没有
    写文件行为。实测中就出现过拿随机串当路径去 open 的情况——若模式是写，
    后果不可控。这里一律拒绝写/追加/创建模式。"""
    if any(m in mode for m in ("w", "a", "x", "+")):
        raise PermissionError("selfopt 批量实测：只读护栏，禁止写入")
    return _real_open(file, mode, *a, **k)


class readonly_guard:
    def __enter__(self):
        builtins.open = _readonly_open
        return self

    def __exit__(self, *exc):
        builtins.open = _real_open
        return False


# 备选证人规格：域默认证人常常与热点函数的签名对不上
# （实测：给期望 str 的 parse_lines 喂 list、给要文件路径的 naive_extract 喂 str，
#  20 条里没有一条能跑通）。这里按"函数能接受哪种输入"自动挑。
ALT_WITNESS_SPECS = [
    {"kind": "rand_str", "n": 24},
    {"kind": "rand_int_list", "n": 8},
    {"kind": "rand_int"},
    {"kind": "rand_rows", "rows": 12, "cols": 4},
]


def pick_samples(fn, domain):
    """挑一组 fn 能吃得下的证人：先试域默认，再逐个试备选。
    判据=前 3 个样本不抛 TypeError/AttributeError/ValueError（签名类错配）。
    注意：不抛异常只能说明形态对得上，**不代表是真实负载**——所以本脚本
    的结果仍只是粗筛，这条边界不因为自适应而消失。"""
    specs = []
    dom = next((d for d in selfopt.DOMAINS["domains"] if d["id"] == domain), None)
    if dom:
        specs.append(dom["witness"])
    specs.extend(ALT_WITNESS_SPECS)
    for spec in specs:
        try:
            samples = selfopt.make_witnesses(spec, n_samples=30)
        except Exception:
            continue
        try:
            for s in samples[:3]:
                fn(s)
        except PermissionError:
            continue          # 触发写保护 → 该函数不安全，换证人也没用
        except (TypeError, AttributeError, ValueError, IndexError, KeyError):
            continue          # 形态不匹配，换下一种
        except Exception:
            return samples, ("域默认" if spec is specs[0] else spec["kind"])
    return None, None


# ----------------------------------------------------------------------
# 主流程：逐条过闸门
# ----------------------------------------------------------------------

def run_one(fd, idx):
    path, fname, line, domain = fd["file"], fd["function"], fd["line"], fd["domain"]
    base = {"#": idx, "domain": domain, "fn": fname,
            "loc": f"{Path(path).name}:{line}"}
    try:
        src, tree, ns, err = build_namespace(path)
    except SyntaxError as e:
        return {**base, "verdict": "不可构造", "detail": f"语法错误: {e}"}
    except Exception as e:
        return {**base, "verdict": "不可构造", "detail": f"读取失败: {type(e).__name__}"}
    if err:
        return {**base, "verdict": "不可构造", "detail": err}

    fn_node = find_function(tree, fname, line)
    if fn_node is None:
        return {**base, "verdict": "不可构造", "detail": "定位不到函数定义"}

    try:
        old = instantiate(fn_node, ns, "_selfopt_old", path)
    except Exception as e:
        return {**base, "verdict": "不可构造",
                "detail": f"实例化旧版失败: {type(e).__name__}: {str(e)[:60]}"}

    rw = REWRITERS.get(domain)
    if rw is None:
        return {**base, "verdict": "改写未生成", "detail": f"域 {domain} 无自动改写规则"}
    try:
        new_node = ast.parse(ast.unparse(fn_node)).body[0]
        hits = rw(new_node, ns)
    except Exception as e:
        return {**base, "verdict": "改写未生成", "detail": f"改写异常: {type(e).__name__}"}
    if not hits:
        return {**base, "verdict": "改写未生成", "detail": "该条形态不匹配自动改写规则"}
    try:
        new = instantiate(new_node, ns, "_selfopt_new", path)
    except Exception as e:
        return {**base, "verdict": "改写未生成",
                "detail": f"实例化新版失败: {type(e).__name__}"}

    samples, wlabel = pick_samples(old, domain)
    if samples is None:
        return {**base, "verdict": "证人不可适配",
                "detail": "域默认+4 种备选证人该函数都吃不下，需人工给真实负载"}
    try:
        r = selfopt.adopt(f"{domain}@{Path(path).name}:{line}", old, new,
                          domain, samples=samples)
    except PermissionError:
        return {**base, "verdict": "安全拦截", "detail": "函数试图写文件，只读护栏拒绝执行"}
    except Exception as e:
        return {**base, "verdict": "不可构造",
                "detail": f"闸门异常: {type(e).__name__}: {str(e)[:60]}"}

    if r.get("ok"):
        return {**base, "verdict": "通过", "speedup": r["speedup"],
                "p": r.get("p_value"), "wins": f"{r.get('wins')}/{r.get('n_pairs')}",
                "witness": wlabel}
    return {**base, "verdict": f"闸门拒绝({r.get('stage')})",
            "speedup": r.get("speedup"), "p": r.get("p_value"),
            "witness": wlabel,
            "detail": (r.get("note") or "")[:90]}


def main():
    if not FINDINGS.exists():
        print(f"缺少 {FINDINGS}，先跑 sweep 导出")
        return 1
    fds = json.loads(FINDINGS.read_text(encoding="utf-8"))
    targets = [f for f in fds
               if f.get("gate") == "python" and f.get("confidence") == "high"]
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        targets = targets[:int(sys.argv[1])]
    print(f"待实测高置信可验证热点: {len(targets)} 条（逐条过闸门，不抽样）\n")
    results = []
    for i, fd in enumerate(targets, 1):
        builtins.open = _readonly_open      # 全程只读护栏
        try:
            results.append(run_one(fd, i))
        except Exception as e:
            results.append({"#": i, "domain": fd.get("domain"), "fn": fd.get("function"),
                            "loc": "?", "verdict": "不可构造",
                            "detail": f"未捕获: {type(e).__name__}"})
        finally:
            builtins.open = _real_open

    from collections import Counter
    cnt = Counter(r["verdict"].split("(")[0] for r in results)
    print("=" * 104)
    print("逐条结果")
    print("=" * 104)
    for r in results:
        sp = f"{r['speedup']}x" if r.get("speedup") else "-"
        extra = r.get("detail", "")
        print(f"{r['#']:>3} [{r['domain']:<24}] {r['loc']:<34} "
              f"{r['verdict']:<18} {sp:>9}  {extra}")
    print("=" * 104)
    print("汇总: " + "  ".join(f"{k}={v}" for k, v in cnt.most_common()))

    passed = [r for r in results if r["verdict"] == "通过"]
    if passed:
        sps = sorted(r["speedup"] for r in passed)
        med = sps[len(sps) // 2]
        print(f"\n通过 {len(passed)} 条：加速比 {sps[0]}x ~ {sps[-1]}x（中位 {med}x）")
        print("⚠ 证人=域默认证人，非该函数真实负载 → 粗筛，不作承诺")
    out = Path("/tmp/selfopt-sweep/batch_adopt.json")
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
