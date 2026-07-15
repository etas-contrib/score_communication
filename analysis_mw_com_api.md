# Analysis: the mw::com public API — semantics, full call chains, and the QNX primitives underneath

Audience: an engineer new to mw::com. Three parts:

1. **The API** — every public call (`score/mw/com/types.h`, `runtime.h`, `proxy_base.h`, `skeleton_base.h` at pin `0d2f535`) and what it means.
2. **The full chains** — one diagram per main API, from the public call through the internal layers (`impl` → LoLa binding → message passing → `QnxDispatchEngine`) down to the QNX kernel.
3. **The QNX primitives** — each OS call explained on its own, with usage diagram and gotcha.

Ends with the decoded "discovery cross-notifies both sides" walkthrough and a
quick-reference table.

## The mental model in five lines

- You write a **service interface** once: a C++ template listing methods, events, and fields.
- One process instantiates it as a **Skeleton** (the server: owns the data, serves the calls).
- Other processes instantiate it as a **Proxy** (the client: a stub that forwards to the skeleton).
- Code never hardcodes who talks to whom. It names abstract ports (`InstanceSpecifier`), and a JSON **manifest** maps each port to a transport deployment (LoLa/SHM here).
- **Service discovery** is the matchmaker: skeletons announce themselves (`OfferService`), proxies look for them (`FindService` / `StartFindService`), and a returned **handle** is the ticket to connect (`Proxy::Create`).

If you know AUTOSAR Adaptive: this is `ara::com` with the same words and roles.
Skeleton = server side, Proxy = client side, InstanceSpecifier + manifest =
deployment binding, LoLa = the shared-memory binding.

## Part 1 — the API, in lifecycle order

### 1.1 Runtime setup

| API | What it does |
|---|---|
| `runtime::InitializeRuntime(argc, argv)` | Boots the middleware inside the process. Parses `--service_instance_manifest <path>`, loads the JSON manifest, and builds the deployment tables. Must run before any skeleton/proxy is created. Second overload takes a `RuntimeConfiguration` directly. |
| `InstanceSpecifier::Create("repro/instance1")` | Makes a validated name for an abstract port. Returns a `Result` — the string must match the manifest. |
| `runtime::ResolveInstanceIDs(specifier)` | Looks the specifier up in the manifest and returns the concrete `InstanceIdentifier`s it maps to. Rarely needed directly; `FindService`/`Create` do it internally. |

Under the hood: `InitializeRuntime` only parses config and sets up singletons.
No threads toward peers, no shm yet. Each process is self-contained; there is no
central broker daemon.

### 1.2 Defining an interface

```cpp
template <typename T>
struct SimpleService : T::Base
{
    using T::Base::Base;
    typename T::template Method<std::int32_t(std::int32_t, std::int32_t)> add{*this, "add"};
    // also possible:
    // typename T::template Event<MyDataType> status{*this, "status"};
    // typename T::template Field<MyDataType, WithGetter, WithNotifier> config{*this, "config"};
};
using SimpleProxy    = score::mw::com::AsProxy<SimpleService>;
using SimpleSkeleton = score::mw::com::AsSkeleton<SimpleService>;
```

- The template parameter `T` is a trait pack. `AsSkeleton<SimpleService>` stamps the same body out with skeleton traits (each member becomes a real, serving element); `AsProxy<SimpleService>` stamps it with proxy traits (each member becomes a calling stub).
- Members register themselves with the enclosing object at construction (`{*this, "add"}` — the string is the wire name). No code generation, no reflection.
- Element kinds: **Method** (request/response), **Event** (publish/subscribe data stream), **Field** (a value with `WithGetter` / `WithSetter` / `WithNotifier` tags — get it, set it, or be notified on update).
- `GenericProxy` / `GenericSkeleton` are the type-erased variants: they speak to a service without compile-time knowledge of its type (events as raw bytes plus `DataTypeMetaInfo`). Used by tooling/gateways, not by normal app code.

### 1.3 Provider (skeleton) side

| API | What it does |
|---|---|
| `Skeleton::Create(specifier_or_identifier)` | Allocates the service instance: its shared-memory segments and bookkeeping. Returns `Result<Skeleton>`; the object is move-only (it owns OS resources). |
| `method.RegisterHandler(callable)` | Attaches the implementation to a method. Signature style at this pin: `void(ReturnType& result, const Args&... args)`. Register **before** offering, so no call can arrive handler-less. |
| `event.Send(value)` | Publishes a sample to all current subscribers (copy path). |
| `event.Allocate()` + `event.Send(SampleAllocateePtr)` | Zero-copy path: get a slot directly in shared memory, fill it in place, publish it. |
| `OfferService()` | Publishes the instance into service discovery. From this instant, peers can find and connect to it. |
| `StopOfferService()` | Withdraws the offer. Destruction of the skeleton does this implicitly. |

### 1.4 Consumer (proxy) side

| API | What it does |
|---|---|
| `Proxy::FindService(specifier)` | One-shot lookup: "who offers this port right now?" Returns `Result<ServiceHandleContainer<HandleType>>` — zero or more handles, one per live provider. No waiting; poll it if you must. |
| `Proxy::StartFindService(handler, specifier)` | Continuous discovery. Registers a callback `void(ServiceHandleContainer<HandleType>, FindServiceHandle)` that fires **now** with the current offers, and **again on every change** (new offer, withdrawn offer). Returns a `FindServiceHandle`. |
| `Proxy::StopFindService(handle)` | Cancels a `StartFindService` registration. |
| `Proxy::Create(handle)` | Connects to one concrete provider. This is the heavyweight call: it opens the provider's shared memory and performs a synchronous handshake per method (see the chain in 2.4). Returns `Result<Proxy>`, move-only. |
| `proxy.add(2, 3)` | Method call. Synchronous round trip into the provider; returns `Result<MethodReturnTypePtr<ReturnType>>` — the return value lives in shared memory, the ptr keeps its slot alive while you read it. |
| `event.Subscribe(max_sample_count)` | Joins an event stream and reserves `max_sample_count` slots for this subscriber. |
| `event.GetNewSamples(receiver, max)` | Pulls pending samples. The receiver gets `SamplePtr<T>` — a zero-copy view into the provider's data segment; holding it pins the slot. |
| `event.SetReceiveHandler(EventReceiveHandler)` | Push-style notification: "wake me when new samples exist". You still pull with `GetNewSamples`. |
| `event.GetSubscriptionState()` | `kSubscribed` / `kSubscriptionPending` / `kNotSubscribed`. |
| `event.Unsubscribe()` / proxy destruction | Releases the subscription; destruction of the proxy unsubscribes everything it holds. |

Error handling everywhere: no exceptions. Every fallible call returns
`score::Result<T>` (same idea as C++23 `std::expected`) — check `has_value()`,
extract with `.value()`, move-extract with `std::move(r).value()`.

### 1.5 The two transport planes (orientation for Part 2)

- **Bulk data plane: shared memory.** Event/field samples and method arguments/returns live in the provider's shm segments (`/dev/shmem/lola-ctl-…` control, `lola-data-…` data). Readers map them read-only. Zero-copy; nothing is serialized onto a socket.
- **Control plane: message passing.** Subscribe/unsubscribe, method invocation signaling, and update notifications are short messages. Platform-selected in `score/message_passing/BUILD`: `QnxDispatchEngine` (native QNX message passing) on QNX, `UnixDomainEngine` (UNIX sockets) on Linux.

Layer map, top to bottom:

```plantuml
@startuml
title Layer map: mw::com call -> QNX primitive
rectangle "mw::com public API\n(Skeleton/Proxy, Offer/Find/Create,\nmethods, events)" as API
rectangle "mw::com impl\n(SkeletonBase/ProxyBase, ServiceDiscovery)" as IMPL
rectangle "LoLa binding\n(lola::Skeleton/Proxy, FlagFile discovery,\nmessage_passing_service_instance)" as LOLA
rectangle "message passing\n(ClientConnection, MessagePassingClientCache,\nSendWaitReply)" as MP
rectangle "QnxDispatchEngine\nserver thread + client thread (PR 676)" as ENG
rectangle "QNX kernel\nMsgSend/Receive/Reply, pulses, resmgr,\nshm, timers, fd events" as K
API --> IMPL
IMPL --> LOLA
LOLA --> MP : control plane
LOLA --> K : data plane:\nshm_open + mmap,\ninotify-style discovery
MP --> ENG
ENG --> K : open/read/writev,\nresmgr_attach, pulses, timers
@enduml
```

## Part 2 — the full chains: public API → internals → QNX kernel

One diagram per main API. Left-to-right inside each process: app code, then the
internal layers, then the kernel between the processes. All function names are
from the `0d2f535` sources.

### 2.1 Engine startup (lazy, on first control-plane use)

```plantuml
@startuml
title First control-plane use -> QnxDispatchEngine with two DispatchThreadRunners
box "Process"
participant "app code\n(first OfferService / Create)" as APP
participant "message passing\n(engine factory)" as MP
participant "server thread\n(DispatchThreadRunner)" as S
participant "client thread\n(DispatchThreadRunner)" as C
end box
participant "QNX kernel\n(procnto)" as K
APP -> MP : first use of the control plane
MP -> K : DispatchThreadRunner::Start (x2):\ndispatch_create_channel,\nmessage_connect(MSG_FLAG_SIDE_CHANNEL),\npulse_attach(kQuitPulseCode),\ndispatch_context_alloc
MP -> K : InitServerThread:\nresmgr_attach("/mw_com/message_passing/<own id>")
MP -> K : InitClientThread:\npulse_attach(timer/select/CoidDeath),\nTimerCreate(CLOCK_MONOTONIC, SIGEV_PULSE)
MP -> S : launch thread
MP -> C : launch thread
loop RunOnThread — serve peers
  S -> K : dispatch_block (MsgReceive)
  K --> S : _IO_CONNECT / _IO_READ / _IO_WRITE from peers
  S -> S : dispatch_handler -> connect_funcs_/io_funcs_
end
loop RunOnThread — drive own client I/O
  C -> K : dispatch_block
  K --> C : pulses: timer / select / CoidDeath / quit
  C -> C : dispatch_handler -> endpoint & timer callbacks
end
@enduml
```

- `resmgr_attach` makes the process callable: its endpoint appears in the pathname space.
- The server thread only answers; the client thread does everything that may block toward peers. This split **is** the PR 676 fix (`analysis_pr676_fix.md`).

### 2.2 `Skeleton::Create` + `RegisterHandler` + `OfferService`

```plantuml
@startuml
title Skeleton::Create + OfferService — shm plus a flag file
box "Provider process"
participant "app code" as APP
participant "impl\n(SkeletonBase)" as IMPL
participant "LoLa\n(lola::Skeleton,\nShmPathBuilder,\nSharedMemoryFactory)" as LOLA
participant "impl\n(ServiceDiscovery)" as SD
end box
participant "QNX kernel /\nfilesystem" as K
box "Any consumer"
participant "SD worker\n(inotify watcher)" as W
end box
APP -> IMPL : SimpleSkeleton::Create(specifier)
IMPL -> LOLA : create binding for <service, instance>
LOLA -> K : shm_open("/lola-ctl-<svc>-<inst>", O_CREAT)\n+ ftruncate + mmap
LOLA -> K : shm_open("/lola-data-<svc>-<inst>", O_CREAT)\n+ ftruncate + mmap
note right of LOLA : segments under /dev/shmem;\nevent/field/method slots live here
APP -> IMPL : add.RegisterHandler(lambda)
IMPL -> IMPL : store type-erased handler\n(pure user space, no syscall)
APP -> IMPL : OfferService()
IMPL -> SD : ServiceDiscovery::OfferService
SD -> K : FlagFile: create marker in the discovery tree\n(/tmp_discovery on our guest)
K --> W : inotify-style event: new offer
note over W : every armed StartFindService\nfor this service fires its callback
@enduml
```

- `Create` costs shm syscalls only; no IPC toward anyone.
- `OfferService` is a **filesystem write**, not a message. Discovery has no daemon; the filesystem is the rendezvous point.

### 2.3 `FindService` / `StartFindService`

```plantuml
@startuml
title StartFindService — crawl once, then watch
box "Consumer process"
participant "app code" as APP
participant "impl\n(ProxyBase,\nServiceDiscovery)" as IMPL
participant "LoLa SD client\n(ServiceDiscoveryClient,\nFlagFileCrawler,\nos::InotifyInstance)" as SDC
participant "SD worker\nthread" as W
end box
participant "discovery tree\n(/tmp_discovery)" as D
APP -> IMPL : StartFindService(callback, specifier)
IMPL -> SDC : BindingSpecificStartFindService
SDC -> D : FlagFileCrawler: scan current offers\n(open/readdir)
SDC -> D : InotifyInstance: add watch on the\nservice/instance directories
IMPL --> APP : Result<FindServiceHandle>
W --> APP : callback fires NOW with current handles\n(may be an empty container)
...later, a peer offers or stops offering...
D --> W : watch event: flag file created/removed
W -> APP : callback(handles, find_handle) again
note right of W
  The callback runs on the SD worker thread.
  Everything called inside it — e.g. Proxy::Create —
  executes on this thread. That is how the production
  pattern put the subscribe on the SD thread.
end note
@enduml
```

`FindService` (one-shot) is the crawl step without the watch. `StopFindService`
removes the watch descriptors.

### 2.4 `Proxy::Create(handle)` — the heavyweight one

```plantuml
@startuml
title Proxy::Create — full chain: connect + SubscribeServiceMethod
box "Consumer (A)"
participant "app code\n(often inside the\nfind callback)" as APP
participant "impl\n(ProxyBase::Create)" as IMPL
participant "LoLa\n(lola::Proxy, message_passing_\nservice_instance:\nSubscribeServiceMethod)" as LOLA
participant "message passing\n(ClientCache,\nClientConnection)" as MP
participant "A client thread\n(QnxDispatchEngine)" as ACL
end box
participant "QNX kernel" as K
box "Provider (B)"
participant "B server\nthread" as BS
end box
APP -> IMPL : SimpleProxy::Create(handle)
IMPL -> LOLA : create proxy binding
LOLA -> K : SharedMemoryFactory::Open:\nshm_open("/lola-ctl-…", "/lola-data-…") + mmap (RO)
LOLA -> MP : SubscribeServiceMethod ->\nCallSubscribeServiceMethodRemotely
MP -> ACL : MessagePassingClientCache::CreateNewClient\n(enqueue TryConnect; poll 10 x 50 ms)
ACL -> K : TryOpenClientConnection:\nopen("/mw_com/message_passing/<B>")
note right of ACL : open() on a resmgr path = MsgSend;\nACL is SEND-blocked until B's server replies
K -> BS : _IO_CONNECT via dispatch_block
BS --> ACL : MsgReply — fd ready, connection kReady
MP -> K : SendProtocolMessage:\nwritev(fd, SubscribeServiceMethodMsg)
K -> BS : _IO_WRITE
BS --> MP : reply queued for A
MP -> MP : SendWaitReply: park caller in\nNonAllocatingFuture::Wait()
K --> ACL : select pulse (MsgRegisterEvent): input on fd
ACL -> K : ReceiveProtocolMessage: read(fd)
ACL -> MP : fulfil future
MP --> APP : Create() returns Result<Proxy>
@enduml
```

- Two separate blocking round trips (connect, then subscribe per method), both served by B's **server** thread.
- `SendWaitReply` has no timeout; only `CreateNewClient`'s 500 ms poll bounds the connect phase for the caller.
- Pre-PR-676, `ACL` and `BS` were the **same single thread** in each process. Two processes running this chain toward each other simultaneously was the deadlock (`FINDINGS.md`).

### 2.5 Method call — `proxy.add(2, 3)`

```plantuml
@startuml
title Method invocation — full chain over the established connection
box "Consumer (A)"
participant "app code" as APP
participant "impl\n(ProxyMethod\noperator())" as IMPL
participant "LoLa\n(CallMethod /\nCallMethodMsg)" as LOLA
participant "message passing\n(ClientConnection)" as MP
participant "A client\nthread" as ACL
end box
participant "QNX kernel" as K
box "Provider (B)"
participant "B server\nthread" as BS
participant "LoLa\n(MethodCallHandler)" as BH
participant "registered\nhandler" as H
end box
APP -> IMPL : proxy.add(2, 3)
IMPL -> LOLA : marshal in-args into the method's\nshm slots (binding-internal)
LOLA -> MP : CallMethod -> send CallMethodMsg
MP -> K : writev(fd, CallMethodMsg) — MsgSend
MP -> MP : SendWaitReply (wait for reply)
K -> BS : _IO_WRITE via dispatch_block
BS -> BH : dispatch to MethodCallHandler
BH -> H : run RegisterHandler lambda:\nresult = a + b (writes shm slot)
BH --> MP : reply message
K --> ACL : select pulse -> read(fd)
ACL -> MP : fulfil future
MP --> APP : Result<MethodReturnTypePtr<T>>\n(pointer into shm; slot pinned while held)
@enduml
```

Every crossed pair of these (both sides calling each other in the same instant)
was also a deadlock candidate pre-PR-676 — subscribe was just the most common
first collision.

### 2.6 Events — `Send` / `Subscribe` / `GetNewSamples`

```plantuml
@startuml
title Events — zero-copy data plane, message-passing notification
box "Provider (B)"
participant "app code" as BP
participant "LoLa\n(SkeletonEvent)" as BE
end box
participant "shared memory\nlola-ctl / lola-data" as SHM
participant "QNX kernel" as K
box "Consumer (A)"
participant "A client\nthread" as ACL
participant "LoLa\n(ProxyEvent)" as AE
participant "app / receive\nhandler" as AH
end box
AH -> BE : Subscribe(max_samples)\n(control round trip, chain as in 2.4)
BP -> BE : event.Send(value)
BE -> SHM : write sample slot (lola-data),\nbump control state (lola-ctl)
BE -> K : notify subscribers: writev on the\ncontrol connection
K --> ACL : select pulse -> read(fd)
ACL -> AH : EventReceiveHandler fires (if set)
AH -> AE : GetNewSamples(receiver, max)
AE -> SHM : read slots directly — no syscall, no copy;\nSamplePtr pins the slot
@enduml
```

The data plane never touches the kernel after setup — samples are read in place
from mapped memory. Only the *notification* is a message.

### 2.7 Teardown — proxy destruction / `StopOfferService`

```plantuml
@startuml
title Teardown — full chain
box "Consumer (A)"
participant "app code\n(proxy goes out of scope)" as APP
participant "LoLa /\nmessage passing" as MP
end box
participant "QNX kernel" as K
box "Provider (B)"
participant "B server\nthread" as BS
participant "B impl\n(ServiceDiscovery)" as BSD
end box
APP -> MP : ~Proxy()
MP -> K : writev — UnsubscribeServiceMethod\n(round trip, SendWaitReply pattern)
K -> BS : _IO_WRITE; B releases the subscription
MP -> K : close(fd)
K --> BS : CoidDeath pulse — peer connection gone
MP -> K : munmap peer segments
note over BSD : provider side: StopOfferService ->\nFlagFile removed from the discovery tree;\nwatchers get the removal event.\nresmgr_detach only at engine shutdown.
@enduml
```

## Part 3 — the QNX primitives, one by one

### 3.1 `MsgSend` / `MsgReceive` / `MsgReply` — the core IPC triangle

QNX native IPC is synchronous rendezvous messaging. A client thread `MsgSend`s
to a channel and the **kernel freezes it** until the server replies. The `pidin`
states tell you exactly where everyone is stuck — which is how we proved the
deadlock.

```plantuml
@startuml
title MsgSend / MsgReceive / MsgReply and the thread states
participant "client thread" as C
participant "kernel" as K
participant "server thread" as S
S -> K : MsgReceive(channel)
note right of S : state: RECEIVE-blocked (idle server)
C -> K : MsgSend(coid, request)
note left of C : state: SEND-blocked\n(pidin: SEND <server pid>)
K -> S : wakes server, delivers request
note left of C : state now: REPLY-blocked
S -> S : process request
S -> K : MsgReply(rcvid, answer)
K --> C : unblocks with the answer
@enduml
```

- Used by mw::com for: everything on the control plane — usually hidden under `open`/`read`/`writev` (3.4).
- Gotcha: `SEND` state means "the server hasn't even *received* my message yet". Two threads mutually SEND-blocked on each other (our `pidin` signature) means neither server loop is running — the definition of the deadlock.

### 3.2 Resource manager framework — `resmgr_attach`, `dispatch_block`, `dispatch_handler`

A resource manager is a user-space server that owns a **pathname**. QNX routes
POSIX file operations on that path to the server as messages.

```plantuml
@startuml
title Resource manager: a process that owns a path
participant "server process" as S
participant "procnto\n(path manager)" as P
participant "any client" as C
S -> P : dispatch_create_channel
S -> P : resmgr_attach("/mw_com/message_passing/<id>",\nconnect_funcs_, io_funcs_)
note right of S : the path now exists in the namespace
loop dispatch loop (one thread)
  S -> P : dispatch_block (= MsgReceive)
  C -> P : open()/read()/write() on the path
  P --> S : _IO_CONNECT / _IO_READ / _IO_WRITE message
  S -> S : dispatch_handler -> io_open / io_read / io_write callback
  S --> C : MsgReply (via the framework)
end
@enduml
```

- Used by mw::com for: each process's message-passing endpoint. `SetupResourceManagerCallbacks` fills `connect_funcs_`/`io_funcs_`; `resmgr_detach` removes the path at shutdown.
- Gotcha: with **one** dispatch thread, anything that blocks inside a callback stalls the whole resmgr. That was the pre-676 design flaw: client I/O ran inside this very loop.

### 3.3 Pulses — `pulse_attach`, `MsgSendPulse`, `message_connect`

A pulse is a tiny **asynchronous, non-blocking** message: fixed 8-bit code +
32-bit value, no reply. The sender never blocks; that's their whole point.

```plantuml
@startuml
title Pulses: async nudges into a dispatch loop
participant "sender\n(any thread / kernel)" as X
participant "dispatch thread" as D
D -> D : message_connect(MSG_FLAG_SIDE_CHANNEL)\n= a coid onto one's own channel
D -> D : pulse_attach(code, callback) — register handler
X -> D : MsgSendPulse(side_channel_coid, code, value)
note right of X : returns immediately — fire and forget
D -> D : dispatch_block wakes,\ndispatch_handler runs the pulse callback
@enduml
```

- Used by mw::com for: waking the client thread — timer expiry (`kTimerPulseCode`), fd readiness (`kSelectPulseCode`), peer death (`_PULSE_CODE_COIDDEATH`), shutdown (`kQuitPulseCode`, sent by `DispatchThreadRunner`'s destructor).
- Gotcha: pulses can't carry payloads or wait for results. They say "look", never "here's the data" — the woken thread must then `read()` or check state.

### 3.4 `open` / `read` / `writev` / `close` on a resmgr fd — MsgSend in disguise

The insight that explains the whole bug class:

```plantuml
@startuml
title Every fd operation on a resmgr path is a synchronous MsgSend
box "Process A"
participant "A client\nthread" as AC
end box
participant "kernel" as K
box "Process B"
participant "B server\nthread" as BS
end box
AC -> K : open("/mw_com/message_passing/<B>")
note right of AC : SEND-blocked until B replies
K -> BS : _IO_CONNECT
BS --> AC : MsgReply -> fd
AC -> K : writev(fd, msg)   — SEND-blocked again
K -> BS : _IO_WRITE
BS --> AC : MsgReply
AC -> K : read(fd)          — SEND-blocked again
K -> BS : _IO_READ
BS --> AC : MsgReply (with data)
AC -> K : close(fd) — detach; B gets a\nCoidDeath pulse on its channel
@enduml
```

- Used by mw::com for: the entire client side — `TryOpenClientConnection` (`open`), `SendProtocolMessage` (`writev`), `ReceiveProtocolMessage` (`read`).
- Gotcha: these look like harmless POSIX calls, but **each one hands your thread's fate to the peer's dispatch loop**. If the peer's loop never runs, you are SEND-blocked forever — there is no default timeout. This single fact, times two processes, was the deadlock.

### 3.5 Kernel timers with pulse delivery — `TimerCreate` / `TimerSettime` / `TimerDestroy`

```plantuml
@startuml
title Timer -> pulse -> dispatch loop
participant "client thread" as C
participant "kernel" as K
C -> K : TimerCreate(CLOCK_MONOTONIC,\nsigevent{SIGEV_PULSE, side_channel_coid,\nkTimerPulseCode})
C -> K : TimerSettime(next deadline)
...deadline passes...
K --> C : pulse kTimerPulseCode on the channel
C -> C : TimerPulseCallback: run due entries\nfrom the timer queue, re-arm
@enduml
```

- Used by mw::com for: the engine's timer queue — connect retry backoff (`connect_retry_ms_`), delayed commands. One kernel timer multiplexes all logical timers.
- Gotcha: delivery is a pulse into the **client thread's** dispatch loop; if that loop is blocked (pre-676), timers silently stop firing — which is why even retry/backoff machinery couldn't self-heal the wedge.

### 3.6 fd readiness events — `MsgRegisterEvent` / `MsgUnregisterEvent` / `ConnectServerInfo`

QNX's modern replacement for `ionotify`/`select` loops: ask the kernel to deliver
a chosen sigevent when an fd becomes ready.

```plantuml
@startuml
title Select emulation: readiness as a pulse
participant "A client\nthread" as AC
participant "kernel" as K
participant "B server\n(peer resmgr)" as BS
AC -> K : SIGEV_PULSE_INIT(event, side_channel_coid,\nkSelectPulseCode, packed{index,nonce,signal})
AC -> K : MsgRegisterEvent(event, fd)
...peer queues data for us...
BS -> K : marks fd readable
K --> AC : pulse kSelectPulseCode(value)
AC -> AC : SelectPulseCallback: validate index+nonce,\nendpoint->input() -> read(fd)
@enduml
```

- Used by mw::com for: knowing when a client connection has an incoming protocol message (replies, event notifications) without dedicating a thread per fd. `ConnectServerInfo` sanity-checks a coid/connection; `MsgUnregisterEvent` cleans up in `UnselectEndpoint`.
- Gotcha: the pulse says "readable", the `read()` that follows is still a blocking MsgSend (3.4) — readiness does **not** guarantee the peer's loop will serve the read promptly. The nonce in the pulse value guards against stale pulses for recycled endpoint slots.

## The confusing sentence, decoded

> "A offers its service, discovery cross-notifies both sides at roughly the same
> time — A's find-callback fires its subscribe toward B, and B fires its
> subscribe toward A."

Setup: A and B each **provide one service and consume the other's** (A offers
`instance1` and wants `instance2`; B is the mirror image). Both use
`StartFindService` with a callback that connects as soon as the peer appears.
Timeline when B starts while A is already up:

- A, earlier: `OfferService()` (instance1 is now in the discovery filesystem) and `StartFindService(cb_A, instance2)` (watcher armed, nothing found yet).
- B starts: `OfferService()` — instance2 appears in discovery.
- B: `StartFindService(cb_B, instance1)` — and because `StartFindService` fires immediately with the *current* state, and instance1 is already offered, **cb_B fires right now** with A's handle.
- A's armed watcher sees the new instance2 entry — **cb_A fires** with B's handle.
- Both callbacks do the same thing: `Proxy::Create(handle)` → connect + `SubscribeServiceMethod` → a blocking request *into the other process* (the full chain of 2.4).

That is the "cross-notification": one event (B coming up) makes **both**
processes' discovery callbacks fire within milliseconds of each other, each on
its own service-discovery worker thread, each launching a synchronous call
toward the other. Two blocking calls, in opposite directions, at nearly the same
instant.

```plantuml
@startuml
title Mutual discovery: one offer triggers both subscribes
participant "Process A" as A
participant "Discovery FS\n(/tmp_discovery)" as D
participant "Process B" as B
A -> D : OfferService (instance1)
A -> D : StartFindService(cb_A, instance2)\n(nothing yet — watcher armed)
...B starts...
B -> D : OfferService (instance2)
B -> D : StartFindService(cb_B, instance1)
D --> B : cb_B fires immediately\n(instance1 already offered)
D --> A : cb_A fires\n(instance2 just appeared)
A -> B : Proxy::Create -> SubscribeServiceMethod\n(blocking MsgSend into B)
B -> A : Proxy::Create -> SubscribeServiceMethod\n(blocking MsgSend into A)
note over A,B
  Two synchronous calls in opposite directions,
  triggered by the same discovery event.
  Normally fine — each side's server answers.
  Pre-PR-676 on QNX: if they cross within the same
  sub-ms window, the single dispatch threads block
  on each other. That is the deadlock.
end note
@enduml
```

Normal case (>99% of the time): the two round trips interleave harmlessly — B's
server answers A while B's own request is in flight, and vice versa. The
deadlock needed both processes' *single* dispatch threads to be inside their
blocking calls simultaneously, which is why it was sporadic in the field and
why this repo needed a barrier / beat-sweep to force it.

## Quick reference: the whole public surface

| Group | APIs |
|---|---|
| Runtime | `InitializeRuntime` (×2 overloads), `ResolveInstanceIDs` |
| Naming | `InstanceSpecifier`, `InstanceIdentifier`, `InstanceIdentifierContainer` |
| Interface shaping | `AsProxy<T>`, `AsSkeleton<T>`, `Method<Sig>`, `Event<T>`, `Field<T, tags…>`, `WithGetter`, `WithSetter`, `WithNotifier` |
| Skeleton | `Create`, `RegisterHandler`, `OfferService`, `StopOfferService`, `Send`, `Allocate` |
| Proxy discovery | `FindService` (×2), `StartFindService` (×2), `StopFindService`, `HandleType`, `ServiceHandleContainer`, `FindServiceHandler`, `FindServiceHandle` |
| Proxy usage | `Create`, method `operator()`, `Subscribe`, `Unsubscribe`, `GetNewSamples`, `SetReceiveHandler`, `GetSubscriptionState`, `SubscriptionState`, `SamplePtr`, `SampleAllocateePtr`, `MethodReturnTypePtr`, `MethodInArgTypePtr`, `EventReceiveHandler` |
| Type-erased | `GenericProxy`, `GenericSkeleton`, `GenericProxyEvent`, `GenericSkeletonEvent`, `DataTypeMetaInfo`, `EventInfo` |
| Errors | `score::Result<T>`, `com_error_domain.h` error codes |

## Related docs in this repo

- `analysis_simple_app1.md` — line-by-line walkthrough of a minimal app using this API.
- `FINDINGS.md` — the deadlock root cause and reproduction evidence.
- `analysis_pr676_fix.md` — how upstream fixed the threading model.
