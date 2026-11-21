"""Plain TCP socket server that allows at most MAX_CLIENTS concurrent clients.

Extra clients get a "server full" message and are disconnected.
Each client is echoed its lines back; send "quit" to disconnect.
"""
import socket
import threading

HOST, PORT = "127.0.0.1", 9000
MAX_CLIENTS = 5

slots = threading.BoundedSemaphore(MAX_CLIENTS)


def handle(conn: socket.socket, addr) -> None:
    try:
        conn.sendall(b"Welcome. Type 'quit' to leave.\n")
        with conn.makefile("rwb") as f:
            for line in f:
                text = line.strip()
                if text.lower() == b"quit":
                    f.write(b"Bye.\n")
                    f.flush()
                    break
                f.write(b"echo: " + text + b"\n")
                f.flush()
    except OSError:
        pass
    finally:
        conn.close()
        slots.release()
        print(f"[-] {addr} disconnected")


def main() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((HOST, PORT))
        srv.listen()
        print(f"Listening on {HOST}:{PORT} (max {MAX_CLIENTS} clients)", flush=True)
        while True:
            conn, addr = srv.accept()
            if slots.acquire(blocking=False):
                print(f"[+] {addr} connected")
                threading.Thread(target=handle, args=(conn, addr), daemon=True).start()
            else:
                print(f"[!] {addr} rejected (server full)")
                try:
                    conn.sendall(b"Server full. Try again later.\n")
                finally:
                    conn.close()


if __name__ == "__main__":
    main()
