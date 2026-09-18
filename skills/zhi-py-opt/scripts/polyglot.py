#!/usr/bin/env python3
"""
selfopt 多语言扫描器（polyglot）— 把热点扫描从 .py 扩到所有代码
================================================================
【思路笔记 / 立此存照】

## 一、解决什么问题
需求原话（2026-09-12）：「改成**包括不限于对 .py，对所有代码的**，
都可以，但是**要求不变**」。

原 selfopt 的扫描链路死在一处：auto_scan 两处硬编码 `root.rglob("*.py")`，
分析器 analyzer.py 又只吃 Python AST。后果是 TS/TSX/Go/JS/Shell 里
**同形态的热点一个都看不见**。实测本机代码库构成
（2026-09-12 find 统计）：py 1668 / tsx 292 / ts 200 / go 110 / sh 24 / js 12
—— 只扫 .py 等于放弃约 **38%** 的代码面。

## 二、核心机制
1. **不做 N 个 AST，做一层语言无关的结构近似**。
   给每种语言写 parser 成本失控且不可维护；改用「**循环深度栈 + 模式表**」：
   - 大括号语言（js/ts/tsx/go/java/c/cpp/cs/rb）：以 `{`/`}` 维护深度，
     遇 `for`/`while` 记下"该深度是循环体"，据此判断某行是否在循环内。
   - Shell：以 `for|while ... do` / `done` 配对称记循环。
   - Python：**不走本模块**，仍走 analyzer.py 的 AST 高精度路径（类型推断更准）。
2. **模式表按语言分组，不混用**。同一语义（循环内拼字符串）在 Go 里是
   `s += "x"`、在 JS 里是 `s += "x"`、在 Shell 里是 `V="$V..."`，写法不同，
   正则必须分开写，不能"一套正则打天下"——混用必出误报。
3. **"要求不变"= 验证标准不降，不是扫描标准不变**（关键设计决定）：
   本模块只产出**信号**，且每条发现强制带 `gate` 字段：
   - `gate="python"` → 可进 adopt 数学闸门（配对+中位数+符号检验）实测
   - `gate="unverified"` → **本模块不为它背书任何加速比**。
     非 Python 语言无法在 Python 进程内构造成对可调用对象，硬套闸门
     等于编数字。宁可只报"这里有形态"，也不报"这里有 1.5x"。
   （这条直接对应 2026-09-11 教训：收益数字不许 AI 编。）
4. **置信度保守**：无类型推断兜底，凡是"可能是也可能不是"的一律 low。
   high 只在「模式字面确定 + 确实在循环内 + 无歧义」时给。

## 三、我的适配点
- 与 analyzer.py 产出**同构** finding（function/line/file/domain/confidence/hint），
  只多 `lang` 与 `gate` 两字段，下游 suggest_targets/auto_scan 零改动可消费。
- 语言识别走扩展名，未知扩展名直接跳过（不猜、不硬扫）。
- 跳过 vendor 目录（node_modules/dist/.next/vendor 等），避免把第三方库
  算成用户代码热点。
- 自带 `--test`：断言模式表在正例上命中、在已知反例上不命中。
  （脚本必须自证 + 给一条可复制的验证命令。）
"""
import re
from pathlib import Path

# ----------------------------------------------------------------------
# 语言识别与目录屏蔽
# ----------------------------------------------------------------------

# 扩展名 → 语言 id。未列出的扩展名一律不扫（不猜语言）。
EXT_LANG = {
    ".ts": "ts", ".tsx": "ts", ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js",
    ".go": "go",
    ".java": "java",
    ".c": "c", ".h": "c", ".cc": "c", ".cpp": "c", ".hpp": "c",
    ".cs": "cs",
    ".rb": "rb",
    ".php": "php",
    ".rs": "rs",
    ".sh": "sh", ".bash": "sh", ".zsh": "sh",
}

SKIP_DIRS = {
    "node_modules", "dist", "build", "out", ".next", ".nuxt", "vendor",
    ".git", "__pycache__", ".venv", "venv", "site-packages", ".workbuddy",
    "coverage", ".cache", "target", "bin", "obj",
}

# ----------------------------------------------------------------------
# 模式表：语言 → [(domain, 编译后的正则, hint, confidence)]
# 每条模式都在「循环内」才报；loop_only=False 的模式不要求循环上下文。
# ----------------------------------------------------------------------

def _c(pattern):
    return re.compile(pattern)


# 语义分组（与 domains.json 的域 id 对齐；无法对齐的用通用名）
#
# 关于 str-join 的判据（自测逼出来的修正，勿回退）：
#   第一版只匹配 `s += "字面量"`，结果 `s += r`（右值是变量）全部漏报——
#   而真实代码里后者更常见。但不能因此放宽成"见 += 就报"：数字累加
#   `count += 1` 是循环里最高频的写法，全报等于用噪音淹没信号。
#   最终判据（二选一才报）：
#     ① 右值是字符串字面量 → high
#     ② 左值变量名被识别为"字符串累加器"（初始化为 "" ）→ high
#   二者都不是 → 不报。宁可漏，不可吵。
STR_JOIN_BRACE = ("str-join", _c(r"\b(\w+)\s*\+=\s*"), "high",
                  "循环内对字符串累加器做 +=，可改数组/builder 收集后 join")


def _string_accumulators(lines):
    """识别"初始化为空串"的变量名——循环内对它的 += 就是字符串拼接。
    覆盖 js/ts(let|const|var s = "")、go(s := "")、java/cs(String s = "")。"""
    rx = _c(r"\b(?:let|const|var|String|string)\s+(\w+)\s*(?::\s*string\s*)?=\s*"
            r"(?:new\s+String\(\s*\))?[\"'`]{2}\s*;?\s*$|"
            r"\b(\w+)\s*:=\s*\"\"\s*$")
    names = set()
    for line in lines:
        m = rx.search(line)
        if m:
            g = m.group(1) or m.group(2)
            if g:
                names.add(g)
    return names


STR_LITERAL_RHS = _c(r"\+=\s*[\"'`]")

PATTERNS = {
    # --- 循环内字符串拼接 → 改用 builder/join ---
    "ts": [
        STR_JOIN_BRACE,
        ("str-join", _c(r"\b(\w+)\s*=\s*\1\s*\+\s*['\"`]"), "high",
         "循环内自拼接 s = s + '...'，可改数组 push + join('')"),
        ("regex-in-loop", _c(r"new\s+RegExp\s*\("), "high",
         "循环内构造 RegExp，可提到循环外复用（模式为常量时安全）"),
        ("membership-in-loop", _c(r"\.includes\s*\("), "low",
         "循环内对数组做 includes，元素多时改 Set.has 由 O(n)→O(1)"),
        ("io-in-loop", _c(r"\b(readFileSync|writeFileSync|appendFileSync)\s*\("), "high",
         "循环内同步文件 I/O，可提到循环外批量处理"),
    ],
    "js": [],  # 与 ts 同规则，运行时复用
    "go": [
        ("str-join", _c(r"\b(\w+)\s*\+=\s*"), "high",
         "循环内字符串 += 产生新分配，改 strings.Builder 或 bytes.Buffer"),
        ("regex-in-loop", _c(r"regexp\.(MustCompile|Compile)\s*\("), "high",
         "循环内编译正则，可提到循环外（或包级 var）复用"),
        ("io-in-loop", _c(r"\b(os\.ReadFile|ioutil\.ReadFile|os\.Open)\s*\("), "high",
         "循环内重复读文件，可提到循环外一次读入"),
    ],
    "java": [
        ("str-join", _c(r"\b(\w+)\s*\+=\s*"), "high",
         "循环内字符串 += 每次新建对象，改 StringBuilder.append"),
        ("regex-in-loop", _c(r"Pattern\.compile\s*\("), "high",
         "循环内 Pattern.compile，可提到循环外 static final 复用"),
        ("membership-in-loop", _c(r"\.contains\s*\("), "low",
         "循环内对 List 做 contains，元素多时改 HashSet.contains"),
    ],
    "c": [
        ("str-join", _c(r"\bstrcat\s*\("), "medium",
         "循环内 strcat 是 O(n²)，改维护尾指针或预分配缓冲"),
        ("io-in-loop", _c(r"\b(fopen|fread|fwrite)\s*\("), "medium",
         "循环内重复打开/读写文件，可提到循环外"),
    ],
    "cs": [
        ("str-join", _c(r"\b(\w+)\s*\+=\s*"), "high",
         "循环内字符串 += 改 StringBuilder.Append"),
        ("regex-in-loop", _c(r"new\s+Regex\s*\("), "high",
         "循环内 new Regex，可提到循环外或改 RegexOptions.Compiled 静态实例"),
    ],
    "rb": [
        ("str-join", _c(r"\b(\w+)\s*<<\s*['\"]"), "medium",
         "循环内字符串 << ，大循环可改 Array#join"),
        ("regex-in-loop", _c(r"Regexp\.new\s*\("), "medium",
         "循环内构造 Regexp，可提到循环外复用"),
    ],
    "php": [
        ("str-join", _c(r"\b(\w+)\s*\.=\s*['\"]"), "high",
         "循环内字符串 .= ，可改数组 + implode"),
        ("regex-in-loop", _c(r"\bpreg_match\s*\("), "low",
         "循环内 preg_match，模式固定时可预编译（preg 自带缓存，收益需实测）"),
    ],
    "rs": [
        ("str-join", _c(r"\b(\w+)\.push_str\s*\("), "low",
         "循环内 push_str，可预分配 String::with_capacity 减少扩容"),
        ("regex-in-loop", _c(r"Regex::new\s*\("), "high",
         "循环内 Regex::new，可提到循环外或 lazy_static 复用"),
    ],
    "sh": [
        ("str-join", _c(r"\b(\w+)=\"\$\{?\1\}?[^\"]*\""), "medium",
         "循环内自拼接变量，量大时改数组或 printf 累积"),
        ("io-in-loop", _c(r"\b(cat|curl|wget|sed|awk|grep)\s+"), "medium",
         "循环内起外部进程，代价高；可提到循环外一次处理"),
    ],
}
# js 与 ts 同规则
PATTERNS["js"] = PATTERNS["ts"]


# 不在循环内也值得报的模式（语言 → [(domain, regex, hint, confidence)]）
GLOBAL_PATTERNS = {
    "ts": [
        ("dispatch-chain", _c(r"^\s*(else\s+if|\}\s*else\s+if)\s*\("), "low",
         "if-else 长链，分支多时可改查表（需实测，短链无收益）"),
    ],
    "go": [
        ("dispatch-chain", _c(r"^\s*case\s+"), "low",
         "switch-case 长链，Go 编译器对密集 case 会跳表，一般无需改"),
    ],
}
GLOBAL_PATTERNS["js"] = GLOBAL_PATTERNS["ts"]


# ----------------------------------------------------------------------
# 循环上下文判断
# ----------------------------------------------------------------------

_BRACE_LOOP_RE = _c(r"\b(for|while|forEach)\b")
_SH_LOOP_START = _c(r"^\s*(for|while)\b")
_SH_DO = _c(r"\bdo\s*$")
_SH_DONE = _c(r"^\s*done\b")


def _brace_loop_depths(lines):
    """大括号语言：返回 set[int]，其中的行号（0-based）处于某个循环体内。
    做法：维护 '{' 深度栈，栈里记录"这一层是循环体吗"；
    遇到 for/while 标记下一个 '{' 为循环入口。"""
    in_loop_lines = set()
    stack = []          # 每层: bool 该层是否循环体
    pending_loop = False
    for i, line in enumerate(lines):
        code = _strip_line_comment(line)
        # 先判断本行是否处于循环体内（在任何括号变化之前）
        if any(stack):
            in_loop_lines.add(i)
        if _BRACE_LOOP_RE.search(code):
            pending_loop = True
        for ch in code:
            if ch == "{":
                entered_loop = pending_loop
                stack.append(pending_loop)
                pending_loop = False
                # 入栈的瞬间就标记：`for (...) { s += r; }` 这种单行写法里
                # '}' 与本行同行，等括号处理完再判，循环层已被弹掉（自测抓出）。
                if entered_loop:
                    in_loop_lines.add(i)
            elif ch == "}":
                if stack:
                    stack.pop()
    return in_loop_lines


def _sh_loop_lines(lines):
    """Shell：do ... done 之间算循环体（以最近的 for/while 为起点）。"""
    in_loop = set()
    stack = []
    for i, line in enumerate(lines):
        if _SH_LOOP_START.match(line):
            stack.append(True)
        if _SH_DO.search(line) and stack:
            # do 之后进入体
            pass
        if stack and not _SH_DONE.match(line):
            in_loop.add(i)
        if _SH_DONE.match(line) and stack:
            stack.pop()
    return in_loop


def _strip_line_comment(line):
    """去掉 // 行注释（粗暴但够用：只在模式匹配前用，避免注释被当代码）。
    字符串里的 // 会被误删，但只会降低召回，不会制造误报——符合保守原则。"""
    idx = line.find("//")
    return line[:idx] if idx != -1 else line


def _enclosing_function(lines, idx, lang):
    """向上找最近的函数/方法定义行，作为 finding 的 function 名（尽力而为）。"""
    fname_re = {
        "ts": _c(r"(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(|"
                 r"^\s*(?:export\s+)?(?:async\s+)?(\w+)\s*\([^)]*\)\s*\{)"),
        "js": _c(r"(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(|"
                 r"function\s*\(\s*\)\s*)"),
        "go": _c(r"func\s+(?:\([^)]*\)\s*)?(\w+)\s*\("),
        "java": _c(r"(?:public|private|protected|static|final|\s)*[\w<>\[\]]+\s+(\w+)\s*\("),
        "c": _c(r"^\s*(?:static\s+)?[\w\*\s]+(\w+)\s*\([^;]*\)\s*\{"),
    }.get(lang)
    if fname_re is None:
        return "<module>"
    for j in range(idx, max(-1, idx - 60), -1):
        m = fname_re.search(lines[j])
        if m:
            for g in m.groups():
                if g:
                    return g
    return "<module>"


# ----------------------------------------------------------------------
# 对外入口
# ----------------------------------------------------------------------

def supported(path):
    """该文件是否属于本模块支持的语言。"""
    return Path(path).suffix.lower() in EXT_LANG


def iter_code_files(root, exts=None, skip_dirs=None):
    """遍历目录下所有受支持的源码文件（同时含 .py，交给上层分流）。
    exts: 可选扩展名集合（如 {'.py','.ts','.go'}），None=全部支持的语言。"""
    root = Path(root)
    skip = set(SKIP_DIRS if skip_dirs is None else skip_dirs)
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in skip for part in p.parts):
            continue
        suf = p.suffix.lower()
        if exts is not None:
            if suf in exts:
                yield p
        elif suf in EXT_LANG:
            yield p


def analyze_polyglot_file(path, min_confidence="low"):
    """扫描单个非 Python 源码文件，返回 finding 列表（同构于 analyzer.py，
    额外带 lang 与 gate 两字段）。"""
    lang = EXT_LANG.get(Path(path).suffix.lower())
    if lang is None or lang == "py":
        return []
    try:
        src = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    lines = src.splitlines()
    if lang == "sh":
        loop_lines = _sh_loop_lines(lines)
    else:
        loop_lines = _brace_loop_depths(lines)

    pats = PATTERNS.get(lang, [])
    globals_pats = GLOBAL_PATTERNS.get(lang, [])
    accs = _string_accumulators(lines)
    out = []
    for i, raw in enumerate(lines):
        code = _strip_line_comment(raw) if lang in ("ts", "js", "go", "java",
                                                     "c", "cs", "rs") else raw
        if not code.strip():
            continue
        in_loop = i in loop_lines
        for domain, rx, conf, hint in pats:
            if not in_loop:
                continue      # 域级模式一律要求循环上下文
            m = rx.search(code)
            if not m:
                continue
            # str-join 二选一判据（见文件头注释：右值是字面量 或 左值是字符串累加器）
            # 只对 `+=` 形式生效：`s = s + '..'`、`OUT="$OUT$f"` 这类非 += 写法
            # 本身就是自拼接，不需要累加器佐证（曾因漏加此条件被误杀）。
            if domain == "str-join" and "+=" in m.group(0):
                var = m.group(1) if m.groups() else None
                if not (STR_LITERAL_RHS.search(code) or (var and var in accs)):
                    continue
            out.append({
                "function": _enclosing_function(lines, i, lang),
                "domain": domain,
                "line": i + 1,
                "file": str(path),
                "confidence": conf,
                "hint": hint,
                "lang": lang,
                "gate": "unverified",
            })
        for domain, rx, conf, hint in globals_pats:
            if rx.search(code):
                out.append({
                    "function": _enclosing_function(lines, i, lang),
                    "domain": domain,
                    "line": i + 1,
                    "file": str(path),
                    "confidence": conf,
                    "hint": hint,
                    "lang": lang,
                    "gate": "unverified",
                })
    if min_confidence != "low":
        order = {"low": 0, "medium": 1, "high": 2}
        want = order.get(min_confidence, 0)
        out = [f for f in out if order.get(f["confidence"], 0) >= want]
    return out


# ----------------------------------------------------------------------
# 自测：正例必须命中，反例必须不命中（脚本自证）
# ----------------------------------------------------------------------

def _test():
    import tempfile
    ok, fail = 0, 0

    def check(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  PASS  {name}")
        else:
            fail += 1
            print(f"  FAIL  {name}")

    def scan(src, suffix):
        d = Path(tempfile.mkdtemp())
        p = d / f"t{suffix}"
        p.write_text(src, encoding="utf-8")
        return analyze_polyglot_file(str(p))

    print("[polyglot 自测]")

    # --- TS 正例：循环内字符串 += ---
    ts_src = """
function build(rows: string[]) {
  let s = "";
  for (const r of rows) {
    s += r;
  }
  return s;
}
"""
    r = scan(ts_src, ".ts")
    check("TS 循环内 str += 命中 str-join",
          any(f["domain"] == "str-join" for f in r))
    check("TS 命中行在循环内(line=5)",
          any(f["domain"] == "str-join" and f["line"] == 5 for f in r))
    check("TS finding 带 lang=ts 与 gate=unverified",
          all(f.get("lang") == "ts" and f.get("gate") == "unverified" for f in r))

    # --- TS 反例：循环外拼接不得命中 ---
    ts_no_loop = """
function one(a: string, b: string) {
  let s = "";
  s += a;
  return s;
}
"""
    check("TS 循环外 str += 不命中（避免误报）",
          not any(f["domain"] == "str-join" for f in scan(ts_no_loop, ".ts")))

    # --- TS 反例：注释里的拼接不得命中 ---
    ts_comment = """
function build(rows: string[]) {
  for (const r of rows) {
    // s += r;
  }
}
"""
    check("TS 注释内 str += 不命中",
          not any(f["domain"] == "str-join" for f in scan(ts_comment, ".ts")))

    # --- TS 正例：循环内 new RegExp ---
    ts_re = """
function f(xs: string[]) {
  for (const x of xs) {
    const re = new RegExp("^a+$");
  }
}
"""
    check("TS 循环内 new RegExp 命中 regex-in-loop",
          any(f["domain"] == "regex-in-loop" for f in scan(ts_re, ".ts")))

    # --- Go 正例 ---
    go_src = """
func build(rows []string) string {
    s := ""
    for _, r := range rows {
        s += r
    }
    return s
}
"""
    r = scan(go_src, ".go")
    check("Go 循环内 s += 命中", any(f["domain"] == "str-join" for f in r))
    check("Go 找到函数名 build",
          all(f["function"] == "build" for f in r) or
          any(f["function"] == "build" for f in r))

    # --- Go 反例：循环外 MustCompile 不命中 ---
    go_no_loop = """
var rx = regexp.MustCompile("^a+$")
func f(s string) bool { return rx.MatchString(s) }
"""
    check("Go 循环外 regexp.MustCompile 不命中",
          not any(f["domain"] == "regex-in-loop" for f in scan(go_no_loop, ".go")))

    # --- Shell 正例 ---
    sh_src = """
for f in $(ls); do
  OUT="$OUT$f"
done
"""
    r = scan(sh_src, ".sh")
    check("Shell 循环内变量自拼接命中",
          any(f["domain"] == "str-join" for f in r))

    # --- 不支持的扩展名直接跳过 ---
    check("未知扩展名(.md)不产出 finding",
          scan("# hello\n", ".md") == [])

    print(f"\n[自测结果] {ok} 通过 / {fail} 失败")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        sys.exit(_test())
    for path in sys.argv[1:]:
        fds = analyze_polyglot_file(path)
        print(f"{path}: {len(fds)} 个热点")
        for fd in fds:
            print(f"  L{fd['line']} [{fd['domain']}/{fd['confidence']}/{fd['lang']}] "
                  f"{fd['function']}: {fd['hint']}")
