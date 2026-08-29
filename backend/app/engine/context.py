"""Run context, template interpolation, and condition evaluation.

Workflow definitions are user-authored JSON, so both the `{{...}}` templating and the
`when` conditions have to be safe against arbitrary input. Conditions are parsed into
an AST and evaluated against a whitelist of node types — never `eval`.
"""

from __future__ import annotations

import ast
import operator
import re
from dataclasses import dataclass, field
from typing import Any

TEMPLATE_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")

_COMPARISONS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}


@dataclass
class RunContext:
    """Everything a module can read about the run it is executing in.

    `steps` accumulates as the run progresses: `steps["screen"]["output"]["score"]`
    is how a later step reads an earlier one's result.
    """

    run_id: int
    candidate: dict[str, Any] = field(default_factory=dict)
    job: dict[str, Any] = field(default_factory=dict)
    application: dict[str, Any] = field(default_factory=dict)
    steps: dict[str, dict[str, Any]] = field(default_factory=dict)
    trigger: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False

    def as_mapping(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "candidate": self.candidate,
            "job": self.job,
            "application": self.application,
            "steps": self.steps,
            "trigger": self.trigger,
        }

    def record(self, step_id: str, output: dict[str, Any]) -> None:
        self.steps[step_id] = {"output": output}

    # -- templating ---------------------------------------------------------

    def resolve(self, path: str) -> Any:
        """Resolve a dotted path such as `steps.screen.output.score`."""
        node: Any = self.as_mapping()
        for part in path.split("."):
            if isinstance(node, dict):
                node = node.get(part)
            elif isinstance(node, list) and part.isdigit():
                idx = int(part)
                node = node[idx] if idx < len(node) else None
            else:
                node = getattr(node, part, None)
            if node is None:
                return None
        return node

    def render(self, value: Any) -> Any:
        """Interpolate `{{...}}` in strings, recursing through dicts and lists.

        A string that is *only* a placeholder resolves to the underlying typed value
        (so `"{{steps.screen.output.score}}"` yields the number 8, not "8"). Mixed
        strings render as text.
        """
        if isinstance(value, str):
            whole = TEMPLATE_RE.fullmatch(value.strip())
            if whole:
                return self.resolve(whole.group(1))
            return TEMPLATE_RE.sub(
                lambda m: _stringify(self.resolve(m.group(1))), value
            )
        if isinstance(value, dict):
            return {k: self.render(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.render(v) for v in value]
        return value

    # -- conditions ---------------------------------------------------------

    def evaluate(self, expression: str) -> bool:
        """Evaluate a `when` condition.

        Placeholders are substituted with their literal repr first, then the result is
        parsed and walked as an AST restricted to comparisons, boolean operators, and
        literals. Anything else raises.
        """
        substituted = TEMPLATE_RE.sub(
            lambda m: repr(self.resolve(m.group(1))), expression
        )
        try:
            tree = ast.parse(substituted, mode="eval")
        except SyntaxError as exc:
            raise ValueError(f"invalid condition {expression!r}: {exc}") from exc
        return bool(_eval_node(tree.body))


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ", ".join(_stringify(v) for v in value)
    return str(value)


def _eval_node(node: ast.AST) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval_node(e) for e in node.elts]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval_node(node.operand)
    if isinstance(node, ast.BoolOp):
        values = [_eval_node(v) for v in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            handler = _COMPARISONS.get(type(op))
            if handler is None:
                raise ValueError(f"unsupported comparison {type(op).__name__}")
            right = _eval_node(comparator)
            # A missing value makes the comparison false rather than raising, so a
            # skipped upstream step degrades gracefully instead of failing the run.
            if left is None or right is None:
                if not isinstance(op, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)):
                    return False
            if not handler(left, right):
                return False
            left = right
        return True
    raise ValueError(f"unsupported expression node {type(node).__name__}")
