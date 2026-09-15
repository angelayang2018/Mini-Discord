Author: Angela Yang 

# Mini Discord Server

A minimal Discord-style chat backend exposing the same shared state (users,
channels, messages) through two interfaces: a line-oriented TCP
protocol and a JSON REST API over HTTP. 

## Design Description

The server keeps all state in a small set of shared, lock-protected
in-memory structures:

- `users` — a set of registered usernames
- `channels` — a dict of channel name → set of member usernames
- `messages` — a dict of channel name → list of message records
- a single monotonically-increasing message-ID counter

Both the TCP server and the HTTP server run as two threads inside the same
process and read/write these same structures under one shared
`threading.RLock`.

Each TCP client connection runs in its own daemon thread and keeps its own
per-connection session (`{"username": ...}`), so one client's login state
never leaks into another's. The HTTP server is a
`http.server.ThreadingHTTPServer`, so each HTTP request is also handled
concurrently; since HTTP is stateless here, the caller identifies itself
per-request via an `X-User` header rather than a login session.

A background accept loop detects TCP client failure: whenever a
connection's read loop ends — whether because the client sent `QUIT` or
because the socket was dropped/killed/closed unexpectedly — a cleanup
routine logs that session out and removes the user from every channel's
membership set. Because that's the same `channels` structure the HTTP
`GET /channels/{channel}/users` endpoint reads, the cleanup is visible
over HTTP immediately.

## Dependencies

None beyond the Python standard library. No `pip install` is required.

- Python 3.8 or newer
- Standard library modules used: `socket`, `threading`, `argparse`, `json`,
  `re`, `itertools`, `http.server`, `urllib.parse`, `datetime`


## Startup Instructions


1. Start the Server
```
./run.sh --host 127.0.0.1 --tcp-port 9000 --http-port 8080
```

This starts both interfaces at once:
- TCP protocol on `127.0.0.1:9000`
- HTTP API on `127.0.0.1:8080`


2. Connect client


a. In a separate terminal, connect with the interactive TCP client:
```
python client.py --host 127.0.0.1 --port 9000
```

b. Or talk to the HTTP API directly, e.g.:

```
curl http://127.0.0.1:8080/health
curl -X POST http://127.0.0.1:8080/users -d '{"username":"alice"}'
curl http://127.0.0.1:8080/channels/%23cs249/users
```


## Protocol Decisions

**TCP interface**
- One request per line, UTF-8, newline-terminated; one JSON response per
  line back. The connection stays open across multiple commands and is
  only closed by `QUIT` or a client disconnect.
- Login is a per-connection session, not a global/shared login — logging
  in on one connection has no effect on any other connection.
- `JOIN <channel>` creates the channel if it doesn't already exist yet
  (channels aren't a separate resource you must create first).
- `SEND`, `LEAVE`, `JOIN`, and `LOGOUT` all require the connection to be
  logged in first (`NOT_AUTHENTICATED` otherwise); `SEND` additionally
  requires the sender to already be a member of the channel
  (`NOT_FOUND` otherwise).
- Usernames and channel names are validated against a fixed pattern
  (`[A-Za-z0-9_]{1,32}` for usernames; channels must additionally start
  with `#`); anything else is rejected as `INVALID_USERNAME` /
  `INVALID_CHANNEL` rather than silently accepted.
- Any unexpected exception while handling a command is caught and turned
  into a `SERVER_ERROR` response rather than crashing the connection or
  the server.

**HTTP interface**
- Stateless by request: the caller's identity is passed via the `X-User`
  header on each request rather than a server-side login session, since
  HTTP has no persistent connection to hang a session off of.
- Channel names containing `#` must be percent-encoded in the URL path
  (`#cs249` → `%23cs249`), since `#` is a URL fragment delimiter.
- `POST /channels/{channel}/members` (join) also creates the channel if
  needed, mirroring the TCP interface's `JOIN` behavior, for consistency
  between the two protocols.
- Sending a message to a channel you haven't joined returns `403
  FORBIDDEN` (distinct from `404 NOT_FOUND`, which is reserved for
  genuinely unknown users/channels).
- An unrecognized path returns `404 NOT_FOUND`; a recognized path with an
  unsupported HTTP method returns `405 METHOD_NOT_ALLOWED`. Both codes
  fall outside the TCP error-code list since the HTTP spec only required
  "an appropriate 4xx status code," not a fixed enum.
- Every stored message carries a `timestamp` (ISO 8601, UTC) in addition
  to the required fields; this is included in HTTP history but per the
  spec isn't graded.

**Shared behavior**
- Message IDs are assigned from one global counter shared by both
  interfaces, so IDs stay unique and increasing regardless of which
  protocol a message came in through.
- TCP client failure (dropped connection without `QUIT`) and TCP client
  `QUIT` both run the same cleanup path (logout + remove from all
  channels), so channel membership never accumulates stale entries from
  disconnected sessions.



| Requirement               | Video Timestamp |
| ------------------------- | --------------- |
| Server startup            |                 |
| Health endpoint           |                 |
| User creation             |                 |
| Channel membership        |                 |
| Message creation          |                 |
| Message history           |                 |
| TCP interface             |                 |
| Shared TCP and HTTP state |                 |
| Concurrent clients        |                 |
| Failure detection         |                 |