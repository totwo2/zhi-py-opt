---
name: selfopt
slug: selfopt
displayName: AI 智能体自优化
summary: "LLM 生成重写，程序用真证人 + 配对实测把关；过不了验证不许入库、不许改源码"
description: >
  AI 说"这样改会更快"——selfopt 把这句话拉到真机上验证：真证人查等价性（含边界与非法输入，
  失败直接返回首个反例）、配对交替实测查加速比 + 统计检验，双条件都过才入库。
  Python 热点可实测；ts/js/go/java/c/cs/rb/php/rs/sh 等 10 种语言只报形态、不背书任何加速比。
  触发词：selfopt、自优化、代码优化验证、AI 改的代码靠谱吗、验证优化建议、
  python 提速、脚本优化、热点扫描、优化会不会改错
agent_created: true
version: 2.1.0
read_when:
  - AI/同事/你自己给出一段"更快"的重写，需要确认它没改错、真的更快
  - 要扫描本地代码库找可优化热点（Python 及 10 种其他语言）
  - 想让 agent 每次写完代码自动过一遍优化验证，库随负载生长
  - 遇到已知域覆盖不到的优化模式（成长机制触发点：必须去长域，不许只报"无优化"就结束）
allowed-tools:
  - Read
  - Grep
  - Glob
  - Bash（仅限运行本 skill 目录下脚本的只读与验证命令）
---

# selfopt — AI 智能体自优化

LLM 负责生成更快的重写，本模块负责**验证与计分**：真证人查等价性、配对实测查加速比，双条件都过才入库。
**核心价值是防错，不是提速** —— 生成可以出错，验证不能缺席。
只标热点 + 把关，**绝不未经 `adopt` 验证就改写源码**。

## 用法

```bash
# 安装：放到 agent 的 skills 目录即可（纯标准库，无需 pip install）
# 也可以克隆到任意目录，把 scripts/ 加进 sys.path 就行
git clone https://github.com/totwo2/selfopt.git ~/.workbuddy/skills/selfopt
```

```python
import sys, os
sys.path.insert(0, os.path.expanduser("~/.workbuddy/skills/selfopt/scripts"))
import selfopt

# 1) 找热点
for fd in selfopt.analyze_file("your_script.py"):
    print(fd["function"], fd["domain"], fd["line"], fd["hint"])

# 2) 你生成 faster_fn，由 selfopt 决定要不要
ok = selfopt.adopt("my_hot_fn", old_fn, new_fn, "str-join")
# ok["ok"] = True  → 入库，带实测加速比与统计背书
# ok["ok"] = False → 看 ok["stage"]（verify / perf / domain）与 ok["note"]、ok["counterexample"]
```

| 目的 | 命令 |
|---|---|
| **装完先跑这个（自测）** | `python3 scripts/selfopt.py selftest` → 6 PASS |
| 多语言扫描器自测 | `python3 scripts/polyglot.py --test` → 11 PASS |
| 看内置演示（含验证拒绝错误重写） | `python3 scripts/selfopt.py` |
| 看库里有什么 / 候选池有什么 | `python3 scripts/selfopt.py report` |
| 全语言全量扫描 + 分布汇总 | `python3 scripts/selfopt.py sweep <root>` |
| 交互式：问目标 → 扫 → 问优化哪几个 | `python3 scripts/selfopt.py scan` |
| 静态扫 `.py`（AST + 类型推断） | `python3 scripts/analyzer.py file.py` |
| 静态扫非 Python 文件 | `python3 scripts/polyglot.py file.ts` |
| 真实负载实测各域值多少 | `python3 scripts/bench_real.py`（自证：`--test`） |
| 把扫出的热点逐条过验证 | `python3 scripts/batch_adopt.py [N]` |
| 自动触发（挂宿主钩子） | `python3 scripts/selfopt-hook.py`（交互版 `--ask`） |
| 域覆盖不到时立新域 | `selfopt.add_domain(entry)` + `selfopt.reload_domains()` |
| 看候选池/成长信号 | `selfopt.growth_signals()` |

**装完验证**：`python3 scripts/selfopt.py selftest` 必须 6 PASS，失败说明环境或包有问题。

## 怎么选

只问一件事：**这段重写能构造出成对的、可调用的函数吗？**

| 情况 | 走法 |
|---|---|
| Python 函数，能构造 old/new 两个可调用对象 | → `adopt()` 过验证，拿带统计背书的结论 |
| 非 Python 语言（ts/js/go/java/c/cs/rb/php/rs/sh…） | → 只能扫形态，`gate="unverified"`，**不许给加速比** |
| 依赖跨模块/第三方对象，构造不出来 | → 别硬套。用 `bench_real.py` 按真实负载测，或跳过 |

每条发现带 `gate`，**读它，再决定能不能谈数字**：`gate="python"` 能进 `adopt` 实测；
`gate="unverified"` 只有形态信号，**selfopt 不为它背书任何加速比**（非 Python 代码无法在 Python 进程里构造成对可调用对象，硬套就是编数字）。

种子域 6 个：`str-join`、`regex-precompile`、`list-to-set-membership`、`dict-dispatch`、`sort-small-net`、`lru-cache-pure`。
`domains.json` 是普通 JSON，可直接编辑追加。

## 成长机制（域覆盖不到时必须走，不许只报"无优化"就结束）

种子域只有 6 个，**一定覆盖不到所有模式**。覆盖不到不是"没活干"，是"该长新域了"。

触发条件（任一）：`adopt` 返回 `{"stage":"domain","note":"未知域..."}`；或你想优化的模式不属于上述 6 域；
或 `auto_scan` 返回 0 信号但你读代码后识别出可优化模式。

必做动作：
1. 起一个描述性 `domain_id`（如 `io-parse-cache`、`json-memo`）；
2. 生成一个**真能过验证**的等价重写，用新 `domain_id` 调 `adopt` 进候选池；
3. 该重写真能过 → **当场** `add_domain(entry)`（`scenario` / `witness` / `rewrite_hint` 三件套缺一不可）+ `reload_domains()`。**立域不等 3 次，当场就立。**

⚠️ **0 信号 ≠ 无优化**：静态 analyzer 只能检测 4 类（`str-join` / `regex-precompile` / `list-to-set-membership` / `dict-dispatch`），
`sort-small-net`、`lru-cache-pure` 和长出的新域它扫不到。0 信号时必须主动研判代码。

## 三条会污染库的坑（照做）

1. **`list-to-set-membership` 的样本禁用集合字面量**（`x in {"a","b","c"}`）：CPython 3.2+ 会在编译期折叠成 `frozenset` 常量，
   微基准报出**虚假加速**。要用运行时变量列表（如 `lst = list(range(8))`）。真正的优化是把 `set(...)` 提到循环外只建一次。
2. **样本要代表真实规模**：加速比依赖输入大小，样本太小过不了门槛，把划算的优化误杀。
3. **传自定义 `samples=` 时，边界由调用方负责**：传了就跳过域的默认证人（含边界样本），必须自己带空/极值/非法形态。

## 适用边界（什么时候别用）

- **非 Python 语言**：只报形态，不给加速比（`gate="unverified"`）。
- **Python 3.13+ 的 `dict-dispatch` 默认不值得重写**：自适应特化后 elif 链已被优化好，实测重写普遍倒退。
- **依赖 BaseModel / asyncio / logger 等不可静态重建的函数**：构造不出证人，给不出结论，别硬跑。
- **别拿静态热点条数推断收益**：实测 2297 文件 → 1018 热点 → 92 条可验证 → **通过 0 条**。条数 ≠ 收益。
- **不替代 profiler**：不做运行时采样、不做 monkey-patch。

## 环境变量

| 变量 | 作用 |
|---|---|
| `SELFOPT_SCAN_ROOT` | hook 脚本传给 `auto_scan()` 的扫描根目录，默认 cwd |
| `SELFOPT_DATA_DIR` | 把库/候选写到别处（测试用 `/tmp/xxx`，避免污染真实库） |

## 验证做了什么（结论；统计细节见 `docs/math-gate.md`）

1. **等价性**：随机 + 边界混合证人；两者抛同类型异常也算等价（不误杀合法重写）；失败返回首个反例，一次改对。
2. **性能**：同轮内 old/new 交替测量（控环境漂移）+ 抗异常值的加速比估计 + 统计检验，双条件都过才入库。
3. **版本感知**：记录 `py_version`，旧版本记录提示可能已失效（3.13+ 特化会吃掉传统技巧收益）。
4. **作废机制**：复核失败用 `retract()` 追加作废清单，**不删库**，审计留痕。
