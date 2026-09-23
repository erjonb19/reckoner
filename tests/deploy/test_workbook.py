"""The Log Analytics workbook, held to the code that feeds it.

A workbook fails silently. Rename an event in the job and the panel that
queries it shows "no results", which reads exactly like a quiet month. So the
event names the queries filter on are checked against the events the code
actually emits, by reading the source rather than a list someone keeps.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WORKBOOK = REPO / "deploy" / "workbook"


def emitted_events() -> set[str]:
    """Every event name the job can log: ``log("x", ...)`` and ``{"event": "x"}``."""
    names: set[str] = set()
    for path in (REPO / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                first = node.args[0] if node.args else None
                if (
                    called == "log"
                    and isinstance(first, ast.Constant)
                    and isinstance(first.value, str)
                ):
                    names.add(first.value)
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "event"
                        and isinstance(value, ast.Constant)
                        and isinstance(value.value, str)
                    ):
                        names.add(value.value)
    return names


def queried_events() -> set[str]:
    names: set[str] = set()
    for path in WORKBOOK.glob("*.kql"):
        text = path.read_text(encoding="utf-8")
        names |= set(re.findall(r'(?:event\)|ev)\s*==\s*"([a-z_]+)"', text))
        for group in re.findall(r"ev in \(([^)]*)\)", text):
            names |= set(re.findall(r'"([a-z_]+)"', group))
    return names


def build() -> dict[str, object]:
    spec = importlib.util.spec_from_file_location(
        "build_workbook", REPO / "scripts" / "build_workbook.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build()  # type: ignore[no-any-return]


class TestTheQueriesMatchTheCode:
    def test_it_finds_the_events_it_depends_on(self):
        """Guard on the guard: an empty set would make the next test vacuous."""
        assert {"job_start", "job_end", "stage_end", "manifest_summary", "mart_shard"} <= (
            queried_events()
        )

    def test_every_queried_event_is_emitted_somewhere(self):
        missing = queried_events() - emitted_events()

        assert not missing, f"the workbook queries events nothing logs: {sorted(missing)}"


class TestTheJson:
    def test_it_is_built_from_the_kql_files_and_has_not_drifted(self):
        committed = json.loads((WORKBOOK / "reckoner.workbook.json").read_text(encoding="utf-8"))

        assert committed == build(), "run python scripts/build_workbook.py"

    def test_every_query_asks_for_the_workspace_rather_than_naming_one(self):
        """No subscription or resource ID belongs in the repository."""
        text = (WORKBOOK / "reckoner.workbook.json").read_text(encoding="utf-8")

        assert "/subscriptions/" not in text
        queries = [i for i in json.loads(text)["items"] if i["type"] == 3]
        assert len(queries) == 4
        assert all(q["content"]["crossComponentResources"] == ["{Workspace}"] for q in queries)
