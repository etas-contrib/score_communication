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

load("@rules_python//python:defs.bzl", "py_binary", "py_test")

py_binary(
    name = "traceability_exporter",
    srcs = ["traceability_exporter.py"],
    visibility = ["//visibility:private"],
)

py_binary(
    name = "docs_check",
    testonly = True,
    srcs = ["traceability_exporter.py"],
    args = [
        "--base-needs=$(location //docs/sphinx:sphinx_doc_needs)",
        "--output-dir=_build",
        "--scan-tests",
    ],
    data = ["//docs/sphinx:sphinx_doc_needs"],
    main = "traceability_exporter.py",
    visibility = ["//visibility:public"],
)

genrule(
    name = "metrics_json",
    testonly = True,
    srcs = ["//docs/sphinx:sphinx_doc_needs"],
    outs = ["metrics.json"],
    cmd = " ".join([
        "$(location :traceability_exporter)",
        "--base-needs=$(location //docs/sphinx:sphinx_doc_needs)",
        "--metrics-output=$@",
    ]),
    tools = [":traceability_exporter"],
    visibility = ["//visibility:public"],
)

py_test(
    name = "trlc_projection_test",
    srcs = ["trlc_projection_test.py"],
    deps = ["//tools:trlc_projection"],
)

py_test(
    name = "artifact_validation_test",
    srcs = ["artifact_validation_test.py"],
    args = [
        "--generated=$(location //:trlc_requirements_rst)",
        "--metrics=$(location //:metrics_json)",
        "--needs=$(location //:needs_json)",
    ],
    data = [
        "//:metrics_json",
        "//:needs_json",
        "//:trlc_requirements_rst",
    ],
)

py_test(
    name = "traceability_exporter_test",
    srcs = [
        "traceability_exporter.py",
        "traceability_exporter_test.py",
    ],
    main = "traceability_exporter_test.py",
)
