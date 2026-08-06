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

load("@rules_python//python:defs.bzl", "py_binary", "py_library", "py_test")

py_library(
    name = "result_artifacts",
    srcs = ["result_artifacts.py"],
)

py_binary(
    name = "unit_test_results",
    srcs = ["generate_test_results.py"],
    args = [
        "--kind=unit",
        "--tag=unit",
    ],
    main = "generate_test_results.py",
    visibility = ["//visibility:public"],
    deps = [":result_artifacts"],
)

py_binary(
    name = "component_test_results",
    srcs = ["generate_test_results.py"],
    args = [
        "--kind=component",
        "--tag=component",
    ],
    main = "generate_test_results.py",
    visibility = ["//visibility:public"],
    deps = [":result_artifacts"],
)

py_binary(
    name = "code_coverage",
    srcs = ["generate_code_coverage.py"],
    main = "generate_code_coverage.py",
    visibility = ["//visibility:public"],
    deps = [":result_artifacts"],
)

py_test(
    name = "result_artifacts_test",
    srcs = ["result_artifacts_test.py"],
    deps = [":result_artifacts"],
)
