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

from tools import trlc_to_needs_genrule as projection


MODEL = """
package ScoreReq

enum Asil { QM B D }
enum Status { valid invalid }

type FeatReq {
  description String
  version Integer
  status Status
  safety Asil
}

type CompReq {
  description String
  version Integer
  status Status
  safety Asil
}
"""


class TrlcProjectionTest(unittest.TestCase):
    def _load(self, requirements):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model.rsl"
            records = root / "requirements.trlc"
            model.write_text(MODEL, encoding="utf-8")
            records.write_text(requirements, encoding="utf-8")
            return projection.load_requirements([model, records])

    def test_uses_validated_trlc_values(self):
        requirements = self._load(
            """
            package Example
            import ScoreReq

            ScoreReq.FeatReq ExampleRequirement {
              description = "A validated requirement"
              version = 2
              status = ScoreReq.Status.valid
              safety = ScoreReq.Asil.B
            }
            """
        )

        self.assertEqual(len(requirements), 1)
        self.assertEqual(requirements[0]["id"], "feat_req__ExampleRequirement")
        self.assertEqual(requirements[0]["safety"], "ASIL_B")
        self.assertEqual(requirements[0]["version"], "2")
        self.assertEqual(requirements[0]["status"], "valid")

    def test_rejects_invalid_trlc(self):
        with self.assertRaisesRegex(projection.ProjectionError, "validation failed"):
            self._load(
                """
                package Example
                import ScoreReq

                ScoreReq.CompReq MissingSafety {
                  description = "Invalid because safety is mandatory"
                  version = 1
                  status = ScoreReq.Status.valid
                }
                """
            )

    def test_renders_native_need_and_links(self):
        rendered = projection.render_requirement(
            {
                "type": "comp_req",
                "name": "Child",
                "id": "comp_req__Child",
                "status": "valid",
                "safety": "QM",
                "version": "1",
                "derived_from": ["feat_req__Parent"],
                "description": "A child requirement",
            }
        )

        self.assertIn(".. comp_req:: Child", rendered)
        self.assertIn(":id: comp_req__Child", rendered)
        self.assertIn(":derived_from: feat_req__Parent", rendered)

    def test_renders_trlc_double_bracket_references_as_rst_literals(self):
        rendered = projection.render_requirement(
            {
                "type": "comp_req",
                "name": "Child",
                "id": "comp_req__Child",
                "status": "valid",
                "safety": "QM",
                "version": "1",
                "derived_from": [],
                "description": "See [[ReferencedRequirement]].",
            }
        )

        self.assertIn("See ``[[ReferencedRequirement]]``.", rendered)


if __name__ == "__main__":
    unittest.main()
