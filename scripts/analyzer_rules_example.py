#!/usr/bin/env python3
"""
selfopt 分析器 · 开放式规则示例模板
==================================
各家智能体的误报长什么样，只有各家自己清楚。本文件是"怎么给分析器加自家
规则"的复制即用例，随包发，但默认不生效——消费方按自家情况删改后 import 并
调用一次 register() 即可。不单独成技能，和 selfopt 放一起。

规则签名：rule(finding, fn, infer) -> 任意
    - 返回 analyzer.SUPPRESS → 明确抑制该发现（这是自家确认的误报）
    - 返回 None              → 不改动，交给下一条规则/保持原置信度
    - 返回 'high'/'low'      → 覆盖该发现的置信度
finding 可用字段：function / domain / line / file / confidence / hint
fn 是 ast.FunctionDef，可看 fn.name / 参数名；infer 是 {变量: 类型} 推断表。

用法：
    import analyzer_rules_example as ex
    ex.register()                       # 注册下面全部规则
    # 或只取其中一条：analyzer.CONFIDENCE_RULES.append(ex._suppress_tests)
"""
import analyzer


def _suppress_framework_buffers(finding, fn, infer):
    """示例1：自家框架的特化累加器，str-join 是误报 → 抑制"""
    if finding["domain"] == "str-join" and "FrameworkBuf" in fn.name:
        return analyzer.SUPPRESS
    return None


def _suppress_tests(finding, fn, infer):
    """示例2：测试函数里的热点不是优化目标 → 抑制"""
    if fn.name.startswith("test_") or fn.name == "test":
        return analyzer.SUPPRESS
    return None


def _downgrade_oneshot_dispatch(finding, fn, infer):
    """示例3：一次性初始化里的 if-elif 分派不是热路径 → 降为 low"""
    if finding["domain"] == "dict-dispatch" and "init" in fn.name:
        return "low"
    return None


def register():
    """把示例规则注册进分析器。消费方按需删改后再 register()。"""
    analyzer.CONFIDENCE_RULES.extend([
        _suppress_framework_buffers,
        _suppress_tests,
        _downgrade_oneshot_dispatch,
    ])
    return analyzer.CONFIDENCE_RULES
