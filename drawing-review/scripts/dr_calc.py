"""安全的复算表达式求值：让脚本算数，模型只负责从图纸/计算书里取数和判断。

只允许数字、四则运算、乘方、括号和下列函数/常数，不执行任何其他代码。
单位不做自动换算：表达式里的数值要先统一单位（例如都用 mm、N），结果单位由调用方写明。

钢筋常用：
  As(d, n=1)      n 根直径 d 的钢筋面积（mm²）            As(25, 6) = 2945.2
  As_s(d, s)      直径 d、间距 s 的每米钢筋面积（mm²/m）   As_s(25, 150) = 3272.5
  rho(As, b, h0)  配筋率 As/(b·h0)
"""
from __future__ import annotations

import ast
import math
import operator

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _As(d, n=1):
    return n * math.pi * d * d / 4


def _As_s(d, s):
    if s <= 0:
        raise ValueError("间距必须大于 0")
    return 1000 / s * math.pi * d * d / 4


def _rho(As, b, h0):
    if b <= 0 or h0 <= 0:
        raise ValueError("截面尺寸必须大于 0")
    return As / (b * h0)


FUNCS = {
    "sqrt": math.sqrt, "abs": abs, "min": min, "max": max, "round": round, "log": math.log, "log10": math.log10, "exp": math.exp,
    "sin": math.sin, "cos": math.cos, "tan": math.tan, "radians": math.radians, "degrees": math.degrees,
    "sind": lambda a: math.sin(math.radians(a)), "cosd": lambda a: math.cos(math.radians(a)), "tand": lambda a: math.tan(math.radians(a)),
    "As": _As, "As_s": _As_s, "rho": _rho,
}
CONSTS = {"pi": math.pi, "e": math.e}


def evaluate(expr: str) -> float:
    expr = (expr or "").strip().replace("×", "*").replace("÷", "/").replace("^", "**").replace("π", "pi").replace("，", ",")
    if not expr:
        raise ValueError("表达式为空")
    if len(expr) > 300:
        raise ValueError("表达式过长")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"表达式语法错误：{exc.msg}") from None

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            a, b = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and (abs(b) > 10 or abs(a) > 1e6):
                raise ValueError("乘方过大")
            try:
                return _BIN[type(node.op)](a, b)
            except ZeroDivisionError:
                raise ValueError("除以 0") from None
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _UNARY[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Name) and node.id in CONSTS:
            return CONSTS[node.id]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCS and not node.keywords:
            return FUNCS[node.func.id](*[ev(a) for a in node.args])
        raise ValueError(f"不支持的写法：{ast.dump(node)[:40]}")

    try:
        val = ev(tree)
    except (TypeError, OverflowError, ValueError) as exc:
        raise ValueError(str(exc)) from None
    if isinstance(val, complex) or not math.isfinite(val):
        raise ValueError("结果不是有限实数")
    return float(val)
