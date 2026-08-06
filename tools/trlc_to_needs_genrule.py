#!/usr/bin/env python3
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
"""Project validated TRLC requirements to native Sphinx-Needs directives."""

import argparse
import re
import sys
from pathlib import Path

from trlc.errors import Message_Handler
from trlc.trlc import Source_Manager


REQUIREMENT_TYPES = {
    "FeatReq": "feat_req",
    "CompReq": "comp_req",
}

SAFETY_VALUES = {
    "QM": "QM",
    "B": "ASIL_B",
    "D": "ASIL_D",
}


class ProjectionError(RuntimeError):
    """Raised when validated TRLC data cannot be projected without loss."""


def _parse_trlc(input_files):
    """Parse RSL and TRLC inputs with the official TRLC implementation."""
    message_handler = Message_Handler()
    source_manager = Source_Manager(message_handler, lint_mode=False)

    for input_file in sorted({str(Path(path)) for path in input_files}):
        source_manager.register_file(input_file)

    symbols = source_manager.process()
    if symbols is None:
        raise ProjectionError("TRLC validation failed; see diagnostics above")
    return symbols


def _need_id(record_type, record_name):
    return f"{REQUIREMENT_TYPES[record_type]}__{record_name}"


def _project_record(record):
    """Convert a validated TRLC record object to projection data."""
    values = record.to_python_dict()
    missing = [
        field
        for field in ("description", "version", "status", "safety")
        if values.get(field) is None
    ]
    if missing:
        raise ProjectionError(
            f"{record.fully_qualified_name()} is missing required field(s): "
            + ", ".join(missing)
        )

    try:
        safety = SAFETY_VALUES[values["safety"]]
    except KeyError as error:
        raise ProjectionError(
            f"{record.fully_qualified_name()} has unsupported safety value "
            f"{values['safety']!r}"
        ) from error

    return {
        "fqn": record.fully_qualified_name(),
        "name": record.name,
        "record_type": record.n_typ.name,
        "type": REQUIREMENT_TYPES[record.n_typ.name],
        "id": _need_id(record.n_typ.name, record.name),
        "description": values["description"],
        "version": str(values["version"]),
        "status": values["status"],
        "safety": safety,
        "derived_from": values.get("derived_from") or [],
    }


def load_requirements(input_files):
    """Return all feature/component requirements from validated TRLC inputs."""
    symbols = _parse_trlc(input_files)
    requirements = [
        _project_record(record)
        for record in symbols.iter_record_objects()
        if record.n_typ.name in REQUIREMENT_TYPES
    ]

    by_fqn = {requirement["fqn"]: requirement for requirement in requirements}
    by_id = {}
    for requirement in requirements:
        if requirement["id"] in by_id:
            raise ProjectionError(
                f"duplicate generated need ID {requirement['id']!r}: "
                f"{by_id[requirement['id']]['fqn']} and {requirement['fqn']}"
            )
        by_id[requirement["id"]] = requirement

    for requirement in requirements:
        links = []
        for reference in requirement.pop("derived_from"):
            target_fqn = reference["item"]
            target = by_fqn.get(target_fqn)
            if target is not None:
                links.append(target["id"])
        requirement["derived_from"] = sorted(set(links))

    return sorted(requirements, key=lambda requirement: requirement["id"])


def _format_content(text):
    """Make TRLC markup safe as a single RST directive paragraph."""
    content = re.sub(r"\s+", " ", text).replace("\\", "\\\\").strip()
    # TRLC descriptions use [[Name]] for prose references. Sphinx-Needs treats
    # that spelling as its deprecated dynamic-function syntax unless it lives
    # in a literal node, so preserve the source text as inline RST literals.
    return re.sub(r"\[\[([^\]\n]+)\]\]", r"``[[\1]]``", content)


def render_requirement(requirement):
    """Render one projected requirement as a native Sphinx-Needs directive."""
    lines = [
        f".. {requirement['type']}:: {requirement['name']}",
        f"   :id: {requirement['id']}",
        f"   :status: {requirement['status']}",
        f"   :safety: {requirement['safety']}",
        f"   :version: {requirement['version']}",
    ]
    if requirement["derived_from"]:
        lines.append(f"   :derived_from: {', '.join(requirement['derived_from'])}")
    lines.extend(["", f"   {_format_content(requirement['description'])}", ""])
    return "\n".join(lines)


def render_document(requirements):
    """Render a deterministic native-needs RST document."""
    lines = [
        ".. Auto-generated from validated TRLC requirements.",
        ".. Generated by tools/trlc_to_needs_genrule.py; do not edit.",
        f".. Total requirements: {len(requirements)}",
        "",
        "Communication Requirements",
        "==========================",
        "",
    ]
    lines.extend(render_requirement(requirement) for requirement in requirements)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Project validated TRLC requirements to native Sphinx-Needs RST"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--input-files", nargs="+", required=True)
    args = parser.parse_args()

    requirements = load_requirements(args.input_files)
    if not requirements:
        raise ProjectionError("TRLC inputs did not contain any feature/component requirements")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_document(requirements), encoding="utf-8")
    print(
        f"Generated {len(requirements)} validated requirements in {args.output}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
