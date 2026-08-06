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
"""Run a classified test set and assemble one stable JUnit result artifact."""

import argparse
import subprocess
from pathlib import Path

from quality.test_results.result_artifacts import (
    merge_junit,
    platform_variant,
    query_tagged_tests,
    workspace_root,
    write_manifest,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=("unit", "component"), required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--collect-only", action="store_true")
    parser.add_argument("--target", action="append", default=[])
    parser.add_argument("--bazel-arg", action="append", default=[])
    parser.add_argument("--variant")
    parser.add_argument("--output-dir", default="_quality/artifacts")
    args = parser.parse_args()

    workspace = workspace_root()
    variant = args.variant or platform_variant()
    targets = sorted(set(args.target)) or query_tagged_tests(workspace, args.tag)
    if not targets:
        raise RuntimeError(f"no non-manual tests carry the '{args.tag}' tag")

    test_exit_code = 0
    if not args.collect_only:
        completed = subprocess.run(
            [
                "bazel",
                "test",
                "--build_tests_only",
                "--skip_incompatible_explicit_targets",
                "--test_output=errors",
                *args.bazel_arg,
                *targets,
            ],
            cwd=workspace,
            check=False,
        )
        test_exit_code = completed.returncode

    output_dir = workspace / Path(args.output_dir)
    junit = output_dir / f"tests.{args.kind}.{variant}.xml"
    counts = merge_junit(workspace, args.kind, variant, targets, junit)
    manifest = output_dir / f"tests.{args.kind}.{variant}.manifest.json"
    write_manifest(
        manifest,
        {
            "artifact_type": "test-results",
            "classification": args.kind,
            "classification_tag": args.tag,
            "variant": variant,
            "test_command_exit_code": test_exit_code,
            "junit_xml": str(junit.relative_to(workspace)),
            **counts,
        },
    )
    print(f"{args.kind.title()} test results: {junit}")
    print(f"{args.kind.title()} test manifest: {manifest}")
    if test_exit_code:
        raise SystemExit(test_exit_code)


if __name__ == "__main__":
    main()
