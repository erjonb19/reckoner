"""Assemble the Log Analytics workbook from the queries in ``deploy/workbook/``.

The four ``.kql`` files are the source; ``reckoner.workbook.json`` is built from
them, so a query exists in exactly one place and each can be run on its own
with ``az monitor log-analytics query``. A test fails if the JSON drifts from
the files.

The workbook names no subscription or workspace. It asks for the workspace
when opened, which keeps resource IDs out of the repository and lets the same
file open against any workspace the job logs to.

    python scripts/build_workbook.py
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parents[1] / "deploy" / "workbook"
OUT = HERE / "reckoner.workbook.json"

#: (file stem, title, what a reader should look for). In reading order.
SECTIONS: tuple[tuple[str, str, str], ...] = (
    (
        "q_runs",
        "Run history",
        "One row per attempt. **killed (no job_end)** is the out-of-memory signature: "
        "the container is stopped before Python can log anything. An execution's "
        "status in the portal can read *Running* while its attempts here have "
        "already failed, because replicas retry.",
    ),
    (
        "q_stage_duration",
        "Duration per stage",
        "Completed stages only. A killed stage has no `stage_end` and appears in the "
        "run history instead, not here.",
    ),
    (
        "q_memory",
        "Peak memory against the ceiling",
        "The process peak never falls, so it cannot say where memory went. The worst "
        "per-slice window names the slice.",
    ),
    (
        "q_manifest",
        "Manifest match per layer",
        "Stage 1's verdict per layer per run. A layer present in one run and absent "
        "from the next means the run checked fewer layers.",
    ),
)

INTRO = (
    "## Reckoner pipeline\n\n"
    "Built from the job's structured logs (`ContainerAppConsoleLogs_CL`, one JSON "
    "event per line). Every query here is also a file in `deploy/workbook/` and "
    "can be run on its own. Log Analytics keeps 31 days."
)


def build() -> dict[str, object]:
    items: list[dict[str, object]] = [
        {"type": 1, "content": {"json": INTRO}, "name": "intro"},
        {
            "type": 9,
            "content": {
                "version": "KqlParameterItem/1.0",
                "parameters": [
                    {
                        "id": "workspace",
                        "version": "KqlParameterItem/1.0",
                        "name": "Workspace",
                        "label": "Log Analytics workspace",
                        "type": 5,
                        "isRequired": True,
                        "typeSettings": {
                            "resourceTypeFilter": {
                                "microsoft.operationalinsights/workspaces": True
                            },
                            "additionalResourceOptions": [],
                        },
                    },
                    {
                        "id": "timerange",
                        "version": "KqlParameterItem/1.0",
                        "name": "TimeRange",
                        "label": "Time range",
                        "type": 4,
                        "isRequired": True,
                        "value": {"durationMs": 2592000000},
                        "typeSettings": {
                            "selectableValues": [
                                {"durationMs": 86400000},
                                {"durationMs": 604800000},
                                {"durationMs": 2592000000},
                            ]
                        },
                    },
                ],
            },
            "name": "parameters",
        },
    ]
    for stem, title, note in SECTIONS:
        query = (HERE / f"{stem}.kql").read_text(encoding="utf-8").strip()
        items.append(
            {"type": 1, "content": {"json": f"### {title}\n\n{note}"}, "name": f"{stem}-note"}
        )
        items.append(
            {
                "type": 3,
                "content": {
                    "version": "KqlItem/1.0",
                    "query": query,
                    "size": 0,
                    "title": title,
                    "timeContextFromParameter": "TimeRange",
                    "queryType": 0,
                    "resourceType": "microsoft.operationalinsights/workspaces",
                    "crossComponentResources": ["{Workspace}"],
                    "visualization": "table",
                },
                "name": stem,
            }
        )
    return {
        "version": "Notebook/1.0",
        "items": items,
        "fallbackResourceIds": [],
        "$schema": "https://github.com/Microsoft/Application-Insights-Workbooks/blob/master/schema/workbook.json",
    }


def main() -> None:
    OUT.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
