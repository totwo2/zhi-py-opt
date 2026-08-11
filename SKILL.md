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
license: MIT
tags:
  - python
  - optimization
  - profiling
  - ast
  - self-improving
allowed-tools: "Read Write Edit Bash Glob Grep WebFetch WebSearch Skill Agent"
---

# zhi-py-opt — Smart Python Optimizer

> AI 智能体自优化种子包：LLM 生成重写 + 程序把关入库，库随用户负载生长。

---

## 1. 触发词（命中即启动）

| 用户说法 | 意图 | 入口 |
|---------|------|------|
| "优化python" / "加速脚本" / "speed up" | 优化Python代码 | selfopt 模块 |
| "分析热点函数" / "找出慢的地方" | 静态/运行时分析 | scripts/analyzer.py / profiler.py |
| "自动扫描" / "auto-scan" | 自动扫描代码库 | selfopt.auto_scan() |
| "自优化" / "self-optimize" | 自动优化并入库 | 完整流程 |
| "跑一下优化" / "看看能不能更快" | 执行优化流程 | 完整流程 |

**注意**：触发词不限于上述 exact match，用户表达类似意图也应启动。

---

## 2. 核心流程（Agent 执行手册）

### 2.1 标准优化流程

```
用户提供代码/路径
  → 1. 分析器扫描（scripts/analyzer.py）
  → 2. Profiler 验证（scripts/profiler.py，可选）
  → 3. LLM 生成重写版本
  → 4. adopt() 验证等价性
  → 5. 入库（data/library.jsonl）或记入候选池（data/candidates.jsonl）
  → 6. 向用户报告结果
```

**详细步骤**：

1. **接收输入**
   - 用户提供 Python 文件路径或代码片段
   - 如果用户提供片段，先写入临时文件

2. **分析热点**
   ```bash
   python scripts/analyzer.py <file.py> --high
   ```
   - 输出：热点函数列表（函数名、行号、建议域、置信度）
   - 如果输出为空，告知用户"未发现明显热点"

3. **生成优化版本**
   - 对每个热点，LLM 根据 domains.json 中的 recipe 生成更快版本
   - 参考 `rewrite_template` 和 `pitfall` 字段

4. **验证入库**
   ```python
   import sys; sys.path.insert(0, "<skill>/scripts")
   import selfopt
   
   ok = selfopt.adopt("fn_name", old_fn, new_fn, "domain_id")
   ```
   - `ok["ok"] == True` → 已入库，告知用户
   - `ok["ok"] == False` → 验证失败，查看 `ok["stage"]` 和 `ok["note"]`，告知用户原因

5. **报告**
   - 成功：显示优化前/后对比、速度提升
   - 失败：说明失败原因（等价性不过、速度不达标等）

### 2.2 自动扫描模式

```bash
# 扫描当前目录
python scripts/selfopt-hook.py

# 友好交互模式
python scripts/selfopt-hook.py --ask
```

**Agent 操作**：
1. 执行 `python scripts/selfopt-hook.py --ask`
2. 解析输出，识别用户选择
3. 对选中的热点执行优化流程
4. 将结果写回报告

### 2.3 批量分析模式

```bash
# 分析单个文件
python scripts/analyzer.py file.py

# 分析多个文件
for f in $(find . -name "*.py"); do
    python scripts/analyzer.py "$f" --high
done
```

---

## 3. 脚本参考

| 脚本 | 用途 | 调用方式 |
|------|------|---------|
| `scripts/selfopt.py` | 主模块，含 adopt/benchmark/growth | `python scripts/selfopt.py` |
| `scripts/analyzer.py` | AST 静态扫描热点 | `python scripts/analyzer.py <file> [--high]` |
| `scripts/profiler.py` | 运行时采样热点 | `python scripts/profiler.py <file> [--threshold 1.0]` |
| `scripts/selfopt-hook.py` | 自动扫描触发器 | `python scripts/selfopt-hook.py [--ask]` |

### 3.1 scripts/selfopt.py

```bash
python scripts/selfopt.py            # 跑内置 demo
python scripts/selfopt.py report     # 看库里有什么、候选池有什么
python scripts/selfopt.py scan       # 自动扫描当前目录
```

**Python API**：
```python
import sys; sys.path.insert(0, "<skill>/scripts")
import selfopt

# 验证并入库
ok = selfopt.adopt("fn_name", old_fn, new_fn, "domain_id")

# 查看候选池
selfopt.growth_signals()

# 添加新域
selfopt.add_domain(entry)

# 重载域库
selfopt.reload_domains()
```

### 3.2 scripts/analyzer.py

```bash
python scripts/analyzer.py your_script.py              # 全量（含 low 置信）
python scripts/analyzer.py your_script.py --high       # 仅 high 置信
```

**输出格式**：
```
L122 [list-to-set-membership/high] fetch_toc: 右值为列表且循环内不变，转 set 后 in 由 O(n)→O(1)
```

**支持检测的域**：
- `str-join`：循环内字符串 +=
- `regex-precompile`：函数内 re.compile
- `list-to-set-membership`：循环内 `x in list`
- `dict-dispatch`：3+ elif 长链

**盲区**（需 LLM/人工判断）：
- `sort-small-net`
- `lru-cache-pure`

### 3.3 scripts/profiler.py

```bash
python scripts/profiler.py your_script.py                    # 跑脚本，输出热点
python scripts/profiler.py your_script.py --threshold 1.0   # 阈值 1%，更全
python scripts/profiler.py -m your_module --args arg1 arg2   # 跑模块
```

**输出格式**：
```
累计%    自身ms    调用次数  函数  [建议域]
 12.3%     8.10ms        1  fetch_toc()  [list-to-set-membership]
  5.7%     3.45ms      120  compile()  [regex-precompile]
```

### 3.4 scripts/selfopt-hook.py

```bash
python scripts/selfopt-hook.py             # 自动扫描当前目录
python scripts/selfopt-hook.py --ask       # 友好交互模式
```

**交互流程**：
1. 问用户：有目标还是全量？
2. 扫描热点
3. 问用户：要优化哪几个？
4. 对选中的执行 adopt()

---

## 4. 错误处理

| 错误 | 原因 | 处理 |
|------|------|------|
| `adopt` 返回 `ok=False` | 等价性验证失败 | 告知用户，不修改源码 |
| `adopt` 返回 `ok=False` | 速度不达标 | 告知用户，不修改源码 |
| 分析器无输出 | 未发现明显热点 | 告知用户"代码已较优，无需优化" |
| Profiler 超时 | 脚本执行时间过长 | 降低 `--threshold` 或缩短样本 |
| 样本不足 | benchmark 样本数不够 | 按 domains.json 的 `benchmark_min_n` 补充样本 |

---

## 5. 设计约束

- 仅依赖标准库，单文件，任何 Python 系 Agent 都能直接 `import`
- 不做运行时 monkey-patch：入库的是**记录与证明**，是否应用到源码由 Agent 决定
- 不信任任何未验证的优化：`adopt` 是唯一闸门
- 绝不未经 `adopt` 验证就改写源码

---

## 6. 数据文件

| 文件 | 用途 |
|------|------|
| `domains.json` | 预置域配方（trigger_patterns、rewrite_template、pitfall、benchmark_min_n） |
| `data/library.jsonl` | 已验证通过的优化记录（用户私有，不随包发布） |
| `data/candidates.jsonl` | 待验证候选（用户私有，不随包发布） |

**注意**：`data/` 目录不随包发布，用户库随用户负载生长。

---

## 7. 快速命令参考

```bash
# 扫描热点
python scripts/analyzer.py file.py --high
python scripts/profiler.py file.py --threshold 1.0

# 自动扫描
python scripts/selfopt-hook.py --ask

# 验证优化
python -c "
import sys; sys.path.insert(0, 'scripts')
import selfopt
ok = selfopt.adopt('fn_name', old_fn, new_fn, 'domain_id')
print(ok)
"
```

---

*最后更新：2026-08-11*
