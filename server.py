import socket
import threading
import argparse
import json
import re
import itertools

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
    return ok("history", channel=channel, messages=history)


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
def handle_tcp_client(client_socket, addr):
    session = {"username": None}
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
                except OSError:
                    # Client went away mid-response; stop handling it.
                    break

                if should_close:
                    break
    except Exception as exc:
        print(f"[TCP] Error handling client {addr}: {exc}")
    finally:
        print(f"[TCP] Connection closed {addr}")


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
# HTTP server (kept minimal / functional -- not part of the spec given here)
# ---------------------------------------------------------------------------
def handle_http_client(client_socket, addr):
    try:
        with client_socket:
            request = client_socket.recv(4096).decode("utf-8", errors="replace")
            if not request:
                return
            request_line = request.splitlines()[0]
            parts = request_line.split()
            method, path = (parts[0], parts[1]) if len(parts) >= 2 else ("", "/")

            body = json.dumps({"status": "ok", "message": f"{method} {path} received"})
            response = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n"
                "\r\n"
                f"{body}"
            )
            client_socket.sendall(response.encode("utf-8"))
    except Exception as exc:
        print(f"[HTTP] Error handling client {addr}: {exc}")


def http_server(host, port):
    http_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    http_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    http_socket.bind((host, port))
    http_socket.listen()
    print(f"HTTP server listening on {host}:{port}")

    while True:
        client_socket, addr = http_socket.accept()
        print(f"[HTTP] Connection from {addr}")
        threading.Thread(
            target=handle_http_client, args=(client_socket, addr), daemon=True
        ).start()


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