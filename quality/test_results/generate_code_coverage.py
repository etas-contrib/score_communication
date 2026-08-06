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
"""Generate stable C++ coverage evidence for all non-manual tests."""

import argparse
import shutil
import subprocess
import zipfile
from pathlib import Path

from quality.test_results.result_artifacts import (
    platform_variant,
    query_non_manual_tests,
    workspace_root,
    write_manifest,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collect-only", action="store_true")
    parser.add_argument("--target", action="append", default=[])
    parser.add_argument("--bazel-arg", action="append", default=[])
    parser.add_argument("--variant")
    parser.add_argument("--output-dir", default="_quality/artifacts")
    parser.add_argument("--archive-name")
    args = parser.parse_args()

    workspace = workspace_root()
    variant = args.variant or platform_variant()
    targets = sorted(set(args.target)) or query_non_manual_tests(workspace)
    if not targets:
        raise RuntimeError("no non-manual tests were found")

    if not args.collect_only:
        subprocess.run(
            [
                "bazel",
                "coverage",
                "--build_tests_only",
                "--skip_incompatible_explicit_targets",
                "--test_output=errors",
                *args.bazel_arg,
                *targets,
            ],
            cwd=workspace,
            check=True,
        )

    raw_report = workspace / "bazel-out/_coverage/_coverage_report.dat"
    if not raw_report.is_file():
        raise RuntimeError(f"coverage report not found: {raw_report}")

    output_dir = workspace / Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    coverage_dir = output_dir / "coverage" / "cpp" / variant
    html_dir = coverage_dir / "html"
    legacy_html_dir = workspace / "cpp_coverage_linux"
    archive_base = (
        workspace / args.archive_name
        if args.archive_name
        else output_dir / f"coverage.cpp.{variant}"
    )
    archive_base.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "bazel",
            "run",
            "//quality/coverage:generate_coverage_html",
            "--",
            "--archive",
            str(archive_base.relative_to(workspace)),
            str(legacy_html_dir.relative_to(workspace)),
        ],
        cwd=workspace,
        check=True,
    )

    if html_dir.is_dir():
        shutil.rmtree(html_dir)
    shutil.copytree(legacy_html_dir, html_dir)

    stable_archive = output_dir / f"coverage.cpp.{variant}.zip"
    generated_archive = Path(f"{archive_base}.zip")
    if generated_archive != stable_archive:
        shutil.copy2(generated_archive, stable_archive)
    coverage_dir.mkdir(parents=True, exist_ok=True)
    stable_raw_report = coverage_dir / "_coverage_report.dat"
    shutil.copy2(raw_report, stable_raw_report)

    manifest = output_dir / f"coverage.cpp.{variant}.manifest.json"
    write_manifest(
        manifest,
        {
            "artifact_type": "code-coverage",
            "language": "cpp",
            "variant": variant,
            "selected_targets": len(targets),
            "coverage_scope": "all non-manual tests",
            "raw_report": str(stable_raw_report.relative_to(workspace)),
            "raw_report_format": "zip" if zipfile.is_zipfile(raw_report) else "lcov",
            "html_index": str((html_dir / "index.html").relative_to(workspace)),
            "archive": str(stable_archive.relative_to(workspace)),
        },
    )
    print(f"Coverage HTML: {html_dir / 'index.html'}")
    print(f"Coverage archive: {stable_archive}")
    print(f"Coverage manifest: {manifest}")


if __name__ == "__main__":
    main()
