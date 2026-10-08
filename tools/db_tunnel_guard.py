#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HK MySQL SSH 隧道守护（方案A：本地开发连香港库）
================================================
本地 127.0.0.1:13306  → 经 127.0.0.1:7892 HTTP CONNECT 代理建 SSH 隧道
                        → 转发到香港服务器(43.161.202.105)内部 127.0.0.1:3306

与原生 ssh -L 的差异（解决"连上就断/转发流被代理掐掉"）：
1. SSH 层 keepalive 每 15s 发包，防代理空闲超时掐线
2. 隧道层每 20s 向 13306 发一次真实 TCP 探测（走完整条隧道），保持代理侧流活跃
3. 传输断开自动整链重连（含代理重连）

凭据（不落盘）：环境变量 SSH_PASS_43 → 文件 ~/.ssh/ubuntu_43_pass → 交互输入。
用法：
    python tools/db_tunnel_guard.py          # 前台运行，Ctrl+C 退出
    项目 data/db_url.txt 指向 127.0.0.1:13306 即走此隧道
"""
import os
import socket
import sys
import threading
import time

import paramiko

LOCAL_PORT = 13306
PROXY = ("127.0.0.1", 7892)
REMOTE_HOST = "43.161.202.105"
REMOTE_SSH_PORT = 22
DB_TARGET = ("127.0.0.1", 3306)          # 隧道另一端：服务器视角的 MySQL
SSH_USER = "ubuntu"
PASS_FILE = os.path.expanduser("~/.ssh/ubuntu_43_pass")
KEEPALIVE_S = 15
PROBE_S = 20


def log(msg):
    print(f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}", flush=True)


def get_password():
    pw = os.environ.get("SSH_PASS_43", "").strip()
    if not pw and os.path.isfile(PASS_FILE):
        try:
            with open(PASS_FILE, "r", encoding="utf-8") as f:
                pw = f.readline().strip()
        except OSError:
            pass
    if not pw and sys.stdin.isatty():
        import getpass
        pw = getpass.getpass(f"{SSH_USER}@{REMOTE_HOST} 密码: ")
    return pw


def open_proxy_tunnel(dst_host, dst_port):
    """经本地混合代理建 HTTP CONNECT 隧道，返回已打通的 socket（含残留字节）。"""
    s = socket.create_connection(PROXY, timeout=15)
    req = f"CONNECT {dst_host}:{dst_port} HTTP/1.1\r\nHost: {dst_host}:{dst_port}\r\n\r\n"
    s.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    head, _, leftover = buf.partition(b"\r\n\r\n")
    status = head.split(b"\r\n")[0].decode("utf-8", "replace")
    if "200" not in status:
        try:
            s.close()
        except OSError:
            pass
        raise ConnectionError(f"代理 CONNECT 失败: {status}")
    return s, leftover


class TunneledSocket:
    """把 CONNECT 后残留字节（可能是 SSH banner）先吐给 paramiko。"""

    def __init__(self, sock, leftover=b""):
        self._sock = sock
        self._buf = leftover

    def recv(self, nbytes, flags=0):
        if self._buf:
            data, self._buf = self._buf[:nbytes], self._buf[nbytes:]
            return data
        return self._sock.recv(nbytes, flags)

    def send(self, data, flags=0):
        return self._sock.send(data, flags)

    def sendall(self, data, flags=0):
        return self._sock.sendall(data, flags)

    def __getattr__(self, name):
        return getattr(self._sock, name)


def connect_transport(password):
    """每次调用都新建代理 socket + Transport；connect() 会自动启动线程。"""
    raw, leftover = open_proxy_tunnel(REMOTE_HOST, REMOTE_SSH_PORT)
    transport = paramiko.Transport(TunneledSocket(raw, leftover))
    transport.daemon = True
    try:
        transport.connect(username=SSH_USER, password=password)
    except Exception:
        try:
            transport.close()
        except Exception:
            pass
        raise
    transport.set_keepalive(KEEPALIVE_S)
    return transport


def pump(a, b):
    try:
        while True:
            data = a.recv(32768)
            if not data:
                break
            b.sendall(data)
    except Exception:
        pass
    finally:
        for sk in (a, b):
            try:
                sk.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle_local_conn(transport, client_sock):
    try:
        chan = transport.open_channel("direct-tcpip", DB_TARGET,
                                      client_sock.getpeername())
    except Exception as e:
        log(f"  通道打开失败: {e}")
        try:
            client_sock.close()
        except OSError:
            pass
        return
    threading.Thread(target=pump, args=(client_sock, chan), daemon=True).start()
    threading.Thread(target=pump, args=(chan, client_sock), daemon=True).start()


def serve(transport, stop_evt):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", LOCAL_PORT))
    listener.listen(32)
    listener.settimeout(2.0)
    log(f"本地 127.0.0.1:{LOCAL_PORT} 监听就绪")

    def periodic_probe():
        """隧道层心跳：定期开一次 TCP 并经隧道读 MySQL banner 再关，
        让代理上的转发流始终有真实流量、不过期。"""
        while not stop_evt.wait(PROBE_S):
            ok = False
            try:
                s = socket.create_connection(("127.0.0.1", LOCAL_PORT), timeout=3)
                s.settimeout(2.0)
                try:
                    ok = len(s.recv(48)) > 0      # MySQL 服务端问候包
                except Exception:
                    pass
                s.close()
            except Exception:
                pass
            if not ok:
                # 探测失败交给主循环用 transport.is_active() 判活，这里不强行退出
                pass

    threading.Thread(target=periodic_probe, daemon=True).start()
    try:
        while not stop_evt.is_set():
            try:
                client_sock, _ = listener.accept()
            except socket.timeout:
                if not transport.is_active():
                    log("检测到 SSH 传输已断开")
                    break
                continue
            threading.Thread(target=handle_local_conn,
                             args=(transport, client_sock), daemon=True).start()
    finally:
        listener.close()


def main():
    password = get_password()
    if not password:
        log("缺少密码：设置 SSH_PASS_43 或创建 ~/.ssh/ubuntu_43_pass")
        return 2
    stop_evt = threading.Event()
    try:
        while not stop_evt.is_set():
            log("建立 SSH 隧道（经 7892 代理）...")
            transport = None
            try:
                transport = connect_transport(password)
                log("SSH 认证成功")
                serve(transport, stop_evt)
            except KeyboardInterrupt:
                break
            except Exception as e:
                log(f"隧道异常: {type(e).__name__}: {e}")
            finally:
                if transport is not None:
                    try:
                        transport.close()
                    except Exception:
                        pass
            if not stop_evt.is_set():
                log("6 秒后自动重连 ...")
                time.sleep(6)
    except KeyboardInterrupt:
        pass
    finally:
        stop_evt.set()
        log("隧道守护已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
