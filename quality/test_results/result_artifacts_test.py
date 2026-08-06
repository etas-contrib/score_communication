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

import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from quality.test_results.result_artifacts import merge_junit, test_xml_path


class ResultArtifactsTest(unittest.TestCase):
    def test_maps_target_to_bazel_test_xml(self):
        workspace = Path("/workspace")
        self.assertEqual(
            test_xml_path(workspace, "//some/package:some_test"),
            workspace / "bazel-testlogs/some/package/some_test/test.xml",
        )

    def test_merges_junit_and_records_missing_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            source = test_xml_path(workspace, "//pkg:passing_test")
            source.parent.mkdir(parents=True)
            source.write_text(
                '<testsuite name="suite" tests="2" failures="1" errors="0" '
                'skipped="0" time="0.5"><testcase name="one"/></testsuite>',
                encoding="utf-8",
            )
            output = workspace / "results/tests.unit.linux_x86_64.xml"

            result = merge_junit(
                workspace,
                "unit",
                "linux_x86_64",
                ["//pkg:passing_test", "//pkg:missing_test"],
                output,
            )

            root = ET.parse(output).getroot()
            self.assertEqual(root.attrib["tests"], "2")
            self.assertEqual(root.attrib["failures"], "1")
            self.assertEqual(root.find("testsuite").attrib["bazel_target"], "//pkg:passing_test")
            self.assertEqual(result["targets_without_results"], ["//pkg:missing_test"])


if __name__ == "__main__":
    unittest.main()
