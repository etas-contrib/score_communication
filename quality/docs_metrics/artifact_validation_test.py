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

import argparse
import json
import re
import unittest
from collections import Counter
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--generated", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--needs", type=Path, required=True)
    return parser.parse_known_args()[0]


ARGS = parse_args()

EXPECTED_TYPE_COUNTS = {"comp_req": 346, "feat_req": 49}
EXPECTED_DERIVED_FROM_COUNT = 346


class ArtifactValidationTest(unittest.TestCase):
    def test_needs_and_metrics_cover_projection(self):
        projection = ARGS.generated.read_text(encoding="utf-8")
        projected_ids = set(re.findall(r"^   :id: (\S+)$", projection, re.MULTILINE))
        self.assertGreater(len(projected_ids), 0)

        needs_path = ARGS.needs / "needs.json" if ARGS.needs.is_dir() else ARGS.needs
        needs_export = json.loads(needs_path.read_text(encoding="utf-8"))
        needs = {}
        for version in needs_export.get("versions", {}).values():
            needs.update(version.get("needs", {}))

        self.assertEqual(set(needs), projected_ids)
        type_counts = Counter(need["type"] for need in needs.values())
        self.assertEqual(dict(type_counts), EXPECTED_TYPE_COUNTS)
        self.assertEqual(
            sum(bool(need.get("derived_from")) for need in needs.values()),
            EXPECTED_DERIVED_FROM_COUNT,
        )

        metrics = json.loads(ARGS.metrics.read_text(encoding="utf-8"))
        self.assertEqual(metrics["schema_version"], "2")
        self.assertEqual(metrics["overall_metrics"]["total"], len(projected_ids))
        self.assertEqual(
            {name: values["total"] for name, values in metrics["metrics_by_type"].items()},
            dict(type_counts),
        )


if __name__ == "__main__":
    unittest.main(argv=[__file__])
