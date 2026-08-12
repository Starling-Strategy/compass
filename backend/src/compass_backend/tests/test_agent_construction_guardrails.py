"""Static guardrails for production Pydantic AI Agent construction."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class AgentConstructor:
    """One production `Agent(...)` call found under src/compass_backend."""

    path: str
    function_name: str
    call: ast.Call

    @property
    def key(self) -> tuple[str, str]:
        return (self.path, self.function_name)


def test_production_agents_use_structured_output_hooks_and_settings() -> None:
    """Active Compass agents must keep the Pydantic AI safety rails wired."""

    constructors = _production_agent_constructors()

    assert {constructor.key for constructor in constructors} == {
        ("answer_layer/agent.py", "build_answer_stylist_agent"),
        ("answer_layer/clarify_agent.py", "build_clarify_stylist_agent"),
        ("agents/criterion_classifier.py", "build_criterion_classifier_agent"),
        ("agents/judge.py", "build_judge_agent"),
        ("catalog/adjudication.py", "create_catalog_adjudicator_agent"),
        ("planning/planner.py", "create_planner_agent"),
    }

    for constructor in constructors:
        keywords = {
            keyword.arg: keyword.value
            for keyword in constructor.call.keywords
            if keyword.arg is not None
        }
        assert "output_type" in keywords, f"{constructor.key} has no structured output_type"
        assert _contains_call(
            keywords.get("capabilities"),
            "build_diagnostic_hooks",
        ), f"{constructor.key} does not use build_diagnostic_hooks(...)"
        assert _has_named_settings_path(constructor), (
            f"{constructor.key} does not use a named Compass model-settings path"
        )


def _production_agent_constructors() -> list[AgentConstructor]:
    constructors: list[AgentConstructor] = []
    for path in sorted(ROOT.rglob("*.py")):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        visitor = _AgentCallVisitor(path.relative_to(ROOT).as_posix())
        visitor.visit(tree)
        constructors.extend(visitor.constructors)
    return constructors


class _AgentCallVisitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.function_stack: list[str] = []
        self.constructors: list[AgentConstructor] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.function_stack.append(node.name)
        self.generic_visit(node)
        self.function_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.function_stack.append(node.name)
        self.generic_visit(node)
        self.function_stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id == "Agent":
            function_name = self.function_stack[-1] if self.function_stack else "<module>"
            self.constructors.append(
                AgentConstructor(
                    path=self.path,
                    function_name=function_name,
                    call=node,
                )
            )
        self.generic_visit(node)


def _contains_call(node: ast.AST | None, call_name: str) -> bool:
    if node is None:
        return False
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
            if child.func.id == call_name:
                return True
    return False


def _has_named_settings_path(constructor: AgentConstructor) -> bool:
    keywords = {
        keyword.arg: keyword.value
        for keyword in constructor.call.keywords
        if keyword.arg is not None
    }
    model_settings = keywords.get("model_settings")
    if model_settings is None:
        return False
    allowed_calls = {
        "_planner_base_model_settings",
        "model_settings_for_profile",
    }
    return any(_contains_call(model_settings, call_name) for call_name in allowed_calls)
