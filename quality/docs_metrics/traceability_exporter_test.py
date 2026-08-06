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
from pathlib import Path
from unittest.mock import patch

from quality.docs_metrics.traceability_exporter import build_outputs


def base_needs():
    return {
        "versions": {
            "": {
                "needs": {
                    "feat_req__Feature": {
                        "id": "feat_req__Feature",
                        "type": "feat_req",
                        "source_code_link": "",
                    },
                    "comp_req__Component": {
                        "id": "comp_req__Component",
                        "type": "comp_req",
                        "source_code_link": "",
                    },
                }
            }
        }
    }


class TraceabilityExporterTest(unittest.TestCase):
    def test_builds_test_links_and_metrics_from_bazel_xml(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            xml = workspace / "bazel-testlogs/pkg/example_test/test.xml"
            xml.parent.mkdir(parents=True)
            xml.write_text(
                """<testsuites><testsuite><testcase name="VerifiesComponent"
                classname="Suite" file="pkg/example_test.cpp" line="42">
                <properties>
                  <property name="FullyVerifies" value="comp_req__Component"/>
                </properties></testcase></testsuite></testsuites>""",
                encoding="utf-8",
            )

            with patch(
                "quality.docs_metrics.traceability_exporter._repository_url",
                return_value="https://github.com/eclipse-score/communication",
            ), patch(
                "quality.docs_metrics.traceability_exporter._revision",
                return_value="abc123",
            ):
                needs, metrics = build_outputs(base_needs(), workspace)

            exported = needs["versions"][""]["needs"]
            self.assertIn("pkg/example_test.cpp#L42", exported["comp_req__Component"]["testlink"])
            self.assertEqual(metrics["overall_metrics"]["total"], 2)
            self.assertEqual(metrics["overall_metrics"]["with_test_link"], 1)
            self.assertEqual(metrics["tests"]["total"], 1)
            self.assertEqual(metrics["tests"]["linked_to_requirements"], 1)
            self.assertEqual(metrics["tests"]["broken_references"], [])

    def test_reports_broken_references(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            xml = workspace / "bazel-testlogs/pkg/example_test/test.xml"
            xml.parent.mkdir(parents=True)
            xml.write_text(
                """<testsuites><testsuite><testcase name="Broken"
                classname="Suite" file="pkg/example_test.cpp" line="7">
                <properties>
                  <property name="PartiallyVerifies" value="comp_req__Missing"/>
                </properties></testcase></testsuite></testsuites>""",
                encoding="utf-8",
            )

            with patch(
                "quality.docs_metrics.traceability_exporter._repository_url",
                return_value="https://github.com/eclipse-score/communication",
            ), patch(
                "quality.docs_metrics.traceability_exporter._revision",
                return_value="abc123",
            ):
                _, metrics = build_outputs(base_needs(), workspace)

            self.assertEqual(
                metrics["tests"]["broken_references"],
                [
                    {
                        "testcase": "Suite__Broken",
                        "missing_need": "comp_req__Missing",
                    }
                ],
            )


if __name__ == "__main__":
    unittest.main()
