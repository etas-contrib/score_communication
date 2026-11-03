/********************************************************************************
 * Copyright (c) 2026 Contributors to the Eclipse Foundation
 *
 * See the NOTICE file(s) distributed with this work for additional
 * information regarding copyright ownership.
 *
 * This program and the accompanying materials are made available under the
 * terms of the Apache License Version 2.0 which is available at
 * https://www.apache.org/licenses/LICENSE-2.0
 *
 * SPDX-License-Identifier: Apache-2.0
 ********************************************************************************/

// Remote peer of the mixed load benchmark on the upstream lg-someip stack (requires a running someipd in the same
// namespace): publishes the RX streams of the workload and subscribes the TX streams of the benchmarked side.
// On SIGINT/SIGTERM it reports per event class sent counts (RX) and loss, duplicates and arrival jitter of the TX
// samples sent within the benchmarked side's measurement window.

#include "experimental/someip_sd/benchmark/bench_protocol.h"
#include "experimental/someip_sd/benchmark/load_workload.h"

#include <lgsomeip/LgsomeipApi.h>

#include <signal.h>

#include <algorithm>
#include <atomic>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace
{

namespace bench = score::someip_sd::benchmark;
namespace load = score::someip_sd::benchmark::load;

using load::Nanoseconds;

constexpr char kApplicationName[] = "someip-load-peer";

struct Options
{
    std::string config;
    std::string workload{"./etc/workload.txt"};
    std::string label{"lg-someip"};
    double scale{1.0};
    std::vector<double> cpu_scales;
    // Bytes before the header in every payload; 4 for a peer serializing the payload as a dynamic array with a
    // 4 byte length field (Vector ara::com), which is written there.
    std::uint32_t payload_offset{0U};
};

bool ParseOptions(int argc, char** argv, Options& options)
{
    if (argc < 2)
    {
        return false;
    }
    options.config = argv[1];
    for (int index = 2; index + 1 < argc; index += 2)
    {
        const std::string name{argv[index]};
        const std::string text{argv[index + 1]};
        if (name == "--workload")
        {
            options.workload = text;
        }
        else if (name == "--label")
        {
            options.label = text;
        }
        else if (name == "--scale")
        {
            options.scale = std::stod(text);
        }
        else if (name == "--cpu-scales")
        {
            if (!load::ParseScales(text, options.cpu_scales))
            {
                return false;
            }
        }
        else if (name == "--payload-offset")
        {
            options.payload_offset = static_cast<std::uint32_t>(std::stoul(text));
        }
        else
        {
            return false;
        }
    }
    return ((argc % 2) == 0) && (options.scale > 0.0) && ((options.payload_offset == 0U) || (options.payload_offset == 4U));
}

}  // namespace

int main(int argc, char** argv)
{
    Options options{};
    if (!ParseOptions(argc, argv, options))
    {
        std::cerr << "usage: " << argv[0]
                  << " <lg-someip config json> [--workload ./etc/workload.txt] [--scale 1.0] [--label lg-someip]"
                     " [--cpu-scales 0.25,0.5] [--payload-offset 0|4]"
                  << std::endl;
        return EXIT_FAILURE;
    }
    load::Workload workload{};
    if (!load::ReadWorkload(options.workload, workload))
    {
        std::cerr << "reading workload " << options.workload << " failed" << std::endl;
        return EXIT_FAILURE;
    }

    sigset_t signals{};
    sigemptyset(&signals);
    sigaddset(&signals, SIGINT);
    sigaddset(&signals, SIGTERM);
    pthread_sigmask(SIG_BLOCK, &signals, nullptr);

    auto application = lgsomeip::api::Runtime::instance().create_application(kApplicationName, options.config);
    if (!application->init())
    {
        std::cerr << "init failed" << std::endl;
        return EXIT_FAILURE;
    }

    // RX streams are provided here, TX streams are consumed.
    std::map<std::pair<std::uint16_t, std::uint16_t>, std::vector<std::size_t>> provided;
    std::map<std::pair<std::uint16_t, std::uint16_t>, std::vector<std::size_t>> consumed;
    std::vector<std::size_t> rx_indices;
    std::vector<Nanoseconds> rx_periods;
    std::vector<Nanoseconds> periods(workload.streams.size(), 0);
    for (std::size_t index = 0U; index < workload.streams.size(); ++index)
    {
        const auto& stream = workload.streams[index];
        periods[index] = load::PeriodNs(stream, options.scale);
        const auto key = std::make_pair(stream.service, stream.instance);
        if (workload.IsTx(stream))
        {
            consumed[key].push_back(index);
        }
        else
        {
            provided[key].push_back(index);
            rx_indices.push_back(index);
            rx_periods.push_back(load::PeriodNs(stream, 1.0));
        }
    }

    // Follows the load phase of the benchmarked side (sent in the TX sequence numbers): 0 is --scale, n the n-th
    // --cpu-scales factor of it.
    std::vector<double> load_phases{options.scale};
    for (const auto factor : options.cpu_scales)
    {
        load_phases.push_back(options.scale * factor);
    }
    std::atomic<std::uint32_t> load_phase{0U};
    std::mutex mutex;
    std::vector<load::ReceiveTracker> trackers(workload.streams.size());
    std::vector<std::vector<Nanoseconds>> jitter_ns(workload.classes.size());
    for (const auto& [key, streams] : consumed)
    {
        for (const auto index : streams)
        {
            const auto& stream = workload.streams[index];
            application->register_message_handler(
                stream.service,
                stream.instance,
                stream.event,
                [&, index](const std::shared_ptr<lgsomeip::api::Message>& message) {
                    const auto arrival = load::NowNs();
                    if ((message->type != lgsomeip::api::MessageType::Notification) ||
                        (message->payload.size() < options.payload_offset + bench::kHeaderSize))
                    {
                        return;
                    }
                    const auto header = bench::DecodeHeader(message->payload.data() + options.payload_offset);
                    const auto phase = (header.sequence >> load::kPhaseShift) & load::kPhaseMask;
                    if (phase < load_phases.size())
                    {
                        load_phase.store(phase);
                    }
                    if ((header.sequence & load::kMeasuredFlag) == 0U)
                    {
                        return;
                    }
                    std::lock_guard<std::mutex> lock{mutex};
                    trackers[index].OnSample(header.sequence & load::kCounterMask,
                                             arrival,
                                             periods[index],
                                             jitter_ns[workload.streams[index].event_class]);
                });
        }
        const auto eventgroup = workload.streams[streams.front()].eventgroup;
        // lg-someip drops a subscribe for a not yet available service and does not repeat it.
        application->register_availability_handler(
            key.first,
            key.second,
            [&application, eventgroup](
                lgsomeip::api::service_t service, lgsomeip::api::instance_t instance, bool available) {
                if (available)
                {
                    application->subscribe(service, instance, eventgroup);
                }
            });
    }

    std::atomic<bool> set_up{false};
    application->register_application_state_handler([&](bool registered) {
        std::cout << "load_peer: daemon " << (registered ? "registered" : "deregistered") << std::endl;
        if ((!registered) || set_up.load())
        {
            return;
        }
        for (const auto& [key, streams] : provided)
        {
            for (const auto index : streams)
            {
                const auto& stream = workload.streams[index];
                application->offer_event(stream.service,
                                         stream.instance,
                                         stream.event,
                                         std::set<lgsomeip::api::eventgroup_t>{stream.eventgroup},
                                         false);
            }
            application->offer_service(key.first, key.second);
        }
        for (const auto& [key, streams] : consumed)
        {
            application->request_service(key.first, key.second);
            for (const auto index : streams)
            {
                const auto& stream = workload.streams[index];
                application->request_event(stream.service,
                                           stream.instance,
                                           stream.event,
                                           std::set<lgsomeip::api::eventgroup_t>{stream.eventgroup});
            }
        }
        set_up.store(true);
    });
    application->start();

    std::atomic<bool> stop{false};
    std::vector<std::uint32_t> sequences(workload.streams.size(), 0U);
    std::vector<load::SendCounters> sent(workload.classes.size());
    std::thread sender{[&]() {
        const load::Pacer pacer{rx_indices, rx_periods};
        pacer.Run(
            [&stop]() {
                return stop.load();
            },
            [&]() {
                return load_phases[load_phase.load()];
            },
            [&](std::size_t index, Nanoseconds lag_ns) {
                if (!set_up.load())
                {
                    return;
                }
                const auto& stream = workload.streams[index];
                const auto size = std::max<std::size_t>(stream.payload, options.payload_offset + bench::kHeaderSize);
                lgsomeip::api::Payload payload(size, 0U);
                if (options.payload_offset > 0U)
                {
                    const auto length = size - options.payload_offset;
                    for (std::uint32_t byte = 0U; byte < 4U; ++byte)
                    {
                        payload[byte] = static_cast<std::uint8_t>(length >> (8U * (3U - byte)));
                    }
                }
                bench::EncodeHeader(bench::Header{sequences[index]++, static_cast<std::uint64_t>(load::NowNs())},
                                    payload.data() + options.payload_offset);
                application->notify(stream.service, stream.instance, stream.event, payload);
                auto& counters = sent[stream.event_class];
                ++counters.sent;
                counters.max_lag_ns = std::max(counters.max_lag_ns, lag_ns);
            });
    }};

    int signal_number{0};
    static_cast<void>(sigwait(&signals, &signal_number));
    stop.store(true);
    sender.join();

    {
        std::lock_guard<std::mutex> lock{mutex};
        for (const auto& item : workload.classes)
        {
            std::vector<const load::ReceiveTracker*> class_trackers;
            const load::Stream* first{nullptr};
            for (std::size_t index = 0U; index < workload.streams.size(); ++index)
            {
                if (workload.streams[index].event_class == item.index)
                {
                    first = (first == nullptr) ? &workload.streams[index] : first;
                    class_trackers.push_back(&trackers[index]);
                }
            }
            if (first == nullptr)
            {
                continue;
            }
            load::ClassResult result{options.label, "peer", item, *first, options.scale};
            if (item.tx)
            {
                result.AddReceived(class_trackers, jitter_ns[item.index]);
            }
            else
            {
                result.AddSent(sent[item.index]);
            }
            std::cout << result.Line() << std::endl;
        }
    }

    for (const auto& [key, streams] : consumed)
    {
        application->unsubscribe(key.first, key.second, workload.streams[streams.front()].eventgroup);
    }
    for (const auto& entry : provided)
    {
        application->stop_offer_service(entry.first.first, entry.first.second);
    }
    application->stop();
    return EXIT_SUCCESS;
}
