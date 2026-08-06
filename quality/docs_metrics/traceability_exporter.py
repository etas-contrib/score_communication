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
"""Export test-aware needs and traceability metrics from the module docs."""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse


REQUIREMENT_TYPES = ("comp_req", "feat_req")


def _needs_file(path: Path) -> Path:
    return path / "needs.json" if path.is_dir() else path


def load_needs(path: Path) -> dict[str, object]:
    return json.loads(_needs_file(path).read_text(encoding="utf-8"))


def _all_needs(document: dict[str, object]) -> dict[str, dict[str, object]]:
    needs: dict[str, dict[str, object]] = {}
    versions = document.get("versions", {})
    if not isinstance(versions, dict):
        return needs
    for version in versions.values():
        if isinstance(version, dict) and isinstance(version.get("needs"), dict):
            needs.update(version["needs"])
    return needs


def _test_name(testcase: ET.Element) -> str:
    name = testcase.get("name", "")
    classname = testcase.get("classname", "")
    return "__".join((classname.split(".")[-1], name)) if classname else name


def _property_values(testcase: ET.Element) -> dict[str, str]:
    properties = testcase.find("properties")
    if properties is None:
        return {}
    return {
        prop.get("name", ""): prop.get("value", "")
        for prop in properties.findall("property")
    }


def _verification_references(properties: dict[str, str]) -> list[str]:
    values = (
        properties.get("PartiallyVerifies", ""),
        properties.get("FullyVerifies", ""),
    )
    return sorted(
        {
            reference.strip()
            for value in values
            for reference in value.split(",")
            if reference.strip()
        }
    )


def _is_linkable(testcase: ET.Element, properties: dict[str, str]) -> bool:
    return bool(
        _verification_references(properties)
        and testcase.get("file") is not None
        and testcase.get("line") is not None
    )


def _repository_url(workspace: Path) -> str:
    try:
        raw = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "https://github.com/eclipse-score/communication"
    if raw.startswith("git@github.com:"):
        raw = "https://github.com/" + raw.removeprefix("git@github.com:")
    parsed = urlparse(raw)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return raw.removesuffix(".git")
    return "https://github.com/eclipse-score/communication"


def _revision(workspace: Path) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _test_link(
    testcase: ET.Element,
    repository_url: str,
    revision: str,
) -> str:
    file = testcase.get("file", "")
    line = testcase.get("line", "1")
    name = _test_name(testcase)
    return f"{repository_url}/blob/{revision}/{file}#L{line}<>{name}"


def scan_test_results(
    workspace: Path,
    requirement_ids: set[str],
) -> tuple[dict[str, list[str]], dict[str, object]]:
    test_root = workspace / "tests-report"
    if not test_root.is_dir():
        test_root = workspace / "bazel-testlogs"
    xml_files = sorted(test_root.rglob("test.xml")) if test_root.is_dir() else []

    links: dict[str, list[str]] = defaultdict(list)
    broken: list[dict[str, str]] = []
    total = 0
    linked = 0
    repository_url = _repository_url(workspace)
    revision = _revision(workspace)

    for xml_file in xml_files:
        try:
            root = ET.parse(xml_file).getroot()
        except ET.ParseError:
            continue
        suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
        for suite in suites:
            for testcase in suite.findall("testcase"):
                total += 1
                properties = _property_values(testcase)
                if not _is_linkable(testcase, properties):
                    continue
                linked += 1
                testcase_id = _test_name(testcase)
                link = _test_link(testcase, repository_url, revision)
                for reference in _verification_references(properties):
                    if reference not in requirement_ids:
                        broken.append(
                            {"testcase": testcase_id, "missing_need": reference}
                        )
                        continue
                    links[reference].append(link)

    return links, {
        "total": total,
        "linked_to_requirements": linked,
        "linked_to_requirements_pct": _percentage(linked, total),
        "broken_references": sorted(
            broken, key=lambda item: (item["testcase"], item["missing_need"])
        ),
    }


def _percentage(numerator: int, denominator: int) -> float:
    return 100.0 if denominator == 0 else numerator / denominator * 100.0


def _has_value(value: object) -> bool:
    return bool(value.strip()) if isinstance(value, str) else bool(value)


def build_outputs(
    base_needs: dict[str, object],
    workspace: Path | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    needs_export = copy.deepcopy(base_needs)
    needs = _all_needs(needs_export)
    requirement_ids = {
        need_id
        for need_id, need in needs.items()
        if need.get("type") in REQUIREMENT_TYPES
    }

    if workspace is None:
        test_links: dict[str, list[str]] = {}
        test_metrics: dict[str, object] = {
            "total": 0,
            "linked_to_requirements": 0,
            "linked_to_requirements_pct": 100.0,
            "broken_references": [],
        }
    else:
        test_links, test_metrics = scan_test_results(workspace, requirement_ids)

    for requirement_id in requirement_ids:
        links = sorted(set(test_links.get(requirement_id, [])))
        needs[requirement_id]["testlink"] = ", ".join(links)

    metrics_by_type: dict[str, dict[str, int | float]] = {}
    overall = {
        "total": 0,
        "with_code_link": 0,
        "with_test_link": 0,
        "fully_linked": 0,
    }
    for requirement_type in REQUIREMENT_TYPES:
        typed = [
            need for need in needs.values() if need.get("type") == requirement_type
        ]
        if not typed:
            continue
        values: dict[str, int | float] = {
            "total": len(typed),
            "with_code_link": sum(
                _has_value(need.get("source_code_link")) for need in typed
            ),
            "with_test_link": sum(
                _has_value(need.get("testlink")) for need in typed
            ),
            "fully_linked": sum(
                _has_value(need.get("source_code_link"))
                and _has_value(need.get("testlink"))
                for need in typed
            ),
        }
        values["with_code_link_pct"] = _percentage(
            int(values["with_code_link"]), int(values["total"])
        )
        values["with_test_link_pct"] = _percentage(
            int(values["with_test_link"]), int(values["total"])
        )
        values["fully_linked_pct"] = _percentage(
            int(values["fully_linked"]), int(values["total"])
        )
        metrics_by_type[requirement_type] = values
        for key in overall:
            overall[key] += int(values[key])

    overall_metrics: dict[str, int | float] = dict(overall)
    overall_metrics["with_code_link_pct"] = _percentage(
        overall["with_code_link"], overall["total"]
    )
    overall_metrics["with_test_link_pct"] = _percentage(
        overall["with_test_link"], overall["total"]
    )
    overall_metrics["fully_linked_pct"] = _percentage(
        overall["fully_linked"], overall["total"]
    )
    metrics = {
        "schema_version": "2",
        "generated_by": "communication_traceability_exporter",
        "overall_metrics": overall_metrics,
        "metrics_by_type": metrics_by_type,
        "tests": test_metrics,
    }
    return needs_export, metrics


def _write_json(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-needs", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--needs-output", type=Path)
    parser.add_argument("--metrics-output", type=Path)
    parser.add_argument("--scan-tests", action="store_true")
    args = parser.parse_args()

    workspace = Path(os.environ.get("BUILD_WORKSPACE_DIRECTORY", os.getcwd()))
    base_needs = load_needs(args.base_needs)
    needs_export, metrics = build_outputs(
        base_needs, workspace if args.scan_tests else None
    )

    needs_output = args.needs_output
    metrics_output = args.metrics_output
    if args.output_dir:
        output_dir = workspace / args.output_dir
        needs_output = needs_output or output_dir / "needs.json"
        metrics_output = metrics_output or output_dir / "metrics.json"
    if needs_output:
        _write_json(needs_output, needs_export)
        print(f"Needs export: {needs_output}")
    if metrics_output:
        _write_json(metrics_output, metrics)
        print(f"Metrics export: {metrics_output}")
    if not needs_output and not metrics_output:
        parser.error("provide --output-dir, --needs-output, or --metrics-output")


if __name__ == "__main__":
    main()
