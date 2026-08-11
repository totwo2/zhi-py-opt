---
name: zhi-py-opt
slug: zhi-py-opt
version: 2.0.1
displayName: Smart Python Optimizer
summary: "AI agent self-optimizing seed pack: LLM generates rewrites + programmatic validation gates. Library grows with user workload."
description: |
  An AI agent self-optimization toolkit. LLM identifies hot Python functions, generates faster equivalents, and a validation gate verifies equivalence before admitting them into a reusable library. The library grows automatically with each task. Includes AST-based static analyzer, runtime profiler, and interactive scan hooks.
  Triggers: optimize python, speed up script, analyze hot functions, auto-scan python, self-optimize.
  AI 智能体自优化种子包：LLM 生成重写 + 程序把关入库，库随用户负载生长。包含 AST 静态分析器、运行时 profiler、交互式扫描钩子。
  触发词：优化python、加速脚本、分析热点函数、自动扫描、自优化
agent_created: true
read_when:
  - Writing or optimizing repeatedly-called Python functions/scripts
  - Wanting repetitive work to auto-accelerate and be verifiable
  - Maintaining domain-level optimization templates that self-grow
  - 写或优化会被反复调用的 Python 函数/脚本时
  - 想让重复劳动自动变快、变可验证
  - 维护一批域级优化模板并希望它能自我生长
---

# selfopt — 自优化种子包

一个**当前就能装进 AI 智能体用、能产生实测加速、且会随用户负载自我生长**的小型优化框架。不追求大而全，只做引子。

## 三层分工

| 层 | 由谁做 | 做什么 |
|----|--------|--------|
| Layer 1 候选生成 | **LLM 智能体本身** | 读代码、识别可优化点、生成重写版本 |
| Layer 2 验证门 | `selfopt.adopt` | 按域证人集查等价性，过不了就拒绝入库 |
| Layer 3 代价评估 | `selfopt.benchmark` | 实测耗时，达标才入 `library.jsonl` |

智能体是生成器，本模块是验证器和计分器。生成可以出错，验证不能缺席——这是整套设计的第一原则。

## 标准流程

1. **识别**：发现一段会被反复调用的函数（热点）。
2. **重写**：LLM 生成一个更快的等价版本。
3. **把关**：调用 `selfopt.adopt(name, fn_old, fn_new, domain_id)`。它会自动查域 → 取证人 → 验等价 → 测速度。
   - 通过 → 记入 `data/library.jsonl`
   - 未知域 → 记入 `data/candidates.jsonl`（生长素材）
   - 等价不过 → 拒绝，不污染库
4. **成长**：定期 `python scripts/selfopt.py report` 看候选池。候选攒够了，由 LLM 蒸馏成新域条目追加进 `domains.json`。

## 种子域（domains.json）

预置 6 个常见场景，按"证人集完备性"分两类：

- **零一完备**（complete=true）：`sort-small-net` —— 用 `binary_seq` 证人，2^n 覆盖等价于任意输入，正确性有数学保障。门槛可设低（1.02x）。
- **经验采样**（complete=false）：`str-join`、`regex-precompile`、`list-to-set-membership`、`dict-dispatch`、`lru-cache-pure` —— 用随机/补充样本验证，够用但不声称完备。

完整零一原理目前只有排序域有。**为每个新域找到自己的"零一式约简"是这套库能长多大的上限**——这是留给未来研究的地方，种子不强行解决。

## 成长机制（引子）

- `domains.json` 是普通 JSON，LLM 可直接编辑追加新域条目。
- 新域需要三件东西：`scenario`（怎么识别）、`witness`（怎么验证）、`rewrite_hint`（怎么改）。
- `witness` 可以指定 `binary_seq`（数学完备）、`rand_int` / `rand_int_list` / `rand_str` / `rand_rows`（经验采样），或由调用方在 `adopt` 时传 `samples=` 补充正例（证人集本身也能进化）。
- 候选池里同域出现 3 次以上、且每次重写都不等价，说明这个域值得专门做一个证人集——这是蒸馏新域的信号。

**成长 API**（在 `selfopt` 模块中）：

```python
selfopt.growth_signals()   # 扫描候选池，返回各域计数及 ready 标志
selfopt.add_domain(entry)  # 追加新域到 domains.json + 热重载（幂等）
selfopt.reload_domains()   # 手动重载域库（LLM 外部编辑后调用）
```

## 用法

```python
import sys; sys.path.insert(0, "<skill>/scripts")
import selfopt

# 第 1 步：分析器定位热点（随包发，消费方不用自己写分析器）
for fd in selfopt.analyze_file("your_script.py"):
    print(fd["function"], fd["domain"], fd["line"], fd["hint"])
    # → 例如 compute_discount dict-dispatch L968

# 第 2 步：LLM 在标出的位置生成 faster_fn，过闸门入库
ok = selfopt.adopt("my_hot_fn", old_fn, new_fn, "str-join")
# ok["ok"] == True → 已入库；False → 看 ok["stage"] 和 ok["note"]
```

```bash
python scripts/selfopt.py            # 跑内置 demo
python scripts/selfopt.py report     # 看库里有什么、候选池有什么
python scripts/analyzer.py file.py   # 静态扫描热点
```

分析器（`scripts/analyzer.py`）用 AST 静态扫描，覆盖消费方最常提的 4 类场景：
`str-join`（循环内字符串 +=）、`regex-precompile`（函数内 re.compile）、
`list-to-set-membership`（循环内 `x in list`）、`dict-dispatch`（3+ elif 长链）。
`sort-small-net` 与 `lru-cache-pure` 静态难可靠检测，留给人工或 LLM 判断。
职责边界：分析器只答"热点在哪、疑似哪个域"，改写由 LLM 生成、`adopt` 把关。

**降噪设计（类型推断 + 开放式）**：分析器不做"拍板"，只给信号。每条发现带
`confidence`（high/low）+ `reason`。最基础的过程内类型推断（只看初始化赋值推断
累加器/容器类型）用来打标——例如 `s = ""` 后 `s += x` 判 high，`s = []` 后 `+=`
判 low（疑似误报），`re.compile(常量)` 判 high、模式依赖运行期输入判 low。
`auto_scan` / `selfopt-hook.py` 默认只看 high（自动模式保守，挡住误报）；
人工审查传 `min_confidence="low"` 看全部（含 low 待 LLM 自行研判）。

**规则不写死**：各家场景不同，误报长什么样只有各家大模型自己清楚。因此核心只放
最通用的判定，另留公开的 `CONFIDENCE_RULES` 列表，大模型可就地追加自家规则来抑制
误报或加自家判定，无需改动核心。规则返回值契约（务必分清，否则会误杀）：

```python
import analyzer
def my_rule(finding, fn, infer):
    if finding["domain"] == "str-join" and "FrameworkBuf" in fn.name:
        return analyzer.SUPPRESS   # 明确抑制该发现（自家确认的误报）
    return None                    # 不改动，交给下一条规则（不要用来"杀"）
    # return "high" / "low"        # 覆盖置信度
analyzer.CONFIDENCE_RULES.append(my_rule)
```

随包另发 `scripts/analyzer_rules_example.py`——一份复制即用的规则模板（抑制框架
特化累加器、抑制测试函数、把一次性初始化的分派降为 low），`import` 后调一次
`register()` 即生效。消费方按自家情况删改即可，不单独成技能，和 selfopt 放一起。

## 自动触发：说工作就自动跑

"安装后还要手动喂一个 .py 让我分析"——这不科学。根因是**本包原本是被动工具箱，缺一个触发器层**，跟"预装还是后装"无关。补上这层后，流程就变成：你只说"干活"，Agent 做完代码类任务自动去扫描、标热点、长库。

两步即可接线：

**1) 自动扫描（无需手动路径）**——`selfopt.auto_scan()` 会扫当前工作目录（或 `SELFOPT_SCAN_ROOT`）下的 `.py`，自动跳过 `.venv/node_modules/__pycache__/.git/site-packages/.workbuddy`，返回聚合热点清单：

```python
import selfopt
hits = selfopt.auto_scan()                 # 不传参 = 扫 cwd
for h in hits:
    print(h["file"], h["line"], h["domain"], h["function"], h["hint"])
```

**2) 触发器示例**——`scripts/selfopt-hook.py` 就是现成的触发脚本：跑 `auto_scan()`、把热点打印出来交回 Agent。宿主只需在合适的事件上调用它：

```bash
python scripts/selfopt-hook.py             # 手动触发一次（等价于"让 Agent 自己跑一遍"）
```

接入方式（各宿主通用，脚本不依赖任何私有机制）：

- **WorkBuddy / OpenClaw**：挂到 `session end` 或 `after tool write` 钩子，钩子里 `python <skill>/scripts/selfopt-hook.py`。
- **LangChain / CrewAI**：在工具执行后的 callback 里调用 `selfopt.auto_scan()`，把结果喂回 LLM 决定要不要改写。
- **最轻量（无需改框架）**：把"每次产出 Python 代码后跑一遍 auto_scan，对热点生成更快版本并 adopt 入库"写进 Agent 的常驻记忆/系统提示，它就会自发去做。

接上之后，用户视角就是：**说工作 → Agent 干活 → 顺手把活出的代码过一遍优化闸门，库随每次工作默默生长**。分析报告里会显示新发现的域热点；Agent 决定采纳时再 `adopt()` 把关，绝不未经验证就改写源码。

## 友好扫描流程（新旧 py 通用）

无论是**对话中新生成的 .py**还是**仓库里已有的旧 .py**，都用同一套友好流程，差别只在"什么时候触发"：

**两句话对话框**（已在 `selfopt-hook.py --ask` / `selfopt.interactive_scan()` 实现，CLI 用 `input()`，宿主 Agent 换自家对话框即可）：
1. **扫描前**：先问"有目标还是全量？"——目标可以是模糊路径片段，也可以是函数名。`auto_scan(target=...)` 先按路径/文件名模糊匹配，匹配不到再全扫按函数名过滤。这样用户给个方向，我们就只找那一块，不惊动整个仓库。
2. **找到后**：把热点按 (域, 函数) 聚合、按频率排序，`suggest_targets()` 标出"●高频"组（同形态出现 ≥2 次），再问"要优化哪几个？"——高频且高置信的优先问。用户勾选后，Agent 才在对应位置生成更快版本并 `adopt()` 把关。

```bash
python scripts/selfopt-hook.py --ask     # 友好交互：先问目标/全量，再问高频是否优化
python scripts/selfopt.py scan           # 同上（自带的 scan 子命令）
```

**新 .py（对话中生成）该走"先优化再落地"还是"先跑再优、第二次才好"？**
推荐**混合**，不要二选一卡死：
- **明显的结构性热点 → 写代码当时就 adopt**：`str-join`、`re.compile(常量)` 这类，等价性由证人集/语法结构保证，不依赖真实运行数据，生成时分析器标 high 就能直接过关入库，用户第一次拿到就是优化版。
- **"到底热不热 / 要多大样本才赢" → 先跑起来，第二次再优**：优化收益是输入规模敏感的（交叉点问题），且你只有代码跑过、见过真实负载，才知道它是否真热、该喂多大样本。所以**不阻塞交付**——先写出来跑（第一次未优化只是常数因子，摊到多次调用里可忽略），等真实负载出来，再对那些"确实热"的做优化并入库；从此相关调用走优化版。
- 一句话：**库随对话持续累积，不是每生成一次都要先过我们这关；但明显的赢当场就拿，热度的赢等跑出来再拿。**

**旧 .py（仓库已有）**：就是上面的"友好扫描流程"——手动触发 `--ask` → 问目标/全量 → 扫 → 问高频是否优化 → adopt。绝不在用户没确认时改源码。

无论新旧、何种模式，都只"标热点 + 交回 + 把关"，**绝不未经 `adopt` 验证就改写源码**；真要应用到文件，由 Agent 在用户确认后做并保留备份。

## 热点定位三件套

**1. 分析器（AST 静态扫描）**——快但不全：

```bash
python scripts/analyzer.py your_script.py              # 全量（含 low）
python scripts/analyzer.py your_script.py --high       # 仅 high 置信
```

输出示例：`L122 [list-to-set-membership/high] fetch_toc: 右值为列表且循环内不变，转 set 后 in 由 O(n)→O(1)`

分析器擅长：`str-join`、`regex-precompile`、`list-to-set-membership`、`dict-dispatch` 的结构性热点。
分析器盲区：`sort-small-net`、`lru-cache-pure` 静态难可靠检测，留给 LLM 或人工判断。

**2. Profiler（运行时采样）**——全但不精确：

```bash
python scripts/profiler.py your_script.py                    # 跑脚本，输出热点
python scripts/profiler.py your_script.py --threshold 1.0   # 阈值 1%，更全
python scripts/profiler.py -m your_module --args arg1 arg2   # 跑模块
```

输出示例：
```
累计%    自身ms    调用次数  函数  [建议域]
 12.3%     8.10ms        1  fetch_toc()  [list-to-set-membership]
  5.7%     3.45ms      120  compile()  [regex-precompile]
```

Profiler 把"代码热点"直接映射到 selfopt 域建议。精确度不高（只看函数名猜），但能快速圈定范围。

**3. LLM 代码审查**——唯一能发现「业务逻辑级」热点的途径：

分析器和 profiler 都只能发现「模式匹配级」热点（如 += 循环、re.compile 内联）。真正的性能杀手往往是「数据结构遍历冗余」、「重复计算」、「不必要的 I/O 等待」——这些需要理解业务逻辑才能发现。

**定位策略**：先用 profiler 圈定热点函数，再用 LLM 读源码判断「这个函数慢在模式匹配还是业务逻辑」，后者决定是否值得 selfopt 优化。

## 实战注意事项

### 样本与 benchmark 指导

加速比依赖输入大小（交叉点问题）。每个域有自己的「甜蜜点」，样本不在甜蜜点上会误杀或误入。

| 域 | 最小样本量 | 甜蜜点输入 | 倒退风险 | 备注 |
|---|---|---|---|---|
| str-join | 3 组 | 列表长度 >=100 | <100 项时 += 可能更快 | join 有一次性分配开销 |
| regex-precompile | 5 组 | 调用次数 >=100 | 调用 <10 次时差异微小 | 必须传真实数据样本 |
| sort-small-net | 1 组 | n=3~5 | n>8 收益递减 | 零一完备，样本量要求低 |
| list-to-set-membership | 3 组 | 列表长度 >=20 | <4 项时转 set 反而慢 | 元素必须可哈希 |
| dict-dispatch | 3 组 | 分支数 >=5 | 3 分支时 if-elif 可能更快 | dict.get 有哈希开销 |
| lru-cache-pure | 5 组 | 重复调用 >=10 次 | 单次调用无意义 | 输入必须可哈希 |

**方差检测**：每个域至少跑 3 次 benchmark，取中位数。极差 >15% 时加样本到 10 组。
**样本代表性**：rand_str/rand_int 证人是通用的，但 `regex-precompile` 等域必须传 `samples=真实数据`（真实文件路径、真实记录）才有意义。

### 其他

- **测试隔离**：设环境变量 `SELFOPT_DATA_DIR=/tmp/xxx` 后再 import，可把库/候选写到临时目录，避免测试数据污染真实库。
- **域的默认证人往往不够用**：`regex-precompile` 等域的 `rand_str` 证人匹配不到目标模式，必须传 `samples=真实数据`（如真实文件路径、真实记录）才有意义。传入自定义样本时，库记录的 `witness` 标记为 `custom`。
- **自动扫描只给信号，不替你拍板**：`auto_scan()` / `selfopt-hook.py` 返回的热点清单是建议，不是指令。Agent 要根据业务逻辑二次判断哪些值得 adopt。

## 设计约束

- 仅依赖标准库，单文件，任何 Python 系 Agent 都能直接 `import`。
- 不做运行时 monkey-patch：入库的是**记录与证明**，是否应用到源码由 Agent 决定（建议附备份与回滚）。
- 不信任任何未验证的优化，包括 LLM 自己写的——adopt 是唯一闸门。
- 分析器只答「热点在哪、疑似哪个域」，改写由 LLM 生成、`adopt` 把关。
- Profiler 只答「哪个函数慢」，域建议是启发式猜测，最终判断交给 LLM。

## 配方卡速查（domains.json）

每个域的详细配方（trigger_patterns、rewrite_template、pitfall、benchmark_min_n、expected_speedup）见 `domains.json` 的 `recipe` 字段。Agent 在 adopt 前应读取对应域的 recipe，按模板改写，按 pitfall 自检。

```json
{
  "id": "str-join",
  "recipe": {
    "trigger_patterns": ["s = ''\\nfor x in iter:\\n    s += x"],
    "rewrite_template": "parts = []\\nfor x in iter:\\n    parts.append(x)\\nreturn ''.join(parts)",
    "pitfall": "小列表（<100项）时 += 可能更快",
    "benchmark_min_n": 3,
    "expected_speedup": "1.2x~3x"
  }
}
```

## 发布安全

发布到公开仓库前，确保：
- `domains.json` 不含任何凭据或私有数据
- `data/` 目录下的 `library.jsonl`、`candidates.jsonl` 不随包发布（用户库随用户负载生长，不应共享）
- `.gitignore` 排除 `data/`、`__pycache__/`、`.venv/`

