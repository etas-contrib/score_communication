#!/bin/bash
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
# Runs the mixed load benchmark on IPNext hardware from a Linux host in the target's network (e.g. the jump server),
# end to end: deploys the target bundle, starts the load peer on this host, runs the benchmark on the target, stops
# the peer, fetches the results, prints the report and resets the target. It can be run repeatedly; each run gets its
# own directory. Stacks: "iosock" (io-sock event module, run_target.sh --app someip_load_lola) or "vector" (the
# image's Vector ara::com stack someipd_posix, safe-posix-platform someip_bench/load, run_target_vector_load.sh).
#
# usage: run_hw_load.sh [options] [-- someip_load_lola/SomeipLoadAra options, default --cpu-scales 0.25,0.5]
#   --stack iosock|vector     default iosock
#   --no-udp-collection       vector only: sets the 5 ms UDP collection timeout of the provided events to 0
#   --target-bundle <tar.gz>  iosock: load_target_bundle (--config=qnx_arm64), default ./load_target_bundle.tar.gz;
#                             vector: someip_bench/load:target_bundle (--config=ipn10_qnx8),
#                             default ./vector_load_target_bundle.tar.gz
#   --peer-bundle <tar.gz>    load_host_bundle built for this host's architecture, default ./load_host_bundle.tar.gz
#   --diag <qqvp_diag.py>     DoIP tool of safe-posix-platform (someip_bench/qqvp), default ./qqvp_diag.py; resets the
#                             target before the run if the event module is still loaded, and after the run
#   --no-reset                keep the target as it is after the run (module loaded, someipd_posix stopped)
#   --target <ssh host>       default ipnext;           --target-ip <ip>   default 160.48.199.101
#   --host-ip <ip>            default 160.48.199.15;    --interface <if>   default vlan_73
#   --target-rx-port <port>   default 31338;            --peer-rx-port <port> default 34501
#   --target-dir <dir>        bundle directory on the target, default /persistent/someip_bench
#   --work <dir>              runs go to <dir>/runs/<timestamp>, default ~/someip_load_hw
# The network forwards only SD 239.192.255.251:30490 and a few unicast ports, hence one RX port per side. The SD port
# and the peer RX port must be free on this host: stop other SOME/IP peers (e.g. a reflector) first.
set -euo pipefail

STACK=iosock
NO_UDP_COLLECTION=""
TARGET_BUNDLE=""
PEER_BUNDLE=$PWD/load_host_bundle.tar.gz
DIAG=$PWD/qqvp_diag.py
RESET_AFTER=1
TARGET=ipnext
TARGET_IP=160.48.199.101
HOST_IP=160.48.199.15
INTERFACE=vlan_73
TARGET_RX_PORT=31338
PEER_RX_PORT=34501
SD_MULTICAST=239.192.255.251
SD_PORT=30490
TARGET_DIR=/persistent/someip_bench
WORK=$HOME/someip_load_hw
while [ $# -gt 0 ]; do
    case "$1" in
        --stack) STACK=$2; shift 2 ;;
        --no-udp-collection) NO_UDP_COLLECTION=--no-udp-collection; shift ;;
        --target-bundle) TARGET_BUNDLE=$(realpath "$2"); shift 2 ;;
        --peer-bundle) PEER_BUNDLE=$(realpath "$2"); shift 2 ;;
        --diag) DIAG=$(realpath "$2"); shift 2 ;;
        --no-reset) RESET_AFTER=0; shift ;;
        --target) TARGET=$2; shift 2 ;;
        --target-ip) TARGET_IP=$2; shift 2 ;;
        --host-ip) HOST_IP=$2; shift 2 ;;
        --interface) INTERFACE=$2; shift 2 ;;
        --target-rx-port) TARGET_RX_PORT=$2; shift 2 ;;
        --peer-rx-port) PEER_RX_PORT=$2; shift 2 ;;
        --target-dir) TARGET_DIR=$2; shift 2 ;;
        --work) WORK=$2; shift 2 ;;
        --) shift; break ;;
        *) sed -n '14,35p' "$0"; exit 1 ;;
    esac
done
case "$STACK" in
    iosock) DEPLOY_NAME=someip_load; TITLE="io-sock event module"
        TARGET_BUNDLE=${TARGET_BUNDLE:-$PWD/load_target_bundle.tar.gz} ;;
    vector) DEPLOY_NAME=someip_load_vector; TITLE="Vector ara::com, someipd_posix"
        TARGET_BUNDLE=${TARGET_BUNDLE:-$PWD/vector_load_target_bundle.tar.gz} ;;
    *) echo "run_hw_load: unknown stack $STACK"; exit 1 ;;
esac
APP_ARGS=("$@")
if [ ${#APP_ARGS[@]} -eq 0 ]; then
    APP_ARGS=(--cpu-scales 0.25,0.5)
fi
# The peer must follow the target's rate scale and CPU sweep.
PEER_ARGS=(--rx-port "$PEER_RX_PORT")
if [ "$STACK" = vector ]; then
    # ara::core::Vector payload: 4 byte length field before the benchmark header.
    PEER_ARGS+=(--payload-offset 4)
fi
for ((i = 0; i + 1 < ${#APP_ARGS[@]}; i++)); do
    case "${APP_ARGS[i]}" in
        --scale | --cpu-scales) PEER_ARGS+=("${APP_ARGS[i]}" "${APP_ARGS[i + 1]}") ;;
    esac
done

log() {
    echo "run_hw_load: $*"
}
on_target() {
    ssh -o BatchMode=yes -o ConnectTimeout=10 "$TARGET" "$@"
}
# QNX's sshd needs the legacy protocol on some images, this host's scp may not know -O.
to_target() {
    scp -q -r -p "$@" 2> /dev/null || scp -q -O -r -p "$@"
}
module_loaded() {
    on_target "ls /dev/io-sock/mods-someip-event.so" > /dev/null 2>&1
}
reset_target() {
    if [ ! -f "$DIAG" ]; then
        log "cannot reset the target: $DIAG not found (see --diag)"
        return 1
    fi
    log "resetting the target"
    python3 "$DIAG" reset
    # The target answers ssh for a moment after the reset request.
    sleep 20
    for _ in $(seq 60); do
        if on_target true 2> /dev/null; then
            log "target is back"
            return 0
        fi
        sleep 5
    done
    log "target did not come back"
    return 1
}

for f in "$TARGET_BUNDLE" "$PEER_BUNDLE"; do
    [ -f "$f" ] || { log "$f not found"; exit 1; }
done
RUN=$WORK/runs/$(date +%Y%m%d_%H%M%S)
mkdir -p "$RUN/deploy" "$RUN/peer" "$RUN/target"
ln -sfn "$RUN" "$WORK/latest"
if [ "$STACK" = vector ]; then
    mkdir -p "$RUN/deploy/$DEPLOY_NAME"
    tar -xzf "$TARGET_BUNDLE" -C "$RUN/deploy/$DEPLOY_NAME"
else
    tar -xzf "$TARGET_BUNDLE" -C "$RUN/deploy"
fi
tar -xzf "$PEER_BUNDLE" -C "$RUN"
PEER_PREFIX=$RUN/someip_load_peer
# ELF e_machine: 62 x86_64, 183 aarch64
case "$(uname -m):$(od -An -tu2 -j18 -N2 "$PEER_PREFIX/bin/someipd" | tr -d ' ')" in
    x86_64:62 | aarch64:183) ;;
    *) log "$PEER_BUNDLE is not built for $(uname -m)"; exit 1 ;;
esac
log "run directory $RUN, stack $STACK"

BUSY=$(ss -Huan "( sport = :$SD_PORT or sport = :$PEER_RX_PORT )" || true)
if [ -n "$BUSY" ]; then
    log "UDP ports $SD_PORT/$PEER_RX_PORT are in use on this host, stop the other SOME/IP peer first:"
    echo "$BUSY"
    exit 1
fi
on_target true || { log "target $TARGET not reachable"; exit 1; }
if module_loaded; then
    log "the event module is still loaded in io-sock (it cannot be unloaded)"
    reset_target || exit 1
    module_loaded && { log "module still loaded after the reset"; exit 1; }
fi

log "deploying the bundle to $TARGET:$TARGET_DIR/$DEPLOY_NAME"
on_target "mkdir -p $TARGET_DIR && rm -rf $TARGET_DIR/$DEPLOY_NAME $TARGET_DIR/work"
to_target "$RUN/deploy/$DEPLOY_NAME" "$TARGET:$TARGET_DIR/"

log "starting the load peer: ${PEER_ARGS[*]}"
# A background job of a script ignores SIGINT; the peer stops with SIGTERM and then writes its results.
SOMEIP_BENCH_PEER_WORK=$RUN/peer "$PEER_PREFIX/scripts/run_host_load_peer.sh" "$HOST_IP" "$INTERFACE" \
    "$SD_MULTICAST" "$SD_PORT" "${PEER_ARGS[@]}" > "$RUN/peer_run.log" 2>&1 < /dev/null &
PEER_PID=$!
stop_peer() {
    if kill -0 "$PEER_PID" 2> /dev/null; then
        log "stopping the load peer"
        kill -TERM "$PEER_PID" 2> /dev/null || true
        wait "$PEER_PID" || true
    fi
}
trap stop_peer EXIT
sleep 5
kill -0 "$PEER_PID" 2> /dev/null || { log "load peer exited, see $RUN/peer_run.log"; cat "$RUN/peer_run.log"; exit 1; }

log "running the benchmark on the target: ${APP_ARGS[*]}"
TARGET_RC=0
if [ "$STACK" = vector ]; then
    TARGET_CMD="$TARGET_DIR/$DEPLOY_NAME/scripts/run_target_vector_load.sh --disable-firewall $NO_UDP_COLLECTION \
--rx-port $TARGET_RX_PORT"
else
    TARGET_CMD="$TARGET_DIR/$DEPLOY_NAME/scripts/run_target.sh $TARGET_IP --disable-firewall --stop someipd_posix \
--sd-multicast $SD_MULTICAST --sd-port $SD_PORT --rx-port $TARGET_RX_PORT --app someip_load_lola"
fi
on_target "SOMEIP_BENCH_WORK=$TARGET_DIR/work /bin/sh $TARGET_CMD ${APP_ARGS[*]}" 2>&1 | tee "$RUN/target_run.log" \
    || TARGET_RC=1
stop_peer
trap - EXIT

log "fetching the target results"
scp -q -o BatchMode=yes "$TARGET:$TARGET_DIR/work/*" "$RUN/target/" 2> /dev/null \
    || scp -q -O -o BatchMode=yes "$TARGET:$TARGET_DIR/work/*" "$RUN/target/" \
    || log "could not fetch $TARGET_DIR/work"

if [ -s "$RUN/target/results.txt" ]; then
    echo
    echo "SOME/IP mixed load benchmark ($TITLE on $TARGET)"
    if [ -s "$RUN/peer/results.txt" ]; then
        python3 "$PEER_PREFIX/scripts/load_report.py" "$RUN/target/results.txt" "$RUN/peer/results.txt" \
            | tee "$RUN/report.txt"
    else
        log "no peer results, see $RUN/peer_run.log; target side only"
        python3 "$PEER_PREFIX/scripts/load_report.py" "$RUN/target/results.txt" | tee "$RUN/report.txt"
    fi
    echo
else
    log "no target results, see $RUN/target_run.log"
    TARGET_RC=1
fi

if [ "$RESET_AFTER" -eq 1 ]; then
    if reset_target; then
        on_target "pidin -f an" | grep -q someipd_posix && log "someipd_posix is running again" \
            || log "someipd_posix is not running after the reset"
    else
        TARGET_RC=1
    fi
else
    log "target not reset: the module (iosock) stays loaded and the image's someipd_posix stopped until the next reset"
fi
log "results, CPU samples and logs: $RUN"
exit $TARGET_RC
