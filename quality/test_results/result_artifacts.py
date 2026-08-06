# *******************************************************************************
# Copyright (c) 2026 Contributors to the Eclipse Foundation
#
# See the NOTICE file(s) distributed with this work for additional
# information regarding copyright ownership.
#
# This program and the accompanying materials are made available under the
# terms of the Apache License Version 2.0 which is available at
# https://www.apache.org/licenses/LICENSE-2.0
#
# SPDX-License-Identifier: Apache-2.0
# *******************************************************************************
"""Shared helpers for repository quality-evidence entry points."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path


def workspace_root() -> Path:
    """Return the workspace used by the surrounding ``bazel run`` command."""
    try:
        return Path(os.environ["BUILD_WORKSPACE_DIRECTORY"]).resolve()
    except KeyError as error:
        raise RuntimeError("Run this target with 'bazel run'.") from error


def platform_variant() -> str:
    """Return the stable OS/architecture spelling used in artifact names."""
    system = platform.system().lower() or "unknown"
    machine = platform.machine().lower() or "unknown"
    aliases = {"amd64": "x86_64", "aarch64": "arm64"}
    return f"{system}_{aliases.get(machine, machine)}"


def query_tagged_tests(workspace: Path, tag: str) -> list[str]:
    """Query non-manual test targets carrying the requested classification."""
    expression = (
        f'attr(tags, "{tag}", tests(//...)) '
        'except attr(tags, "manual", tests(//...))'
    )
    result = subprocess.run(
        ["bazel", "query", expression, "--output=label"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    return sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})


def query_non_manual_tests(workspace: Path) -> list[str]:
    """Query every non-manual test, matching Bazel's wildcard coverage scope."""
    expression = 'tests(//...) except attr(tags, "manual", tests(//...))'
    result = subprocess.run(
        ["bazel", "query", expression, "--output=label"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    return sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})


def test_xml_path(workspace: Path, target: str) -> Path:
    """Translate a main-repository test label to its Bazel JUnit path."""
    if not target.startswith("//") or ":" not in target:
        raise ValueError(f"unsupported test target label: {target}")
    package, name = target[2:].split(":", 1)
    return workspace / "bazel-testlogs" / package / name / "test.xml"


def _integer_attribute(element: ET.Element, name: str) -> int:
    try:
        return int(element.attrib.get(name, "0"))
    except ValueError:
        return 0


def _float_attribute(element: ET.Element, name: str) -> float:
    try:
        return float(element.attrib.get(name, "0"))
    except ValueError:
        return 0.0


def merge_junit(
    workspace: Path,
    kind: str,
    variant: str,
    targets: list[str],
    output: Path,
) -> dict[str, object]:
    """Merge selected Bazel JUnit files and return manifest-ready counts."""
    merged = ET.Element("testsuites", {"name": f"communication-{kind}"})
    available: list[dict[str, str]] = []
    missing: list[str] = []
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    total_time = 0.0

    for target in targets:
        xml_path = test_xml_path(workspace, target)
        if not xml_path.is_file():
            missing.append(target)
            continue

        document = ET.parse(xml_path)
        root = document.getroot()
        suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
        for suite in suites:
            suite.set("bazel_target", target)
            merged.append(suite)
            for key in totals:
                totals[key] += _integer_attribute(suite, key)
            total_time += _float_attribute(suite, "time")
        available.append(
            {
                "target": target,
                "source": str(xml_path.relative_to(workspace)),
            }
        )

    if not available:
        raise RuntimeError(f"no JUnit XML results were found for {kind} tests")

    merged.attrib.update({key: str(value) for key, value in totals.items()})
    merged.set("time", f"{total_time:.6f}")
    merged.set("variant", variant)
    ET.indent(merged, space="  ")
    output.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(merged).write(output, encoding="utf-8", xml_declaration=True)

    return {
        "selected_targets": len(targets),
        "targets_with_results": len(available),
        "targets_without_results": missing,
        "results": available,
        **totals,
        "time": total_time,
    }


def write_manifest(path: Path, payload: dict[str, object]) -> None:
    """Write a consistently versioned JSON evidence manifest."""
    document = {
        "schema_version": "1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
