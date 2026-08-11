#!/usr/bin/env python3
"""
selfopt 分析器：静态扫描 Python 源码，按种子域标出热点位置。
免去消费方自己写分析器的负担。

设计原则（含类型推断 + 开放式降噪）：
  - 最基础的【过程内类型推断】：仅根据初始化赋值推断变量类型，
    用来"降噪打标"，不宣称完备、不做数据流/控制流敏感分析。
  - 分析器只给【信号】，不替你拍板：每条发现带 confidence（high/low）
    与 reason，由调用方（人或 LLM）决定采纳与否。
  - 故意做成【开放式】：判定规则不写死在核心里。各家场景不同，大
    模型可就地向 CONFIDENCE_RULES 追加自己的规则（见文件底部说明），
    既能抑制自家特有的误报，也能加自家特有的判定。
  - auto_scan / hook 默认只看 high（自动模式保守），手动审查看全部。

检测覆盖消费方最常提的 4 类场景：str-join / regex-precompile /
list-to-set-membership / dict-dispatch。sort-small-net 与 lru-cache-pure
静态难可靠检测，留给人工或 LLM 判断。
仅依赖标准库（ast / sys）。
"""
import ast
import sys

# confidence 排序，用于 min_confidence 过滤
_ORDER = {"low": 0, "medium": 1, "high": 2}

# 抑制哨兵：规则返回它 = 明确抑制该发现；返回 None = 不改动（继续下一条规则）
SUPPRESS = object()


def _finding(function, domain, line, filename, confidence, hint):
    return {"function": function, "domain": domain, "line": line,
            "file": filename, "confidence": confidence, "hint": hint}


# ----------------------------------------------------------------------
# 最基础的类型推断：只看"首次初始化赋值"推断变量类型
# ----------------------------------------------------------------------

def _type_of_expr(e):
    """推断一个表达式的（乐观）类型标签。仅看语法形态，不跟踪值。"""
    if isinstance(e, ast.Constant):
        if isinstance(e.value, str):
            return "str"
        if isinstance(e.value, (int, float)):
            return "num"
        if isinstance(e.value, bytes):
            return "bytes"
        return "const"
    if isinstance(e, (ast.List, ast.ListComp)):
        return "list"
    if isinstance(e, ast.Tuple):
        return "tuple"
    if isinstance(e, (ast.Set, ast.SetComp)):
        return "set"
    if isinstance(e, ast.Dict):
        return "dict"
    if isinstance(e, ast.Call):
        f = e.func
        if isinstance(f, ast.Name) and f.id in ("list", "set", "dict",
                                                 "str", "tuple", "frozenset"):
            return f.id
        if isinstance(f, ast.Attribute) and f.attr in ("split", "join",
                                                        "lower", "upper", "format"):
            return "str"  # 字符串方法结果，乐观当 str
        return "call"
    if isinstance(e, ast.Name):
        return "name"   # 需查作用域
    if isinstance(e, ast.BinOp):
        return "str" if isinstance(e.op, ast.Add) else "unknown"
    return "unknown"


def _collect_assigns(fn):
    """收集函数内（不进入嵌套函数）的首次赋值，得到 变量名→类型 表。"""
    assigns = {}

    def rec(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef):
                continue  # 不进入嵌套函数作用域
            if (isinstance(child, ast.Assign) and len(child.targets) == 1
                    and isinstance(child.targets[0], ast.Name)):
                assigns.setdefault(child.targets[0].id,
                                   _type_of_expr(child.value))
            rec(child)

    rec(fn)
    return assigns


# ----------------------------------------------------------------------
# 各域检测（带类型推断降噪）
# ----------------------------------------------------------------------

def _elif_count(fn):
    """统计函数体顶层 if-elif 链的分支数。"""
    count = 0
    for stmt in fn.body:
        if isinstance(stmt, ast.If):
            count = 1
            node = stmt
            while node.orelse:
                if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
                    count += 1
                    node = node.orelse[0]
                else:
                    break
    return count


def _scan_function(fn, filename, infer):
    out = []

    def walk(node, depth):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef):
                out.extend(_scan_function(child, filename, _collect_assigns(child)))
                continue
            new_depth = depth + 1 if isinstance(child, (ast.For, ast.While)) else depth

            # ---- str-join：循环内对累加器做 += ----
            if (isinstance(child, ast.AugAssign) and isinstance(child.op, ast.Add)
                    and isinstance(child.target, ast.Name) and new_depth > 0):
                t = infer.get(child.target.id, "unknown")
                if t == "str":
                    conf, reason = "high", "累加器初始化为空串，确定为字符串拼接，可改 ''.join(...)"
                elif t in ("list", "tuple", "set", "dict"):
                    conf, reason = "low", "累加器非字符串类型，+= 不是字符串拼接，疑似误报"
                else:
                    conf, reason = "low", "累加器类型不确定，请确认是字符串拼接再改 join"
                out.append(_finding(fn.name, "str-join", child.lineno, filename, conf, reason))

            # ---- regex-precompile：re.compile 出现在函数体内 ----
            if isinstance(child, ast.Call):
                f = child.func
                if (isinstance(f, ast.Attribute) and f.attr == "compile"
                        and isinstance(f.value, ast.Name) and f.value.id == "re"):
                    arg = child.args[0] if child.args else None
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        conf, reason = "high", "模式为字符串常量，可安全提到模块级预编译"
                    elif isinstance(arg, ast.Constant):
                        conf, reason = "low", "模式非字符串常量"
                    else:
                        conf, reason = "low", "模式依赖运行期输入，可能无法安全提升（需确认每次是否相同）"
                    out.append(_finding(fn.name, "regex-precompile", child.lineno,
                                        filename, conf, reason))

            # ---- list-to-set-membership：循环内做 `x in 容器` ----
            if (isinstance(child, ast.Compare)
                    and any(isinstance(op, ast.In) for op in child.ops) and new_depth > 0):
                rhs = child.comparators[0]
                rt = _type_of_expr(rhs)
                if rt in ("list", "tuple"):
                    conf, reason = "high", "右值为列表/元组且循环内不变，转 set 后 in 由 O(n)→O(1)"
                elif rt == "name":
                    nt = infer.get(rhs.id, "unknown")
                    if nt == "list":
                        conf, reason = "high", "右值推断为 list 且循环内不变，可转 set"
                    elif nt == "set":
                        conf, reason = "low", "右值已为 set，无需再转"
                    elif nt == "str":
                        conf, reason = "low", "右值为字符串，in 是子串匹配而非成员检测，非本域"
                    elif nt == "dict":
                        conf, reason = "low", "右值为 dict，in 查键而非成员检测"
                    else:
                        conf, reason = "low", "右值类型不确定，请确认是否为循环内不变的列表"
                elif rt in ("set", "dict", "str"):
                    conf, reason = "low", "右值已为 set/dict/str，不属于'列表转 set'场景"
                else:
                    conf, reason = "low", "右值来源不明，请确认是否为循环内不变的列表"
                out.append(_finding(fn.name, "list-to-set-membership", child.lineno,
                                    filename, conf, reason))

            walk(child, new_depth)

    walk(fn, 0)

    # ---- dict-dispatch：3+ 个 elif 的长链 ----
    if _elif_count(fn) >= 3:
        out.append(_finding(fn.name, "dict-dispatch", fn.lineno, filename, "high",
                            "if-elif 长链分派，可改为 dict.get(key, default) 查表"))
    return out


# ----------------------------------------------------------------------
# 开放式判定规则钩子
# ----------------------------------------------------------------------
# 各家场景不同，不要把"何谓误报"写死在核心里。大模型/调用方可在运行时
# 向此列表追加自己的规则：
#
#   def my_rule(finding, fn, infer):
#       """返回 'high' / 'low' 调整置信度；返回 None 表示抑制该发现。"""
#       # 例如：自家框架里 str-join 其实走的是特化累加器，统一抑制
#       if finding["domain"] == "str-join" and "FrameworkBuf" in fn.name:
#           return None
#       return None  # 不动
#   analyzer.CONFIDENCE_RULES.append(my_rule)
#
# 规则签名：rule(finding: dict, fn: ast.FunctionDef, infer: dict) -> Optional[str]
# 多个规则依次执行；任一返回 None 即抑制该发现；返回字符串则覆盖置信度。
CONFIDENCE_RULES = []


def _apply_rules(findings, fn, infer):
    kept = []
    for fd in findings:
        conf = fd["confidence"]
        suppressed = False
        for rule in CONFIDENCE_RULES:
            try:
                res = rule(fd, fn, infer)
            except Exception:
                continue  # 规则自身出错不阻断主流程
            if res is SUPPRESS:
                suppressed = True
                break
            if isinstance(res, str) and res in _ORDER:
                conf = res  # 覆盖置信度，继续看后续规则
        if suppressed:
            continue
        fd["confidence"] = conf
        kept.append(fd)
    return kept


# ----------------------------------------------------------------------
# 对外入口
# ----------------------------------------------------------------------

def analyze_source(src, filename="<string>", min_confidence="low"):
    """扫描源码字符串，返回热点清单（list[dict]）。
    min_confidence: 'low'(默认, 全报) / 'medium' / 'high'(仅高置信)。
    """
    tree = ast.parse(src)
    out = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            infer = _collect_assigns(node)
            findings = _scan_function(node, filename, infer)
            findings = _apply_rules(findings, node, infer)
            out.extend(findings)
    if min_confidence != "low":
        out = [f for f in out if _ORDER.get(f["confidence"], 0) >= _ORDER[min_confidence]]
    return out


def analyze_file(path, min_confidence="low"):
    """扫描文件，返回热点清单。"""
    with open(path, encoding="utf-8") as f:
        return analyze_source(f.read(), path, min_confidence=min_confidence)


if __name__ == "__main__":
    for path in sys.argv[1:]:
        try:
            findings = analyze_file(path)
        except FileNotFoundError:
            print(f"{path}: 文件不存在")
            continue
        except SyntaxError as e:
            print(f"{path}: 语法错误 {e}")
            continue
        print(f"{path}: {len(findings)} 个热点")
        for fd in findings:
            print(f"  L{fd['line']} [{fd['domain']}/{fd['confidence']}] "
                  f"{fd['function']}: {fd['hint']}")
