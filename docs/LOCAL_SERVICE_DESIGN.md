# The local service

- **Status**: 2.7.0a1. The stdio entry relays to the service by default;
  `CPERSONA_LOCAL_SERVICE=off` keeps the old in-process server until 2.7.0b1,
  which removes it.
- **Scope**: how a stdio launch finds or starts the one server for its store, what
  that server accepts, and when it stops. The versioned `/api/v1` the dashboard
  and command line will share, and the browser sign-in, are later steps of 2.7
  ([below](#what-is-not-here-yet)).
- **Code**: `cpersona/service.py` (the relay side), `cpersona/entry.py` (the
  command), the `local-service` transport in `cpersona/server.py`.

## Why

Up to 2.6 every MCP client that launched `cpersona` got a process of its own that
opened the database and served that client. Three clients open meant three
processes writing one SQLite file, three copies of every process-wide state (the
embedding cache, the calibration, the no-persist pause), and a dashboard that read
the file directly beside them. Two processing paths, stdio in-process and the HTTP
server, carried the same tools.

From 2.7 there is one server per store on the person's machine. A stdio launch is a
relay to it, so the clients of a store share one process and one writer, and the
HTTP transport is the only processing path.

The client's configuration does not change: the line that launched `cpersona`
before launches the relay now.

## What a stdio launch does

1. Work out the store: the absolute path of `CPERSONA_DB_PATH` (or of the relative
   default, `data/cpersona.db`, against the client's working directory).
2. Under a lock for that store, read the service record. If a service answers on
   the recorded port as this store's service, check its settings
   ([next section](#which-service-a-client-uses)) and use it.
3. Otherwise start one, detached from the client
   ([how](#starting-a-service-that-outlives-its-client)), wait until it answers,
   and record it.
4. Forward the client's messages to it over loopback HTTP, with the existing stdio
   proxy (`cpersona/proxy_stdio.py`), adding the service's token and a name for
   this connection.

The relay does not import the server: the command decides what to run from its
arguments and environment before anything heavy is loaded, so relaying costs a
small fraction of the server's own start.

## Which service a client uses

A service is keyed by the **store** and the **version of this package**. Two
versions keep separate services, as two versions of the stdio server kept
separate processes. The older one stops once it has been idle for the idle period.

The client's **settings** travel with it. Every `CPERSONA_*` and `EMBEDDING_*`
variable is part of the store's configuration except the few that describe one
connection: `CPERSONA_TRANSPORT`, `CPERSONA_REMOTE_URL`, `CPERSONA_AUTH_TOKEN`,
`CPERSONA_PROXY_MAX_INFLIGHT`, `CPERSONA_HTTP_HOST`, `CPERSONA_HTTP_PORT`,
`CPERSONA_LOCAL_SERVICE`, `CPERSONA_SERVICE_DIR`, and the variables the relay
itself passes to the service. A path setting (a name ending in `_PATH`, `_FILE`
or `_DIR`) is compared as the absolute file it names.

A client whose settings differ from those of the running service is **refused**,
and every request it sends is answered with an error naming the settings that
differ, never their values. Two other answers were possible, and both are worse:

- serving it from a service configured differently loses a setting the client asked
  for, silently;
- starting a second service on the same store brings back the second writer the
  service exists to remove.

The comparison uses one keyed hash per setting, with a random key the service is
started with, so the service's answer about itself carries no setting's value,
including secrets such as an embedding API key.

## What the service accepts

The service runs the HTTP transport with the name `local-service`, which the
relay sets. It differs from a deployment of the HTTP transport in what it lets in:

- It binds `127.0.0.1` only.
- Every request carries the service's **token**. The relay reads it from the
  service record, which only the owner can read. The token is new for every
  service started.
- The **Host** header must name the loopback address and the service's port. A
  web page that points a name of its own at `127.0.0.1` sends that name, and is
  refused.
- No CORS, no OAuth discovery, and no catch-all route: `/mcp/`,
  `/api/v1/instance` and `/api/v1/stop` are all there is, and any other path is
  404.
- With an ACL file, every request is the reserved `local` principal, as the
  stdio process was ([ACL design §5.4](ACL_DESIGN.md)).
- It serves the person on its own machine, so it behaves as the stdio server did
  where the two transports differ: `open_dashboard` opens a page on this machine.

`/api/v1/instance` answers what the service is (the package version, its key,
store, process id, the settings hashes and the idle period), and `503` once it is
stopping, so a relay does not hand a client to a service that is about to close.
`/api/v1/stop` asks it to stop. The versioned API grows from these two in 2.7.0a2.

## Callers that declare no session key

A caller that sends no `session_key` used to land in the stdio process's one
bucket, which was that client's own. In a service shared by several clients that
bucket would be shared too: a keyless `pause_persistence` from one client would
silently skip the writes of another.

So each relay names its connection (a random id per launch, sent in the
`X-CPersona-Connection` header), and the service makes that name the bucket of a
keyless caller ([session identity](SESSION_IDENTITY_DESIGN.md)). The bucket is
still undeclared: responses read as they did under stdio (`scope: "process"`), the
degraded-recall runbook is given once per connection, and the `identity_shared`
flag stays off. A declared key works as before. The remote HTTP transport ignores
the header.

## Lifetime

- **Idle stop**: the service stops when nothing has used it for
  `CPERSONA_SERVICE_IDLE_MINUTES` (30 by default; `0` keeps it running). A
  request in progress is use, and so is a connected client: each relay tells the
  service it is still there every five minutes, so a client that is open but quiet
  does not lose the service, and with it a pause it set.
- **Explicit stop**: `cpersona service stop [KEY]`; `cpersona service list` shows
  the recorded services without their tokens.
- **Restart**: when a relay finds nothing listening (the service stopped while
  idle, crashed, or the machine slept through its idle period), it finds or starts
  a service once and sends the message again; nothing had been sent, so nothing
  runs twice. If that fails too, the client gets an error naming the service's log.
  The relay never falls back to serving the client in-process, which would bring
  the second path back.
- **Startup failure**: a service that exits before it answers, or does not answer
  within 60 seconds, is reported with the last lines of its log.

## Starting a service that outlives its client

The service has to survive the client that started it: closing one editor must not
stop the memory of another. What that takes was measured on each platform before
the design was fixed.

- **macOS and Linux**: the service is started in a new session. It survives the
  client's process group being killed, and an ssh connection that launched it over
  stdio being closed.
- **Windows**: the service is started detached, in a process group of its own,
  outside the job the client runs in, and through an intermediate process that
  exits at once. Each part is needed. A detached process alone ended with the
  session that started it; breaking away from the job kept it alive across the
  session but not when the client ended its process tree, which follows the
  parent link that the intermediate process removes. The first start of the
  server is slower on Windows (the import), and later connections are not.
- **Sandboxed clients**: a client that runs its tools in a read-only sandbox still
  reached a service on loopback, and the service outlived the client.

## Files

`~/.cpersona/services/` (`CPERSONA_SERVICE_DIR` moves it), owner-only:

| File | What it holds |
|---|---|
| `<key>.json` | port, process id, version, start time, the store, the token and the hash key |
| `<key>.lock` | held while a relay finds or starts the service |
| `<key>.log` | the service's output; started afresh when it passes 5 MB at a start |

The key is a hash of the store path and the version.

## What is not here yet

- **2.7.0a2**: the versioned `/api/v1` contract, and the dashboard as a client of it
  instead of reading the database file.
- **2.7.0a3**: one browser sign-in for the local service and remote servers.
- **2.7.0b1**: the in-process stdio server and `CPERSONA_LOCAL_SERVICE` are removed,
  with `open_dashboard` and the dashboard's own server and sign-in.
- Not planned for 2.7: registering the service with the operating system's service
  manager. It starts when a client needs it.

## Invariants

1. One service per store and version: a second client of the same store and
   settings joins the running service (`tests/test_local_service.py`, the test that
   starts real services).
2. A client with other settings is refused with the names of the differing settings
   and no values.
3. No request reaches the MCP endpoint without the token and a loopback Host.
4. A keyless caller's in-process state is its connection's: one connection's pause
   does not skip another's writes.
5. The service stops only when idle (no request in progress, no recent request or
   heartbeat), or when asked.
6. A relay retries a message only when nothing was sent, and only once.
