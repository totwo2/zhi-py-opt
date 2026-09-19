# selfopt — the gate your AI's "this is faster" has to pass

[中文](README.zh-CN.md) | English · current version **v2.1.0**

> **This repo (`zhi-py-opt`) IS selfopt** — same project, one codebase. The SkillHub listing still uses the old pinyin slug.

> Your agent rewrote a hot function and claims **1.5x faster**.
> Who checked?
>
> **selfopt puts that claim on the bench** — real witnesses, paired interleaved timing, sign test.
> If it doesn't hold, it never enters the library, and your source stays untouched.

---

## Install it and every change goes through a real-machine gate

Your agent changed a function and says "this is faster." selfopt puts that claim on the bench:

- **Equivalent?** Random witnesses + boundary witnesses (including exception semantics) compare old vs new; not equivalent → rejected, with a counterexample returned.
- **Actually faster?** old/new measured interleaved in the same run (controls for environment drift) + outlier-robust speedup + sign test; both conditions must hold to be admitted.

If it doesn't hold, it never enters the library and your source stays untouched.

## What it has confirmed as genuinely faster (real workloads, p = 0.002)

| Pattern | Speedup |
|---|---|
| regex precompile | **1.9x** |
| `str-join` vs `+=` | **4x** |
| `list` → `set` membership | **1.27x** @ 3 elements → **29–49x** @ 200 elements |
| `dict` dispatch | 1.14x @ 3 branches → **2.96x** @ 12 branches |

> These are **pattern-level** numbers, not "your codebase is N% faster overall" — nobody can honestly give you the latter, and this package won't pretend to.

## The changes in your project that "look optimizable" mostly won't pass

Because the gate above is hard, it catches **the changes your own AI wants to make in your project.** A full sweep of a real working directory (2,297 files) produced 1,018 static hotspots; 92 were verifiable and high-confidence. After each went through the gate:

| Result | Count | Meaning |
|---|---|---|
| **Admitted (proven faster & equivalent)** | **0** | none could be proven on its own |
| Rejected (wrong result) | **1** | mechanical rewrite returned `','` on `[]` where the original returned `''` — **the gate caught it** |
| Witness not constructible | 54 | depends on BaseModel / asyncio / logger etc. |
| Witnesses not adaptable | 31 | function needs specific object shapes |
| Rewrite not generated | 6 | pattern didn't match mechanical rules |

**Not a failure of the rewrites — the gate doing its job.** It tells you those "looks hot" changes are mostly unprovable: no independent witness, or the witness shape doesn't fit, or they're simply wrong (e.g. `str-join` returns `','` instead of `''` on `[]`). Static analysis says *where it looks hot*; it can't say *how much faster, or whether it's correct*.

**What it blocks is worth more than what it admits:**

| What it caught | Real case |
|---|---|
| **Wrong rewrite** | `str-join` rewrite: on `[]` old returns `''`, new returns `','`. Random witnesses passed it; boundary witnesses rejected it. |
| **Noise "speedup"** | Two *identical* functions: old point-estimate gate admitted **1.095x** in 1 of 5 trials. Sign test over 9 pairs: **p = 0.25 → rejected**. |
| **Fake speedup trap** | CPython folds `x in {"a","b","c"}` into a `frozenset` constant **at compile time** → microbenchmark lies. (cf. `Lib/test/test_peepholer.py::test_folding_of_sets_of_constants`) |
| **Silent regression** | Two previously-admitted records re-checked on real workload: **0.896x** and **0.509x** — both slower. Now retracted. |

**Not shipping a wrong rewrite comes first; speed comes second.**

---

## 30-second start

```bash
git clone https://github.com/totwo2/selfopt.git ~/.workbuddy/skills/selfopt
# or clone it anywhere — just point sys.path at its scripts/ directory

# verify it works
python3 ~/.workbuddy/skills/selfopt/scripts/polyglot.py --test   # → 11 PASS
python3 ~/.workbuddy/skills/selfopt/scripts/selfopt.py selftest   # → 6 PASS
```

Pure Python standard library. No `pip install`, no build step.

## Use it

```python
import sys; sys.path.insert(0, "~/.workbuddy/skills/selfopt/scripts")
import selfopt

# 1. find hotspots (Python: AST-based; other languages: pattern-based)
for fd in selfopt.analyze_file("your_script.py"):
    print(fd["function"], fd["domain"], fd["line"], fd["hint"])

# 2. your agent writes a faster version → the gate decides
ok = selfopt.adopt("my_hot_fn", old_fn, new_fn, "str-join")
# ok["ok"] is True  → admitted to library, with statistical backing
# ok["ok"] is False → see ok["stage"] and ok["note"] (and ok["counterexample"])
```

```bash
python3 scripts/selfopt.py               # built-in demo (watch the gate reject a bad rewrite)
python3 scripts/selfopt.py report        # what's in the library / candidate pool
python3 scripts/selfopt.py sweep <root>  # full multi-language sweep + distribution summary
python3 scripts/selfopt.py scan          # interactive: ask target → scan → ask which to optimize
python3 scripts/analyzer.py file.py      # static scan (.py, AST + type inference)
python3 scripts/polyglot.py file.ts      # static scan (non-Python)
python3 scripts/bench_real.py            # measure what each domain is actually worth
python3 scripts/batch_adopt.py [N]       # push swept hotspots through the gate one by one
```

## Language coverage

`.py` via AST + type inference. `.ts .tsx .js .jsx .mjs .cjs .go .java .c .cc .cpp .h .hpp .cs .rb .php .rs .sh .bash .zsh` via a language-agnostic pattern table (loop-depth stack — no N AST implementations).

**Every finding carries a `gate` field — read it before you quote a number:**

| `gate` | Meaning |
|---|---|
| `"python"` | can enter the `adopt` gate and be measured for real |
| `"unverified"` | **shape signal only. selfopt backs no speedup number for it.** Non-Python code can't be constructed as callable pairs inside a Python process; forcing the gate would just fabricate a number. |

Widening the scope did **not** lower the bar. What can't be verified is explicitly labeled unverified.

## The gate (short version)

1. **Equivalence** — random + boundary witnesses (empty / single / extreme / negative / duplicate / non-ASCII / illegal input). Raising the same exception type on both sides counts as equivalent, so legal rewrites aren't killed. Failure returns the **first counterexample** (`[]`, `-1`, …) so the agent fixes the right input in one pass.
2. **Performance** — paired interleaved measurement (controls for environment drift), median speedup, and a **sign test** (`p < 0.05` for empirical domains). Two conditions must both hold.
3. **Version-aware** — every record stores `py_version`; CPython 3.13+ specialization eats several classic tricks, so old records are flagged as possibly stale.
4. **Retraction** — a record that fails re-check goes to `data/retracted.jsonl`. **The library is never silently rewritten**; history is kept for audit.

Math details live in [`docs/math-gate.md`](docs/math-gate.md) — they are not the product.

## Growth: when the 6 seed domains don't cover you

Six seed domains will never cover everything — **and that's the trigger, not the end of the road.**
When `adopt` returns `{"stage": "domain", ...}` or the analyzer reports 0 signals but you can see an optimizable pattern:

1. Give it a descriptive `domain_id` (`io-parse-cache`, `json-memo`, …)
2. Produce a rewrite that actually passes the gate with that id
3. `selfopt.add_domain(entry)` — `scenario` / `witness` / `rewrite_hint` — then `reload_domains()`

```python
selfopt.growth_signals()   # candidate-pool counts + ready flags
selfopt.add_domain(entry)  # idempotent, hot-reloads
selfopt.reload_domains()
```

> ⚠️ **0 signals ≠ nothing to do.** The static analyzer only detects 4 domains. `sort-small-net`, `lru-cache-pure` and grown domains are invisible to it. If you stop at "no findings", you're the bug.

## Boundaries (honest)

- **Does not rewrite your source.** It flags hotspots and gates rewrites. Applying a change stays a human/agent decision, with backup.
- **Not a profiler.** No runtime sampling, no monkey-patching.
- **On Python 3.13+, skip `dict-dispatch` rewrites** — measured regressions, not wins.
- **Static hotspot counts are not a benefit estimate.** 92 candidates → 0 admitted.

## Auto-trigger (optional)

`scripts/selfopt-hook.py` runs `auto_scan()` and hands the hotspots back to your agent. Hook it to `session end` / `after tool write` (WorkBuddy, OpenClaw), a LangChain/CrewAI callback, or just write "after producing Python code, run auto_scan and gate any rewrite" into your agent's standing instructions.

```bash
python3 scripts/selfopt-hook.py          # one-shot trigger
python3 scripts/selfopt-hook.py --ask    # interactive: target or full scan? which to optimize?
```

## Env vars

| Var | Effect |
|---|---|
| `SELFOPT_SCAN_ROOT` | scan root the hook script passes to `auto_scan()` (default: cwd) |
| `SELFOPT_DATA_DIR` | redirect library/candidate files (use `/tmp/...` for tests) |

## License

MIT
