import socket
import threading
import argparse
import json
import re
import itertools
import http.server
from datetime import datetime, timezone
from urllib.parse import urlsplit, parse_qs, unquote

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
USERNAME_RE = re.compile(r'^[A-Za-z0-9_]{1,32}$')
CHANNEL_RE = re.compile(r'^#[A-Za-z0-9_]{1,32}$')

# ---------------------------------------------------------------------------
# Shared state (protected by state_lock -- multiple client threads touch this)
# ---------------------------------------------------------------------------
state_lock = threading.RLock()
users = set()                 # registered usernames
channels = {}                 # channel_name -> set(usernames currently joined)
messages = {}                 # channel_name -> list of {"message_id","username","text"}
_message_id_counter = itertools.count(1)


def next_message_id():
    return next(_message_id_counter)


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------
def ok(operation, **kwargs):
    resp = {"status": "ok", "operation": operation}
    resp.update(kwargs)
    return resp


def error(code, message):
    return {"status": "error", "code": code, "message": message}


# ---------------------------------------------------------------------------
# Command handlers -- each returns a plain dict (the JSON response body)
# ---------------------------------------------------------------------------
def handle_register(username):
    if not USERNAME_RE.match(username):
        return error("INVALID_USERNAME", f"'{username}' is not a valid username")
    with state_lock:
        if username in users:
            return error("CONFLICT", f"User '{username}' already exists")
        users.add(username)
    return ok("register", username=username)


def handle_login(username, session):
    if not USERNAME_RE.match(username):
        return error("INVALID_USERNAME", f"'{username}' is not a valid username")
    with state_lock:
        if username not in users:
            return error("NOT_FOUND", f"User '{username}' not found")
    session["username"] = username
    return ok("login", username=username)


def handle_join(channel, session):
    if session["username"] is None:
        return error("NOT_AUTHENTICATED", "You must log in before joining a channel")
    if not CHANNEL_RE.match(channel):
        return error("INVALID_CHANNEL", f"'{channel}' is not a valid channel name")
    with state_lock:
        # JOIN creates the channel on the fly if it doesn't exist yet.
        channels.setdefault(channel, set()).add(session["username"])
    return ok("join", channel=channel, username=session["username"])


def handle_leave(channel, session):
    if session["username"] is None:
        return error("NOT_AUTHENTICATED", "You must log in before leaving a channel")
    if not CHANNEL_RE.match(channel):
        return error("INVALID_CHANNEL", f"'{channel}' is not a valid channel name")
    with state_lock:
        members = channels.get(channel)
        if not members or session["username"] not in members:
            return error("NOT_FOUND", f"You are not in channel '{channel}'")
        members.remove(session["username"])
    return ok("leave", channel=channel, username=session["username"])


def handle_send(channel, text, session):
    if session["username"] is None:
        return error("NOT_AUTHENTICATED", "You must log in before sending messages")
    if not CHANNEL_RE.match(channel):
        return error("INVALID_CHANNEL", f"'{channel}' is not a valid channel name")
    if not text:
        return error("BAD_REQUEST", "Message text must not be empty")
    with state_lock:
        members = channels.get(channel)
        if not members or session["username"] not in members:
            return error("NOT_FOUND", f"You must join '{channel}' before sending messages")
        msg_id = next_message_id()
        messages.setdefault(channel, []).append({
            "message_id": msg_id,
            "username": session["username"],
            "text": text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    return ok("send", message_id=msg_id)


def handle_list_channels():
    with state_lock:
        names = sorted(channels.keys())
    return ok("list_channels", channels=names)


def handle_list_users(channel):
    if not CHANNEL_RE.match(channel):
        return error("INVALID_CHANNEL", f"'{channel}' is not a valid channel name")
    with state_lock:
        if channel not in channels:
            return error("NOT_FOUND", f"Channel '{channel}' does not exist")
        members = sorted(channels[channel])
    return ok("list_users", channel=channel, users=members)


def handle_history(channel, limit):
    if not CHANNEL_RE.match(channel):
        return error("INVALID_CHANNEL", f"'{channel}' is not a valid channel name")
    with state_lock:
        if channel not in channels:
            return error("NOT_FOUND", f"Channel '{channel}' does not exist")
        history = list(messages.get(channel, []))
    if limit is not None:
        history = history[-limit:]
    # Only the fields required by the TCP protocol -- storage may carry
    # extra bookkeeping fields (e.g. timestamp) that aren't part of the spec.
    trimmed = [
        {"message_id": m["message_id"], "username": m["username"], "text": m["text"]}
        for m in history
    ]
    return ok("history", channel=channel, messages=trimmed)


def handle_logout(session):
    if session["username"] is None:
        return error("NOT_AUTHENTICATED", "You are not logged in")
    username = session["username"]
    session["username"] = None
    return ok("logout", username=username)


# ---------------------------------------------------------------------------
# Command parsing / dispatch
# ---------------------------------------------------------------------------
def process_command(line, session):
    """Parse one line of input and return (response_dict, should_close_connection)."""
    parts = line.split()
    if not parts:
        return error("BAD_REQUEST", "Empty command"), False

    cmd = parts[0].upper()

    if cmd == "REGISTER":
        if len(parts) != 2:
            return error("BAD_REQUEST", "Usage: REGISTER <username>"), False
        return handle_register(parts[1]), False

    if cmd == "LOGIN":
        if len(parts) != 2:
            return error("BAD_REQUEST", "Usage: LOGIN <username>"), False
        return handle_login(parts[1], session), False

    if cmd == "JOIN":
        if len(parts) != 2:
            return error("BAD_REQUEST", "Usage: JOIN <channel>"), False
        return handle_join(parts[1], session), False

    if cmd == "LEAVE":
        if len(parts) != 2:
            return error("BAD_REQUEST", "Usage: LEAVE <channel>"), False
        return handle_leave(parts[1], session), False

    if cmd == "SEND":
        if len(parts) < 3:
            return error("BAD_REQUEST", "Usage: SEND <channel> <text>"), False
        channel = parts[1]
        # Preserve the original spacing of the message text.
        text = line.split(None, 2)[2]
        return handle_send(channel, text, session), False

    if cmd == "LIST":
        if len(parts) < 2:
            return error("BAD_REQUEST", "Usage: LIST CHANNELS | LIST USERS <channel>"), False
        sub = parts[1].upper()
        if sub == "CHANNELS":
            if len(parts) != 2:
                return error("BAD_REQUEST", "Usage: LIST CHANNELS"), False
            return handle_list_channels(), False
        if sub == "USERS":
            if len(parts) != 3:
                return error("BAD_REQUEST", "Usage: LIST USERS <channel>"), False
            return handle_list_users(parts[2]), False
        return error("BAD_REQUEST", f"Unknown LIST subcommand: {parts[1]}"), False

    if cmd == "HISTORY":
        if len(parts) < 2 or len(parts) > 3:
            return error("BAD_REQUEST", "Usage: HISTORY <channel> [limit]"), False
        channel = parts[1]
        limit = None
        if len(parts) == 3:
            try:
                limit = int(parts[2])
                if limit < 0:
                    raise ValueError
            except ValueError:
                return error("BAD_REQUEST", "limit must be a non-negative integer"), False
        return handle_history(channel, limit), False

    if cmd == "LOGOUT":
        return handle_logout(session), False

    if cmd == "QUIT":
        return ok("quit"), True

    return error("BAD_REQUEST", f"Unknown command: {parts[0]}"), False


# ---------------------------------------------------------------------------
# TCP server
# ---------------------------------------------------------------------------
def cleanup_session(session):
    """Log out the session's user (if any) and remove them from every
    channel they were a member of. Called whenever a TCP connection ends --
    whether the client sent QUIT or simply vanished -- so channel
    membership never holds onto a disconnected user. Returns the list of
    channels the user was removed from, for logging.
    """
    username = session["username"]
    if username is None:
        return []

    left_channels = []
    with state_lock:
        for channel, members in channels.items():
            if username in members:
                members.discard(username)
                left_channels.append(channel)
        session["username"] = None

    return left_channels


def handle_tcp_client(client_socket, addr):
    session = {"username": None}
    quit_requested = False
    disconnect_reason = None

    try:
        # makefile() gives us convenient line-based reading over the socket
        # while still letting us write raw bytes back with sendall().
        reader = client_socket.makefile("r", encoding="utf-8", newline="\n")
        with client_socket, reader:
            for raw_line in reader:
                line = raw_line.rstrip("\r\n")
                if line == "":
                    # Ignore blank lines rather than treating them as errors
                    # or killing the connection.
                    continue

                try:
                    response, should_close = process_command(line, session)
                except Exception as exc:
                    # Any unexpected failure becomes SERVER_ERROR; the
                    # connection and server both stay alive.
                    response = error("SERVER_ERROR", f"Internal error: {exc}")
                    should_close = False

                try:
                    client_socket.sendall((json.dumps(response) + "\n").encode("utf-8"))
                except OSError as exc:
                    # Client went away mid-response -- this is a failed
                    # client, not a graceful QUIT.
                    disconnect_reason = f"write failed ({exc})"
                    break

                if should_close:
                    quit_requested = True
                    break
            # If the for-loop above ends without break, the client closed
            # its socket (or the process died / terminal closed) without
            # ever sending QUIT -- an unexpectedly failed client.
    except (ConnectionResetError, BrokenPipeError, TimeoutError) as exc:
        disconnect_reason = str(exc)
    except OSError as exc:
        disconnect_reason = str(exc)
    except Exception as exc:
        print(f"[TCP] Error handling client {addr}: {exc}")
        disconnect_reason = str(exc)
    finally:
        username = session["username"]
        left_channels = cleanup_session(session)

        if quit_requested:
            detail = f"user '{username}' logged out" if username else "no active session"
            print(f"[TCP] Connection closed by QUIT {addr} -- {detail}")
        else:
            # This is the failure-detection path: the client disconnected
            # without sending QUIT (closed process, dropped connection,
            # closed terminal, etc.).
            detail_parts = []
            if username:
                detail_parts.append(f"logged out '{username}'")
            if left_channels:
                detail_parts.append(f"removed from channels: {left_channels}")
            detail = "; ".join(detail_parts) if detail_parts else "no active session"
            reason = f" ({disconnect_reason})" if disconnect_reason else ""
            print(f"[TCP] Detected failed/disconnected client {addr}{reason} -- {detail}")


def tcp_server(host, port):
    tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tcp_socket.bind((host, port))
    tcp_socket.listen()
    print(f"TCP server listening on {host}:{port}")

    while True:
        client_socket, addr = tcp_socket.accept()
        print(f"[TCP] Connection from {addr}")
        threading.Thread(
            target=handle_tcp_client, args=(client_socket, addr), daemon=True
        ).start()


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
def http_error(code, message):
    return {"status": "error", "code": code, "message": message}


def _read_json_body(handler):
    """Read and parse a JSON object from the request body.

    Returns (body_dict, error_body). On success error_body is None. On
    failure body_dict is None and error_body is a ready-to-send error dict.
    """
    try:
        length = int(handler.headers.get("Content-Length", 0) or 0)
    except ValueError:
        length = 0

    if length == 0:
        return {}, None

    raw = handler.rfile.read(length)
    if not raw.strip():
        return {}, None

    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, http_error("BAD_REQUEST", "Invalid JSON in request body")

    if not isinstance(data, dict):
        return None, http_error("BAD_REQUEST", "Request body must be a JSON object")

    return data, None


def http_create_user(handler, groups, query):
    body, err_body = _read_json_body(handler)
    if err_body is not None:
        return 400, err_body

    username = body.get("username")
    if not isinstance(username, str) or not username:
        return 400, http_error("BAD_REQUEST", "Missing required field: username")
    if not USERNAME_RE.match(username):
        return 400, http_error("INVALID_USERNAME", f"'{username}' is not a valid username")

    with state_lock:
        if username in users:
            return 409, http_error("CONFLICT", f"User '{username}' already exists")
        users.add(username)

    return 201, {"username": username}


def http_list_users(handler, groups, query):
    with state_lock:
        names = sorted(users)
    return 200, {"users": names}


def http_list_channels(handler, groups, query):
    with state_lock:
        names = sorted(channels.keys())
    return 200, {"channels": names}


def http_join_channel(handler, groups, query):
    channel = unquote(groups[0])
    username = handler.headers.get("X-User")

    if not username:
        return 400, http_error("BAD_REQUEST", "Missing X-User header")
    if not CHANNEL_RE.match(channel):
        return 400, http_error("INVALID_CHANNEL", f"'{channel}' is not a valid channel name")
    if not USERNAME_RE.match(username):
        return 400, http_error("INVALID_USERNAME", f"'{username}' is not a valid username")

    with state_lock:
        if username not in users:
            return 404, http_error("NOT_FOUND", f"User '{username}' not found")
        # Joining creates the channel on the fly, same as the TCP interface.
        channels.setdefault(channel, set()).add(username)

    return 200, {"channel": channel, "username": username, "joined": True}


def http_list_channel_members(handler, groups, query):
    channel = unquote(groups[0])
    if not CHANNEL_RE.match(channel):
        return 400, http_error("INVALID_CHANNEL", f"'{channel}' is not a valid channel name")

    with state_lock:
        if channel not in channels:
            return 404, http_error("NOT_FOUND", f"Channel '{channel}' does not exist")
        members = sorted(channels[channel])

    return 200, {"channel": channel, "users": members}


def http_leave_channel(handler, groups, query):
    channel = unquote(groups[0])
    username = unquote(groups[1])

    if not CHANNEL_RE.match(channel):
        return 400, http_error("INVALID_CHANNEL", f"'{channel}' is not a valid channel name")
    if not USERNAME_RE.match(username):
        return 400, http_error("INVALID_USERNAME", f"'{username}' is not a valid username")

    with state_lock:
        members = channels.get(channel)
        if members is None:
            return 404, http_error("NOT_FOUND", f"Channel '{channel}' does not exist")
        if username not in members:
            return 404, http_error("NOT_FOUND", f"User '{username}' is not in channel '{channel}'")
        members.remove(username)

    return 200, {"channel": channel, "username": username, "left": True}


def http_send_message(handler, groups, query):
    channel = unquote(groups[0])
    username = handler.headers.get("X-User")

    if not CHANNEL_RE.match(channel):
        return 400, http_error("INVALID_CHANNEL", f"'{channel}' is not a valid channel name")
    if not username:
        return 400, http_error("BAD_REQUEST", "Missing X-User header")

    body, err_body = _read_json_body(handler)
    if err_body is not None:
        return 400, err_body

    text = body.get("text")
    if not isinstance(text, str) or not text:
        return 400, http_error("BAD_REQUEST", "Missing required field: text")

    with state_lock:
        if username not in users:
            return 404, http_error("NOT_FOUND", f"User '{username}' not found")
        members = channels.get(channel)
        if not members or username not in members:
            return 403, http_error(
                "FORBIDDEN", f"User '{username}' must join '{channel}' before sending messages"
            )
        msg_id = next_message_id()
        messages.setdefault(channel, []).append({
            "message_id": msg_id,
            "username": username,
            "text": text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    return 201, {"message_id": msg_id, "channel": channel, "username": username, "text": text}


def http_get_history(handler, groups, query):
    channel = unquote(groups[0])
    if not CHANNEL_RE.match(channel):
        return 400, http_error("INVALID_CHANNEL", f"'{channel}' is not a valid channel name")

    limit = None
    if "limit" in query:
        try:
            limit = int(query["limit"][0])
            if limit < 0:
                raise ValueError
        except (ValueError, IndexError):
            return 400, http_error("BAD_REQUEST", "limit must be a non-negative integer")

    with state_lock:
        if channel not in channels:
            return 404, http_error("NOT_FOUND", f"Channel '{channel}' does not exist")
        history = list(messages.get(channel, []))

    if limit is not None:
        history = history[-limit:]

    return 200, {
        "channel": channel,
        "messages": [
            {
                "message_id": m["message_id"],
                "username": m["username"],
                "text": m["text"],
                "timestamp": m.get("timestamp"),
            }
            for m in history
        ],
    }


def http_health(handler, groups, query):
    return 200, {"status": "ok"}


# Route table: (compiled path pattern, {HTTP_METHOD: handler_function})
# Handlers receive (request_handler, regex_groups, parsed_query_dict) and
# return (http_status_code, response_body_dict).
HTTP_ROUTES = [
    (re.compile(r'^/health$'), {"GET": http_health}),
    (re.compile(r'^/users$'), {"GET": http_list_users, "POST": http_create_user}),
    (re.compile(r'^/channels$'), {"GET": http_list_channels}),
    (re.compile(r'^/channels/([^/]+)/members$'), {"POST": http_join_channel}),
    (re.compile(r'^/channels/([^/]+)/users$'), {"GET": http_list_channel_members}),
    (re.compile(r'^/channels/([^/]+)/members/([^/]+)$'), {"DELETE": http_leave_channel}),
    (re.compile(r'^/channels/([^/]+)/messages$'), {"POST": http_send_message, "GET": http_get_history}),
]


class DiscordHTTPRequestHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MiniDiscordHTTP/1.0"

    def _send_json(self, status, body):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _dispatch(self):
        url = urlsplit(self.path)
        path = url.path
        query = parse_qs(url.query)

        for pattern, methods in HTTP_ROUTES:
            match = pattern.match(path)
            if not match:
                continue

            handler_fn = methods.get(self.command)
            if handler_fn is None:
                self._send_json(
                    405,
                    http_error(
                        "METHOD_NOT_ALLOWED",
                        f"{self.command} is not supported on {path}",
                    ),
                )
                return

            try:
                status, body = handler_fn(self, match.groups(), query)
            except Exception as exc:
                status, body = 500, http_error("SERVER_ERROR", f"Internal error: {exc}")

            self._send_json(status, body)
            return

        # No route matched at all.
        self._send_json(404, http_error("NOT_FOUND", f"Unknown path: {path}"))

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def do_PATCH(self):
        self._dispatch()

    def log_message(self, fmt, *fmt_args):
        print(f"[HTTP] {self.address_string()} - {fmt % fmt_args}")


def http_server(host, port):
    server = http.server.ThreadingHTTPServer((host, port), DiscordHTTPRequestHandler)
    print(f"HTTP server listening on {host}:{port}")
    server.serve_forever()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Mini discord server that handles both TCP and HTTP connections."
    )
    parser.add_argument("--host", required=True)
    parser.add_argument("--tcp-port", type=int, required=True)
    parser.add_argument("--http-port", type=int, required=True)
    args = parser.parse_args()

    tcp_thread = threading.Thread(target=tcp_server, args=(args.host, args.tcp_port), daemon=True)
    http_thread = threading.Thread(target=http_server, args=(args.host, args.http_port), daemon=True)
    tcp_thread.start()
    http_thread.start()

    tcp_thread.join()
    http_thread.join()


if __name__ == "__main__":
    main()