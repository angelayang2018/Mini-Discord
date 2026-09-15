import socket
import sys
import json
import argparse


def main():
    parser = argparse.ArgumentParser(description="TCP client for the mini discord server.")
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=9000)
    args = parser.parse_args()

    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    try:
        print(f"Connecting to {args.host}:{args.port}...")
        client_socket.connect((args.host, args.port))
        print("Connected successfully! Type your commands below (Type 'exit' to quit).")

        # A line-oriented protocol needs line-oriented I/O: recv(1024) can
        # return a partial line, multiple lines, or nothing at all if the
        # response hasn't fully arrived yet. makefile() buffers for us and
        # readline() blocks until it sees a full line.
        reader = client_socket.makefile('r', encoding='utf-8', newline='\n')

        while True:
            try:
                message = input("You: ")
            except EOFError:
                # stdin closed (e.g. piped input ran out)
                break

            if not message:
                continue

            if message.lower() == 'exit':
                print("Closing connection...")
                try:
                    client_socket.sendall(("QUIT\n").encode('utf-8'))
                    reader.readline()  # drain the server's QUIT response, if any
                except OSError:
                    pass
                break

            try:
                # The server expects one command per line, terminated by '\n'.
                client_socket.sendall((message + "\n").encode('utf-8'))
            except (BrokenPipeError, ConnectionResetError):
                print("Server closed the connection.")
                break

            line = reader.readline()
            if not line:
                print("Server closed the connection.")
                break

            line = line.strip()
            try:
                parsed = json.loads(line)
                print(f"Server: {json.dumps(parsed, indent=2)}")
            except json.JSONDecodeError:
                # Fall back to raw output if the server ever sends non-JSON.
                print(f"Server: {line}")

    except ConnectionRefusedError:
        print(f"Error: Could not connect to the server at {args.host}:{args.port}.")
        print("Is your server script running?")
    except KeyboardInterrupt:
        print("\nDisconnecting...")
    finally:
        client_socket.close()
        print("Socket closed.")


if __name__ == "__main__":
    main()