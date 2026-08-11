# zhi-py-opt — Smart Python Optimizer

A self-optimizing toolkit for AI agents. LLM identifies hot Python functions, generates faster equivalents, and a validation gate verifies equivalence before admitting them into a reusable library. **The library grows with every task you do.**

[中文说明见下方](#中文说明)

---

## What it does

| Layer | Who | What |
|-------|-----|------|
| Layer 1 Candidate generation | **LLM agent** | Reads code, spots optimization opportunities, generates faster versions |
| Layer 2 Validation gate | `selfopt.adopt` | Checks equivalence using domain witness sets; rejects if not proven |
| Layer 3 Cost evaluation | `selfopt.benchmark` | Measures actual speedup; only admits if threshold met |

The agent is the generator; this module is the validator and scorer. Generation can be wrong — validation cannot. That's the first principle.

## Install

```bash
# From GitHub Agent Skills
gh skill install {owner} zhi-py-opt

# From SkillHub (CN)
skillhub install zhi-neng-py-jiao-ben-you-hua --namespace user_c18b02ff
```

Python standard library only — no pip install needed.

## Quick start

```python
import sys; sys.path.insert(0, "<skill>/scripts")
import selfopt

# LLM generates faster_fn, gate validates and stores
ok = selfopt.adopt("my_hot_fn", slow_fn, faster_fn, "str-join")
# ok["ok"] == True → stored; False → check ok["stage"] and ok["note"]
```

```bash
python scripts/selfopt.py report     # See library + candidate pool
python scripts/analyzer.py file.py   # Static scan for hotspots
python scripts/selfopt-hook.py       # Auto-scan cwd, return hotspots to agent
```

Includes AST-based static analyzer (covers str-join, regex-precompile, list-to-set-membership, dict-dispatch), runtime profiler, and interactive scan hooks.

## License

MIT

---

## 中文说明

# selfopt — AI 智能体自优化种子包

一个当前就能装进 AI 智能体用、能产生实测加速、且会随用户负载自我生长的小型优化框架。**不做大而全，只留引子。**

## 三层分工

| 层 | 由谁做 | 做什么 |
|----|--------|--------|
| Layer 1 候选生成 | **LLM 智能体本身** | 读代码、识别可优化点、生成重写版本 |
| Layer 2 验证门 | `selfopt.adopt` | 按域证人集查等价性，过不了就拒绝入库 |
| Layer 3 代价评估 | `selfopt.benchmark` | 实测耗时，达标才入 `library.jsonl` |

智能体是生成器，本模块是验证器和计分器。生成可以出错，验证不能缺席。

## 安装

解压后把 `selfopt/` 目录放到你的 Agent 的**技能目录**下即可：

- WorkBuddy：`~/.workbuddy/skills/selfopt/`
- OpenClaw：ClawHub 技能目录或自定义 skills 路径
- 其他 Python 系 Agent：任何会被该 Agent 扫描为技能的目录

```bash
unzip selfopt-skill.zip -d ~/.workbuddy/skills/
```

仅依赖 Python 标准库，无需 pip install。

## 用法

```python
import sys; sys.path.insert(0, "<skill>/scripts")
import selfopt

# LLM 生成 faster_fn 后，过闸门入库
ok = selfopt.adopt("my_hot_fn", slow_fn, faster_fn, "str-join")
# ok["ok"] == True → 已入库；False → 看 ok["stage"] 和 ok["note"]
```

```bash
python scripts/test_growth.py     # 验证引子能健康发芽（自带隔离，不污染真实库）
python scripts/selfopt.py report  # 看库里有什么、候选池有什么
python scripts/analyzer.py file.py  # 静态扫描热点，定位可在哪优化
```

## 完整工作流

```python
import sys; sys.path.insert(0, "<skill>/scripts")
import selfopt

# 1) 分析器定位热点（随包发，消费方不用自己写分析器）
for fd in selfopt.analyze_file("your_script.py"):
    print(fd["function"], fd["domain"], fd["line"], fd["hint"])

# 2) LLM 在标出的位置生成更快版本，过闸门入库
ok = selfopt.adopt("hot_fn", slow_fn, faster_fn, "str-join")
# ok["ok"] == True → 已入库；False → 看 ok["stage"] 和 ok["note"]
```

分析器（`scripts/analyzer.py`）用 AST 静态扫描，覆盖最常提的 4 类场景：
字符串拼接、循环内正则编译、列表成员检测、if-elif 长链分派。

**降噪 + 开放式**：每条发现带 `confidence`（high/low）+ `reason`，最基础的类型
推断只用来打标不拍板——`auto_scan` 默认只看 high（自动挡误报），人工审查传
`min_confidence="low"` 看全部。规则不写死在核心：各家误报长什么样只有自家大模型
清楚，故留公开 `CONFIDENCE_RULES` 列表，大模型可 `append` 自家规则。规则返回值
契约：返回 `analyzer.SUPPRESS`=抑制该发现；返回 `None`=不改动（继续下一条规则）；
返回 `"high"/"low"`=覆盖置信度。随包发 `scripts/analyzer_rules_example.py` 复制即用。

## 自动触发：说工作就自动跑

包本身是被动工具箱，差一个触发器层才会"还要手动喂 py"。补上后，Agent 做完代码任务自动扫描、标热点、长库：

```python
import selfopt
hits = selfopt.auto_scan()        # 不传参 = 扫当前工作目录，自动跳过 .venv/node_modules 等
```

现成触发脚本 `scripts/selfopt-hook.py` 已随包发，宿主在合适事件上调用即可：

```bash
python scripts/selfopt-hook.py    # 扫 cwd，把热点打印交回 Agent
```

- WorkBuddy / OpenClaw：挂到 `session end` 或 `after tool write` 钩子。
- LangChain / CrewAI：工具执行后 callback 里调用 `selfopt.auto_scan()`。
- 最轻量：把"产出 Python 代码后跑 auto_scan + 对热点 adopt 入库"写进常驻记忆，Agent 自发执行。

## 成长机制

- 未知域的优化尝试 → 记进 `data/candidates.jsonl`
- `selfopt.growth_signals()` 自动检测候选达到阈值（默认 3 次）的域
- LLM 蒸馏新域条目追加进 `domains.json`，`selfopt.add_domain()` 幂等写入并热重载
- 每个用户的库随自己的负载生长

## 实战注意

- 样本要代表真实规模（加速比有交叉点，小样本可能误杀好优化）
- 域的默认证人常不够用，必要时传 `samples=真实数据`
- 测试隔离：设 `SELFOPT_DATA_DIR=/tmp/xxx` 后再 import，避免污染真实库

详见 `SKILL.md`。
