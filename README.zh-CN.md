[English](README.md) | 中文 · 当前版本 **v2.1.0**

> **本仓库（`zhi-py-opt`）就是 selfopt** —— 同一项目、同一份代码。SkillHub 条目暂用旧拼音 slug。

# selfopt — 给你的 AI 那句"这样更快"设一道闸门

> 你的 agent 重写了热点函数，声称**快了 1.5 倍**。
> 谁来验过？
>
> **selfopt 把这句话拉到真机上** —— 真证人、配对交替实测、符号检验。
> 不过关就永远进不了库，你的源码原样不动。

---

## 装上它，你的每次改动都过一道真机闸门

你的 agent 改了一处代码，说"这样更快"。selfopt 把这句话拉到真机上验：

- **等价吗？** 随机证人 + 边界证人（含异常语义）逐对比对，不等价直接拒，并带回反例。
- **真的更快吗？** old/new 同轮交替测量（控环境漂移）+ 抗异常加速比 + 符号检验，双条件都过才入库。

不过关的改动永远进不了库，你的源码原样不动。

## 它确认过哪些真的更快（真实负载实测，p = 0.002）

| 模式 | 加速比 |
|---|---|
| 正则预编译 | **1.9x** |
| `str-join` vs `+=` | **4x** |
| `list` → `set` 成员判断 | **1.27x**（3 元素）→ **29–49x**（200 元素） |
| `dict` 派发 | 1.14x（3 分支）→ **2.96x**（12 分支） |

> 这些是**模式级**数字，不是"你的代码库整体提速 X%"——没人能诚实地给你后者，这个包也不假装能。

## 你项目里那些"看起来能优化"的改动，大多过不了

正因上面这套闸门很硬，它才会拦下**你自己代码里 AI 想动的那些**。真实目录（2,297 个文件）扫出 1,018 个静态热点，92 个高置信可验证，逐个过闸门：

| 结果 | 数量 | 含义 |
|---|---|---|
| **放行（确认更快且等价）** | **0** | 没有一个能独立证真 |
| 拒绝（结果错） | **1** | 机械重写对 `[]` 返回 `','`，原式返回 `''` —— **闸门抓到了** |
| 无法构造证人 | 54 | 依赖 BaseModel / asyncio / logger 等 |
| 证人不适配 | 31 | 函数需要特定对象形态 |
| 没生成重写 | 6 | 模式不匹配机械规则 |

**这不是重写失败，是闸门在干活**——它告诉你那些"看起来热"的改动，大部分造不出独立证人、或形态对不上、或干脆改错（如 `str-join` 空列表返回 `','` 而非 `''`）。静态分析只说*哪里热*，说不出*快多少、对不对*。

**它拦下的，比它放行的更有价值：**

| 它拦下的 | 真实案例 |
|---|---|
| **错误重写** | `str-join` 重写：`[]` 时旧版 `''`、新版 `','`。随机证人放过，边界证人拒掉。 |
| **噪声"加速"** | 两个*完全相同*的函数：旧版点估计在 5 次里有 1 次放行了 **1.095x**。9 对符号检验：**p = 0.25 → 拒绝**。 |
| **假加速陷阱** | CPython 在编译期把 `x in {"a","b","c"}` 折叠成 `frozenset` 常量 → 微基准测谎。（参见 `Lib/test/test_peepholer.py::test_folding_of_sets_of_constants`） |
| **静默回退** | 两条已放行记录在真实负载下复检：**0.896x** 和 **0.509x** —— 都更慢。现已撤回。 |

**不出错的重写排第一，速度排第二。**

---

## 30 秒上手

```bash
git clone https://github.com/totwo2/selfopt.git ~/.workbuddy/skills/selfopt
# 或克隆到任意目录，只要把 scripts/ 加进 sys.path 即可

# 验证能用
python3 ~/.workbuddy/skills/selfopt/scripts/polyglot.py --test   # → 11 PASS
python3 ~/.workbuddy/skills/selfopt/scripts/selfopt.py selftest   # → 6 PASS
```

纯 Python 标准库。没有 `pip install`，没有构建步骤。

## 怎么用

```python
import sys; sys.path.insert(0, "~/.workbuddy/skills/selfopt/scripts")
import selfopt

# 1. 找热点（Python：AST；其他语言：模式）
for fd in selfopt.analyze_file("your_script.py"):
    print(fd["function"], fd["domain"], fd["line"], fd["hint"])

# 2. 你的 agent 写出更快版本 → 闸门判定
ok = selfopt.adopt("my_hot_fn", old_fn, new_fn, "str-join")
# ok["ok"] 为 True → 入库，带统计支撑
# ok["ok"] 为 False → 看 ok["stage"] 和 ok["note"]（以及 ok["counterexample"]）
```

```bash
python3 scripts/selfopt.py               # 内置 demo（看闸门拒绝坏重写）
python3 scripts/selfopt.py report        # 库 / 候选池里有什么
python3 scripts/selfopt.py sweep <root>  # 全语言扫描 + 分布汇总
python3 scripts/selfopt.py scan          # 交互：选目标 → 扫描 → 选优化哪个
python3 scripts/analyzer.py file.py      # 静态扫描（.py，AST + 类型推断）
python3 scripts/polyglot.py file.ts      # 静态扫描（非 Python）
python3 scripts/bench_real.py            # 测每个域到底值多少
python3 scripts/batch_adopt.py [N]       # 把扫出的热点逐个过闸门
```

## 语言覆盖

`.py` 走 AST + 类型推断。`.ts .tsx .js .jsx .mjs .cjs .go .java .c .cc .cpp .h .hpp .cs .rb .php .rs .sh .bash .zsh` 走与语言无关的模式表（循环深度栈 —— 不写 N 套 AST）。

**每条 finding 都带 `gate` 字段 —— 引用数字前先读它：**

| `gate` | 含义 |
|---|---|
| `"python"` | 能进 `adopt` 闸门，被真实测量 |
| `"unverified"` | **只有形态信号。selfopt 不为它背书任何加速比。** 非 Python 代码无法在 Python 进程里构造成可调用的配对；硬过闸门只会编个数字。 |

扩大范围**没有**降低门槛。验不了的明确标 unverified。

## 闸门（简版）

1. **等价性** —— 随机 + 边界证人（空 / 单元素 / 极端 / 负数 / 重复 / 非 ASCII / 非法输入）。两边都抛同类型异常算等价，合法重写不会被误杀。失败返回**第一个反例**（`[]`、`-1` …），agent 一次改对。
2. **性能** —— 配对交替测量（控环境漂移）、中位数加速比、以及**符号检验**（经验域 `p < 0.05`）。两个条件须同时成立。
3. **版本感知** —— 每条记录存 `py_version`；CPython 3.13+ 特化吃掉多个经典技巧，旧记录标"可能过时"。
4. **撤回** —— 复检失败记录进 `data/retracted.jsonl`。**库从不被静默改写**；留历史供审计。

数学细节在 [`docs/math-gate.md`](docs/math-gate.md) —— 它们不是产品。

## 生长：6 个种子域覆盖不到你时

6 个种子域永远覆盖不了全部 —— **这正是触发点，不是终点。**
当 `adopt` 返回 `{"stage": "domain", ...}`，或分析器报 0 信号但你能看到可优化模式：

1. 给它一个描述性 `domain_id`（`io-parse-cache`、`json-memo` …）
2. 产出一个真能过闸门的重写，带上这个 id
3. `selfopt.add_domain(entry)` —— `scenario` / `witness` / `rewrite_hint` —— 然后 `reload_domains()`

```python
selfopt.growth_signals()   # 候选池计数 + 就绪标志
selfopt.add_domain(entry)  # 幂等，热重载
selfopt.reload_domains()
```

> ⚠️ **0 信号 ≠ 没事干。** 静态分析器只认 4 个域。`sort-small-net`、`lru-cache-pure` 和生长出的域它看不见。你停在"没发现"就成 bug 了。

## 边界（诚实）

- **不改写你的源码。** 它标热点、给重写设闸。改不改是人/agent 决定，带备份。
- **不是性能分析器。** 不做运行时采样，不猴子补丁。
- **Python 3.13+ 跳过 `dict-dispatch` 重写** —— 实测是回退不是胜利。
- **静态热点数不是收益估计。** 92 候选 → 0 放行。

## 自动触发（可选）

`scripts/selfopt-hook.py` 跑 `auto_scan()`，把热点交还你的 agent。挂到 `session end` / `after tool write`（WorkBuddy、OpenClaw）、LangChain/CrewAI 回调，或者直接在 agent 的常驻指令里写"产出 Python 代码后跑 auto_scan 给任何重写设闸"。

```bash
python3 scripts/selfopt-hook.py          # 一次性触发
python3 scripts/selfopt-hook.py --ask    # 交互：全扫还是选目标？优化哪个？
```

## 环境变量

| 变量 | 作用 |
|---|---|
| `SELFOPT_SCAN_ROOT` | hook 脚本传给 `auto_scan()` 的扫描根（默认 cwd） |
| `SELFOPT_DATA_DIR` | 重定向库/候选文件（测试用 `/tmp/...`） |

## 许可证

MIT
