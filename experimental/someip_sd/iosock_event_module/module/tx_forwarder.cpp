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
#include "experimental/someip_sd/iosock_event_module/module/tx_forwarder.h"

#include "experimental/someip_sd/iosock_event_module/module/event_plugin.h"
#include "experimental/someip_sd/iosock_event_module/module/log.h"
#include "experimental/someip_sd/iosock_event_module/module/wakeup_signal.h"

#include <algorithm>
#include <atomic>
#include <cstddef>
#include <new>
#include <string>
#include <utility>

namespace score::someip_sd::iosock_event_module
{
namespace
{

namespace com = score::mw::com;

using SampleCount = std::size_t;
using TxReferenceCount = std::uint64_t;

// GetNewSamples() returns only the newest samples, so this bounds the backlog that is forwarded without loss.
// TODO(production): take it from the deployment; it must fit the provider's numberOfSampleSlots.
constexpr SampleCount kTxMaxSamples{8U};
// One sample slot stays free for the last value of a field.
constexpr SampleCount kTxMaxNewSamples{kTxMaxSamples - 1U};

// Updated from the mbuf free callback, which may run outside the kernel thread.
std::atomic<TxReferenceCount> pending_tx{0U};
std::atomic<TxReferenceCount> released_tx{0U};
// Set while the kernel thread waits for sample references to be released by the network.
std::atomic<bool> release_awaited{false};

}  // namespace

TxForwarder::ProxyOwner::~ProxyOwner()
{
    // Unsubscribe() also removes the receive handler and waits for a running one to finish.
    for (auto& entry : subscribed)
    {
        entry.second.event->Unsubscribe();
    }
}

void TxForwarder::AddProvidedService(const protocol::Message& message)
{
    if (!sockets_.OpenUdp(message.local_port))
    {
        return;
    }
    const ServiceInstance key{message.service, message.instance};
    auto& service = provided_[key];
    service.port = message.local_port;
    service.major = message.major;
    for (const auto event : message.events)
    {
        service.events[event].is_field =
            std::find(message.fields.begin(), message.fields.end(), event) != message.fields.end();
    }
    if (!service.find_handle.has_value())
    {
        service.discovered = std::make_shared<DiscoveredProvider>();
        const auto name = protocol::LolaInstanceSpecifier(key.first, key.second);
        auto specifier = com::InstanceSpecifier::Create(std::string{name});
        if (!specifier.has_value())
        {
            someip_log(SOMEIP_LOG_ERROR, "invalid provided instance specifier %s", name.c_str());
            return;
        }
        // LoLa also calls the handler for a provider offered before the search started.
        auto find = com::GenericProxy::StartFindService(
            [discovered = service.discovered](auto handles, auto) noexcept {
                {
                    std::lock_guard<std::mutex> lock{discovered->mutex};
                    discovered->handle.reset();
                    if (!handles.empty())
                    {
                        discovered->handle.emplace(handles.front());
                    }
                    discovered->changed = true;
                }
                someip_wakeup_signal(SOMEIP_WAKEUP_TX);
            },
            std::move(specifier).value());
        if (!find.has_value())
        {
            someip_log(SOMEIP_LOG_ERROR, "starting LoLa service discovery %s failed", name.c_str());
            return;
        }
        service.find_handle = std::move(find).value();
    }
    someip_log(SOMEIP_LOG_INFO,
               "providing 0x%04x.0x%04x",
               static_cast<unsigned>(key.first),
               static_cast<unsigned>(key.second));
}

OperationSucceeded TxForwarder::RemoveProvidedService(const ServiceInstance& key)
{
    const auto provided = provided_.find(key);
    if (provided == provided_.end())
    {
        return false;
    }
    if (provided->second.find_handle.has_value())
    {
        static_cast<void>(com::GenericProxy::StopFindService(provided->second.find_handle.value()));
    }
    if (provided->second.owner)
    {
        retired_[key] = std::move(provided->second.owner);
    }
    for (const auto& event : provided->second.events)
    {
        if (event.second.skipped != 0U)
        {
            someip_log(SOMEIP_LOG_WARNING,
                       "event 0x%04x: %llu LoLa samples skipped in total",
                       static_cast<unsigned>(event.first),
                       static_cast<unsigned long long>(event.second.skipped));
        }
    }
    provided_.erase(provided);
    someip_log(SOMEIP_LOG_INFO,
               "stopped providing 0x%04x.0x%04x",
               static_cast<unsigned>(key.first),
               static_cast<unsigned>(key.second));
    someip_log(SOMEIP_LOG_INFO, "pending TX references: %llu", static_cast<unsigned long long>(pending_tx.load()));
    return true;
}

void TxForwarder::AddRemoteSubscriber(const protocol::Message& message)
{
    const ServiceInstance key{message.service, message.instance};
    const auto service = provided_.find(key);
    if (service == provided_.end())
    {
        someip_log(SOMEIP_LOG_WARNING,
                   "remote subscriber for unknown provided service 0x%04x.0x%04x",
                   static_cast<unsigned>(key.first),
                   static_cast<unsigned>(key.second));
        return;
    }
    const auto found = service->second.events.find(message.event);
    if (found == service->second.events.end())
    {
        someip_log(SOMEIP_LOG_WARNING,
                   "remote subscriber for unknown event 0x%04x",
                   static_cast<unsigned>(message.event));
        return;
    }
    auto& event = found->second;
    const Endpoint endpoint{message.remote_ip, message.remote_port};
    if (!event.subscribers.insert(endpoint).second)
    {
        //TODO: silent bailout really good?
        return;
    }
    if (event.is_field && event.last_value)
    {
        SendNotification(key, service->second, message.event, event, event.last_value, {endpoint});
        someip_log(SOMEIP_LOG_INFO,
                   "sent initial field value of event 0x%04x",
                   static_cast<unsigned>(message.event));
    }
}

void TxForwarder::RemoveRemoteSubscriber(const protocol::Message& message)
{
    const auto service = provided_.find(ServiceInstance{message.service, message.instance});
    if (service == provided_.end())
    {
        return;
    }
    const auto event = service->second.events.find(message.event);
    if (event != service->second.events.end())
    {
        event->second.subscribers.erase(Endpoint{message.remote_ip, message.remote_port});
    }
}

void TxForwarder::ReleaseRetiredProxies()
{
    // Announced before checking, so a release in between is not missed.
    release_awaited.store(!retired_.empty());
    // Pairs with the fence in ReleaseTxContext(): either use_count() sees the release or the releaser signals.
    std::atomic_thread_fence(std::memory_order_seq_cst);
    for (auto retired = retired_.begin(); retired != retired_.end();)
    {
        if (retired->second.use_count() == 1)
        {
            retired = retired_.erase(retired);
        }
        else
        {
            ++retired;
        }
    }
}

//TODO: connect only to services that have new subscribers
void TxForwarder::ConnectProvidedServices()
{
    for (auto& entry : provided_)
    {
        auto& service = entry.second;
        // Do not re-subscribe the same PID/event before the old proxy has released all of its samples.
        if (service.owner || (retired_.count(entry.first) != 0U) || !service.discovered)
        {
            continue;
        }
        std::optional<com::GenericProxy::HandleType> handle;
        {
            std::lock_guard<std::mutex> lock{service.discovered->mutex};
            if (!service.discovered->changed)
            {
                continue;
            }
            service.discovered->changed = false;
            handle = service.discovered->handle;
        }
        if (handle.has_value())
        {
            service.owner = CreateProxyOwner(entry.first, service, handle.value());
        }
    }
}

std::shared_ptr<TxForwarder::ProxyOwner> TxForwarder::CreateProxyOwner(const ServiceInstance& key,
                                                                        ProvidedService& service,
                                                                        const com::GenericProxy::HandleType& handle)
{
    const auto name = protocol::LolaInstanceSpecifier(key.first, key.second);
    auto proxy = com::GenericProxy::Create(handle);
    if (!proxy.has_value())
    {
        someip_log(SOMEIP_LOG_ERROR, "creating LoLa proxy %s failed", name.c_str());
        return nullptr;
    }
    auto owner = std::make_shared<ProxyOwner>();
    owner->proxy = std::make_unique<com::GenericProxy>(std::move(proxy).value());
    for (auto& event : service.events)
    {
        const auto found = owner->proxy->GetEvents().find(protocol::LolaEventName(event.first));
        if (found == owner->proxy->GetEvents().cend())
        {
            someip_log(SOMEIP_LOG_WARNING,
                       "LoLa service %s has no event 0x%04x",
                       name.c_str(),
                       static_cast<unsigned>(event.first));
            continue;
        }
        if (!found->second.Subscribe(kTxMaxSamples).has_value())
        {
            someip_log(SOMEIP_LOG_ERROR, "subscribing LoLa event 0x%04x failed", static_cast<unsigned>(event.first));
            continue;
        }
        auto& subscription = owner->subscribed[event.first];
        subscription.event = &found->second;
        auto* const pending = &subscription.pending;
        const auto handler = found->second.SetReceiveHandler([pending]() noexcept {
            pending->store(true);
            someip_wakeup_signal(SOMEIP_WAKEUP_TX);
        });
        if (!handler.has_value())
        {
            someip_log(SOMEIP_LOG_ERROR,
                       "setting LoLa receive handler of event 0x%04x failed",
                       static_cast<unsigned>(event.first));
            found->second.Unsubscribe();
            owner->subscribed.erase(event.first);
            continue;
        }
        event.second.subscription = &subscription;
    }
    someip_log(SOMEIP_LOG_INFO, "consuming LoLa service %s of the local provider", name.c_str());
    return owner;
}

void TxForwarder::ForwardSamples()
{
    for (auto& entry : provided_)
    {
        for (auto& element : entry.second.events)
        {
            auto& event = element.second;
            if ((event.subscription == nullptr) || !event.subscription->pending.load())
            {
                continue;
            }
            auto& proxy_event = *event.subscription->event;
            auto free_samples = proxy_event.GetFreeSampleCount();
            if (free_samples == 0U)
            {
                // Announced before checking again, so a release in between is not missed.
                release_awaited.store(true);
                // Pairs with the fence in ReleaseTxContext(): either the re-check sees the freed slot or the releaser
                // sees release_awaited and signals. LoLa frees the slot through its own atomics.
                std::atomic_thread_fence(std::memory_order_seq_cst);
                free_samples = proxy_event.GetFreeSampleCount();
                if (free_samples == 0U)
                {
                    continue;
                }
            }
            event.subscription->pending.store(false);
            const auto limit = std::min(free_samples, kTxMaxNewSamples);
            const auto available = proxy_event.GetNumNewSamplesAvailable();
            if (available.has_value() && (available.value() > limit))
            {
                if (event.skipped == 0U)
                {
                    someip_log(SOMEIP_LOG_WARNING,
                               "event 0x%04x: backlog of %zu LoLa samples exceeds %zu, older samples are skipped",
                               static_cast<unsigned>(element.first),
                               available.value(),
                               limit);  
                }
                event.skipped += available.value() - limit;
            }
            const auto result = proxy_event.GetNewSamples(
                [this, &entry, &element](auto sample) noexcept {
                    auto lease = std::make_shared<SampleLease>(SampleLease{entry.second.owner, std::move(sample)});
                    SendNotification(
                        entry.first, entry.second, element.first, element.second, lease, element.second.subscribers);
                    if (element.second.is_field)
                    {
                        element.second.last_value = std::move(lease);
                    }
                },
                limit);
            if (!result.has_value())
            {
                someip_log(SOMEIP_LOG_ERROR,
                           "getting LoLa samples for event 0x%04x failed",
                           static_cast<unsigned>(element.first));
            }
        }
    }
}

void TxForwarder::SendNotification(const ServiceInstance& key,
                                   ProvidedService& service,
                                   const EventId event_id,
                                   ProvidedEvent& event,
                                   const std::shared_ptr<SampleLease>& sample,
                                   const std::set<Endpoint>& receivers)
{
    const auto* data = static_cast<const protocol::EventSample*>(sample->sample.get());
    if ((data == nullptr) || (data->size > protocol::kMaxPayloadSize))
    {
        someip_log(SOMEIP_LOG_WARNING, "invalid LoLa sample for event 0x%04x", static_cast<unsigned>(event_id));
        return;
    }
    if (receivers.empty())
    {
        return;
    }
    event.session = (event.session == 0xFFFFU) ? 1U : static_cast<SessionId>(event.session + 1U);
    SomeipHeader header{};
    Write32(header.data(), (static_cast<WireWord>(key.first) << 16U) | event_id);
    Write32(&header[4], 8U + data->size);
    Write32(&header[8], event.session);
    header[12] = 1U;
    header[13] = service.major;
    header[14] = 2U;
    for (const auto& receiver : receivers)
    {
        //TODO: rework dynamic alloc here
        auto* context = new (std::nothrow) TxContext{sample};
        if (context == nullptr)
        {
            someip_log(SOMEIP_LOG_ERROR, "allocating TX ownership context failed");
            continue;
        }
        pending_tx.fetch_add(1U);
        const ErrorCode error = someip_event_send(service.port,
                                                  receiver.first.data(),
                                                  receiver.second,
                                                  header.data(),
                                                  data->payload.data(),
                                                  data->size,
                                                  context);
        if (error != 0)
        {
            someip_log(SOMEIP_LOG_ERROR,
                       "kernel sosend event 0x%04x failed: %d",
                       static_cast<unsigned>(event_id),
                       error);
        }
    }
    if (event.sent++ == 0U)
    {
        someip_log(SOMEIP_LOG_INFO,
                   "TX MEXTADD: first notification of 0x%04x.0x%04x event 0x%04x",
                   static_cast<unsigned>(key.first),
                   static_cast<unsigned>(key.second),
                   static_cast<unsigned>(event_id));
    }
}

void TxForwarder::ReleaseTxContext(const TxContextHandle context)
{
    delete static_cast<TxContext*>(context);
    pending_tx.fetch_sub(1U);
    if (released_tx.fetch_add(1U) == 0U)
    {
        someip_log(SOMEIP_LOG_INFO, "TX MEXTADD: first LoLa sample reference released");
    }
    // Orders the sample release above before reading release_awaited; pairs with the kernel thread's fences.
    std::atomic_thread_fence(std::memory_order_seq_cst);
    if (release_awaited.exchange(false))
    {
        someip_wakeup_signal(SOMEIP_WAKEUP_TX);
    }
}

}  // namespace score::someip_sd::iosock_event_module
