"""Unix socket relay: /tmp/mhde-relay/streamlit.sock → Streamlit 127.0.0.1:8501.

Same pattern as mhde_bridge_relay.py but for the Streamlit dashboard.
"""
import os
import socket
import threading
import logging
import sys

SOCKET_PATH = "/tmp/mhde-relay/streamlit.sock"
TARGET_HOST = "127.0.0.1"
TARGET_PORT = 8501
BUFSIZE = 65536

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s mhde-streamlit-relay %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("mhde-streamlit-relay")


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(BUFSIZE)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass


def _handle(client: socket.socket) -> None:
    try:
        target = socket.create_connection((TARGET_HOST, TARGET_PORT), timeout=10)
    except OSError as e:
        log.warning("Cannot reach %s:%s — %s", TARGET_HOST, TARGET_PORT, e)
        client.close()
        return
    threading.Thread(target=_pipe, args=(client, target), daemon=True).start()
    threading.Thread(target=_pipe, args=(target, client), daemon=True).start()


def main() -> None:
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)

    os.makedirs(os.path.dirname(SOCKET_PATH), exist_ok=True)

    old_mask = os.umask(0)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(SOCKET_PATH)
        os.chmod(SOCKET_PATH, 0o666)
    except OSError as e:
        log.error("Cannot bind %s — %s", SOCKET_PATH, e)
        sys.exit(1)
    finally:
        os.umask(old_mask)

    srv.listen(64)
    log.info("Relay unix:%s → %s:%s", SOCKET_PATH, TARGET_HOST, TARGET_PORT)

    while True:
        try:
            client, _ = srv.accept()
            threading.Thread(target=_handle, args=(client,), daemon=True).start()
        except KeyboardInterrupt:
            break
        except OSError as e:
            log.error("Accept error: %s", e)

    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)


if __name__ == "__main__":
    main()
