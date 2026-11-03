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
"""Report of the SOME/IP mixed load benchmark: merges the results of both sides into one table per event class, and
tabulates and fits the CPU_RESULT windows of the benchmarked side.

usage: load_report.py <target results.txt> [<peer results.txt>]
       load_report.py --compare <target results A> <peer results A> <target results B> <peer results B>
  target results: LOAD_RESULT and CPU_RESULT lines of run_target.sh --app someip_load_lola (io-sock event module) or
                  of run_target_vector_load.sh (Vector someipd_posix, safe-posix-platform someip_bench/load)
  peer results:   LOAD_RESULT lines of someip_load_peer (run_host_load_peer.sh)
  --compare:      both reports and a comparison of B against A: CPU per phase and per component, and delivery
Also used by test_load_qemu.py.
"""

import json
import pathlib
import sys

RESULT_PREFIX = "LOAD_RESULT "
CPU_PREFIX = "CPU_RESULT "
# CPU groups of CPU_RESULT per stack: (column, group). The first is the stack's SOME/IP processing, reported per event.
STACKS = {
    "iosock": {
        "name": "io-sock event module",
        "groups": (("module", "module"), ("io-sock", "io-sock"), ("sd daemon", "someip_sd_daemon"), ("app", "someip_load_lola")),
        "note": "module: event module thread in io-sock; io-sock: all other io-sock threads; busy: all CPUs minus idle",
        "components": {"stack": ("module", "someip_sd_daemon"), "io-sock": ("io-sock",), "app": ("someip_load_lola",)},
    },
    "vector": {
        "name": "Vector someipd_posix",
        "groups": (("someipd", "someipd_posix"), ("io-sock", "io-sock"), ("app", "SomeipLoadAra")),
        "note": "someipd: someipd_posix (SOME/IP and SD); io-sock: all io-sock threads; busy: all CPUs minus idle",
        "components": {"stack": ("someipd_posix",), "io-sock": ("io-sock",), "app": ("SomeipLoadAra",)},
    },
}
CPU_GROUPS = STACKS["iosock"]["groups"]
COMPONENTS = (
    ("SOME/IP stack", "stack"),
    ("io-sock (network)", "io-sock"),
    ("application", "app"),
)


def parse(output, prefix=RESULT_PREFIX):
    """The JSON objects of all lines starting with prefix."""
    return [json.loads(line[len(prefix) :]) for line in output.splitlines() if line.startswith(prefix)]


def merge(sd_results, peer_results):
    """One row per event class: the sender's counts and the receiver's statistics."""
    peer_by_class = {result["class"]: result for result in peer_results}
    rows = []
    for sd in sd_results:
        peer = peer_by_class.get(sd["class"], {})
        sender, receiver = (sd, peer) if sd["direction"] == "TX" else (peer, sd)
        rows.append({"sd": sd, "peer": peer, "sender": sender, "receiver": receiver})
    return rows


def format_results(rows):
    header = (
        f"{'dir':>3} {'frequency':>19} {'payload':>11} {'bytes':>5} {'rate_hz':>7} {'streams':>7} {'sent':>7} "
        f"{'recv':>7} {'lost':>6} {'loss%':>6} {'dup':>4} {'jit_p50':>8} {'jit_p99':>8} {'lag_max':>8}"
    )
    lines = [
        "",
        "SOME/IP mixed load benchmark, jitter and send lag in ms",
        "TX: sent by the benchmarked side, received by the peer; RX: sent by the peer, received by the benchmarked side",
        "RX sent and lost are derived from the sequence numbers received in the window (the peer sends continuously)",
        header,
        "-" * len(header),
    ]
    for row in rows:
        sd, sender, receiver = row["sd"], row["sender"], row["receiver"]
        received = receiver.get("received", 0)
        # TX counts only samples sent in the measurement window, so the loss is exact; RX loss are sequence gaps.
        if sd["direction"] == "TX":
            sent = sender.get("sent", 0)
            lost = sent - received
        else:
            lost = receiver.get("lost", 0)
            sent = received + lost
        jitter = receiver.get("jitter_us", {})
        lines.append(
            f"{sd['direction']:>3} {sd['frequency']:>19} {sd['payload_bucket']:>11} {sd['payload']:>5} "
            f"{sd['rate_hz']:>7g} {sd['streams']:>7} {sent:>7} {received:>7} {lost:>6} "
            f"{100.0 * lost / max(1, lost + received):>6.2f} {receiver.get('duplicates', 0):>4} "
            + " ".join(
                f"{value / 1000:>8.2f}" if value is not None else f"{'-':>8}"
                for value in (jitter.get("p50"), jitter.get("p99"), sender.get("max_send_lag_us"))
            )
        )
    return "\n".join(lines) + "\n"


def events_per_s(result):
    return (result["tx_events"] + result["rx_events"]) / result["window_s"]


def fit(points):
    """Least squares line y = intercept + slope * x; None with fewer than two distinct x."""
    if len({x for x, _ in points}) < 2:
        return None
    count = len(points)
    mean_x = sum(x for x, _ in points) / count
    mean_y = sum(y for _, y in points) / count
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / sum((x - mean_x) ** 2 for x, _ in points)
    return mean_y - slope * mean_x, slope


def stack_of(cpu_results):
    """The STACKS entry matching the sampled processes."""
    vector = any("someipd_posix" in result["groups"] for result in cpu_results)
    return STACKS["vector" if vector else "iosock"]


def pct(result, groups):
    return sum(result["groups"].get(group, {}).get("pct", 0.0) for group in groups)


def busy_pct(result):
    return 100.0 * result["cpus"] - result["groups"].get("idle", {}).get("pct", 0.0)


def format_cpu(cpu_results):
    stack = stack_of(cpu_results)
    groups = stack["groups"]
    first = groups[0][0]
    header = (
        f"{'phase':>12} {'ev/s':>7} "
        + " ".join(f"{name + ' %':>12}" for name, _ in groups)
        + f" {'total %':>8} {'busy %':>7} {first + ' us/ev':>13}"
    )
    lines = [
        "",
        f"CPU time per thread group from QNX procfs, % of one CPU ({stack['name']})",
        stack["note"] + "; total: sum of the columns before",
        header,
        "-" * len(header),
    ]
    for result in cpu_results:
        rate = events_per_s(result)
        first_us = (
            result["groups"].get(groups[0][1], {}).get("cpu_ms", 0.0)
            * 1000.0
            / max(1, result["tx_events"] + result["rx_events"])
        )
        lines.append(
            f"{result['phase']:>12} {rate:>7.0f} "
            + " ".join(f"{pct(result, (group,)):>12.2f}" for _, group in groups)
            + f" {pct(result, [group for _, group in groups]):>8.2f} {busy_pct(result):>7.1f} "
            + (f"{first_us:>13.2f}" if rate > 0 else f"{'-':>13}")
        )
    for name, group in groups[:2]:
        # CPU ns per second over events per second: slope in ns (= us/1000) per event, intercept in % of one CPU.
        line = fit(
            [(events_per_s(result), result["groups"].get(group, {}).get("pct", 0.0) * 1.0e7) for result in cpu_results]
        )
        if line is not None:
            lines.append(f"{name}: {line[0] / 1.0e7:.2f} % fixed + {line[1] / 1000.0:.2f} us CPU per event")
    if cpu_results:
        busy_threads = [
            f"{thread['tid']} '{thread['name']}' {thread['pct']:.2f}"
            for thread in cpu_results[-1]["threads"]
            if thread["pct"] >= 0.05
        ]
        lines.append("io-sock threads >= 0.05 % in the load window (tid 'name' %): " + ", ".join(busy_threads))
    return "\n".join(lines) + "\n"


def delivery(rows):
    """Lost and duplicate events and the worst per-class receive jitter p99 in ms, of all classes."""
    lost = duplicates = received = 0
    jitter_p99 = 0.0
    for row in rows:
        receiver = row["receiver"]
        if row["sd"]["direction"] == "TX":
            row_lost = row["sender"].get("sent", 0) - receiver.get("received", 0)
        else:
            row_lost = receiver.get("lost", 0)
        lost += row_lost
        received += receiver.get("received", 0)
        duplicates += receiver.get("duplicates", 0)
        jitter_p99 = max(jitter_p99, (receiver.get("jitter_us", {}).get("p99") or 0.0) / 1000.0)
    return {"received": received, "lost": lost, "duplicates": duplicates, "jitter_p99": jitter_p99}


def _change(a, b):
    """B - A and B relative to A in %."""
    delta = b - a
    relative = f"{100.0 * delta / a:>+9.1f}" if a > 0 else f"{'-':>9}"
    return f"{delta:>+9.2f} {relative}"


def format_comparison(cpu_a, cpu_b, rows_a, rows_b):
    stack_a, stack_b = stack_of(cpu_a), stack_of(cpu_b)
    name_a, name_b = stack_a["name"], stack_b["name"]
    header = f"{'':>24} {'A':>9} {'B':>9} {'B - A':>9} {'B vs A %':>9}"
    lines = ["", f"Comparison, A: {name_a}, B: {name_b}"]
    for component, key in COMPONENTS:
        lines.append(
            f"  {component}: A " + " + ".join(stack_a["components"][key]) + ", B " + " + ".join(stack_b["components"][key])
        )
    lines += ["", "Total CPU (stack + io-sock + application), % of one CPU, per phase", header, "-" * len(header)]
    by_phase_b = {result["phase"]: result for result in cpu_b}

    def total(stack, result):
        return pct(result, [group for key in stack["components"] for group in stack["components"][key]])

    for result_a in cpu_a:
        result_b = by_phase_b.get(result_a["phase"])
        if result_b is None:
            continue
        label = f"{result_a['phase']} ({events_per_s(result_a):.0f} ev/s)"
        a, b = total(stack_a, result_a), total(stack_b, result_b)
        lines.append(f"{label:>24} {a:>9.2f} {b:>9.2f} {_change(a, b)}")
    load_a = next((result for result in cpu_a if result["phase"] == "load"), None)
    load_b = by_phase_b.get("load")
    if (load_a is not None) and (load_b is not None):
        lines += ["", "Load window per component, % of one CPU", header, "-" * len(header)]
        for component, key in COMPONENTS:
            a, b = pct(load_a, stack_a["components"][key]), pct(load_b, stack_b["components"][key])
            lines.append(f"{component:>24} {a:>9.2f} {b:>9.2f} {_change(a, b)}")
        a, b = total(stack_a, load_a), total(stack_b, load_b)
        lines.append(f"{'total':>24} {a:>9.2f} {b:>9.2f} {_change(a, b)}")
        a, b = busy_pct(load_a), busy_pct(load_b)
        lines.append(f"{'busy (all CPUs)':>24} {a:>9.2f} {b:>9.2f} {_change(a, b)}")
        lines += ["", "CPU per event in the load window, us", header, "-" * len(header)]
        for component, key in COMPONENTS + (("total", None),):
            values = []
            for stack, result in ((stack_a, load_a), (stack_b, load_b)):
                groups = (
                    stack["components"][key]
                    if key is not None
                    else [group for item in stack["components"] for group in stack["components"][item]]
                )
                cpu_ms = sum(result["groups"].get(group, {}).get("cpu_ms", 0.0) for group in groups)
                values.append(cpu_ms * 1000.0 / max(1, result["tx_events"] + result["rx_events"]))
            lines.append(f"{component:>24} {values[0]:>9.2f} {values[1]:>9.2f} {_change(*values)}")
    if rows_a and rows_b:
        a, b = delivery(rows_a), delivery(rows_b)
        lines += ["", "Delivery in the load window, all classes", header, "-" * len(header)]
        for name, key, form in (
            ("received events", "received", "d"),
            ("lost events", "lost", "d"),
            ("duplicates", "duplicates", "d"),
            ("worst jitter p99, ms", "jitter_p99", ".2f"),
        ):
            lines.append(f"{name:>24} {a[key]:>9{form}} {b[key]:>9{form}} {_change(float(a[key]), float(b[key]))}")
    return "\n".join(lines) + "\n"


def report(target, peer):
    """Prints the report of one run; returns its CPU results and merged rows."""
    sd_results = parse(target)
    rows = merge(sd_results, parse(peer)) if sd_results else []
    if rows:
        print(format_results(rows))
    cpu_results = parse(target, CPU_PREFIX)
    if cpu_results:
        print(format_cpu(cpu_results))
    return cpu_results, rows


def main(argv):
    if (len(argv) == 6) and (argv[1] == "--compare"):
        texts = [pathlib.Path(path).read_text() for path in argv[2:]]
        print("=== A ===")
        cpu_a, rows_a = report(texts[0], texts[1])
        print("=== B ===")
        cpu_b, rows_b = report(texts[2], texts[3])
        if cpu_a and cpu_b:
            print(format_comparison(cpu_a, cpu_b, rows_a, rows_b))
        return 0
    if len(argv) not in (2, 3):
        print(__doc__)
        return 1
    target = pathlib.Path(argv[1]).read_text()
    peer = pathlib.Path(argv[2]).read_text() if len(argv) == 3 else ""
    report(target, peer)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
