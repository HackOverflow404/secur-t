#!/usr/bin/env python3
"""SECUR-T hub: the single owner of the Echo's echo_camera socket, fanned out to many readers.

EchoCameraStreamer on the Echo accepts one client at a time. This hub holds that one
connection (via ADB forwarding) and re-serves it on localhost:
  * ECH0_PORT  (7001): the identical ECH0 v1 protocol, for echo_camera_receiver.py --hub
                       (the laptop's virtual camera).
  * ANNEXB_PORT (7002): plain H.264 Annex-B, for go2rtc (tcp:// source).
Every new client gets the cached codec config, then frames from the next keyframe.
Clients stay connected while the Echo side reconnects; a fresh config precedes new frames.
"""
import argparse
import collections
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time

CONFIG, KEY, EOS = 2, 1, 4
MAX_PAYLOAD = 16 * 1024 * 1024
HEADER = struct.pack('>4s5I', b'ECH0', 1, 1280, 720, 25, 1000000)
PACKAGE = 'com.echo.camerastreamer'
# Seconds to let EchoCameraStreamer's accessibility keeper restart a killed service before
# falling back to launching its Activity (which briefly takes over the Echo's screen).
KEEPER_GRACE = 20
QUEUE_LIMIT = 90  # ~4 s of video; a client further behind than this is resynced at a keyframe
STATUS = '/run/secur-t-hub/status.json'  # read by mainframe-bridge for Home Assistant


def log(message):
    print(f'[{time.strftime("%H:%M:%S")}] {message}', file=sys.stderr, flush=True)


class Client:
    def __init__(self, sock, addr, annexb):
        self.sock, self.addr, self.annexb = sock, addr, annexb
        self.queue = collections.deque()
        self.cond = threading.Condition()
        self.need_key = True
        self.closed = False
        self.name = f'{"annexb" if annexb else "ech0"} {addr[0]}:{addr[1]}'

    def push(self, packet, config_packet):
        """packet = (pts, flags, payload). Called from the source thread only."""
        pts, flags, _ = packet
        with self.cond:
            if self.closed:
                return
            if flags & CONFIG:
                self.queue.append(packet)
                self.need_key = True
            elif self.need_key:
                if not flags & KEY:
                    return
                self.need_key = False
                self.queue.append(packet)
            else:
                self.queue.append(packet)
            if len(self.queue) > QUEUE_LIMIT:
                # Too slow: drop the backlog, replay config, wait for the next keyframe.
                self.queue.clear()
                if config_packet:
                    self.queue.append(config_packet)
                self.need_key = True
                log(f'{self.name}: fell behind, resyncing at next keyframe')
            self.cond.notify()

    def close(self):
        with self.cond:
            self.closed = True
            self.cond.notify()
        try:
            self.sock.close()
        except OSError:
            pass

    def run(self, hub):
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock.settimeout(5)
            if not self.annexb:
                self.sock.sendall(HEADER)
            while True:
                with self.cond:
                    while not self.queue and not self.closed:
                        self.cond.wait()
                    if self.closed:
                        return
                    pts, flags, payload = self.queue.popleft()
                if self.annexb:
                    self.sock.sendall(payload)
                else:
                    self.sock.sendall(struct.pack('>IQI', len(payload), pts, flags) + payload)
        except OSError as exc:
            log(f'{self.name}: disconnected ({exc})')
        finally:
            hub.remove(self)


class Hub:
    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.clients = []
        self.config_packet = None
        self.connected = False
        self.frames = 0  # since the last status write
        self.last_frame = 0.0

    def add(self, client):
        with self.lock:
            self.clients.append(client)
            if self.config_packet:
                client.push(self.config_packet, self.config_packet)
        log(f'{client.name}: connected ({len(self.clients)} clients)')

    def remove(self, client):
        client.close()
        with self.lock:
            if client in self.clients:
                self.clients.remove(client)

    def broadcast(self, packet):
        pts, flags, payload = packet
        with self.lock:
            if flags & CONFIG:
                self.config_packet = packet
            # The Echo sends SPS/PPS once per encoder session and bare IDRs after that. Annex-B
            # readers (go2rtc snapshots, late joiners) need them inline, so repeat them per IDR.
            inline = packet
            if flags & KEY and self.config_packet:
                inline = (pts, flags, self.config_packet[2] + payload)
            for client in self.clients:
                client.push(inline if client.annexb else packet, self.config_packet)

    def serve(self, port, annexb):
        server = socket.create_server((self.args.bind, port), reuse_port=False)
        log(f'Listening for {"Annex-B" if annexb else "ECH0"} clients on {self.args.bind}:{port}')
        while True:
            sock, addr = server.accept()
            client = Client(sock, addr, annexb)
            self.add(client)
            threading.Thread(target=client.run, args=(self,), daemon=True).start()

    # --- Echo side -------------------------------------------------------

    def adb(self, *command, timeout=15):
        return subprocess.run(['adb', '-s', self.args.serial, *command], capture_output=True, text=True, timeout=timeout)

    def service_running(self):
        check = self.adb('shell', 'dumpsys', 'activity', 'services', PACKAGE)
        if check.returncode:
            raise ConnectionError('cannot query Echo over ADB: ' + (check.stderr.strip() or f'adb exited {check.returncode}'))
        return '.StreamService' in check.stdout

    def keeper_enabled(self):
        enabled = self.adb('shell', 'settings', 'get', 'secure', 'enabled_accessibility_services').stdout
        return f'{PACKAGE}/.KeeperService' in enabled or f'{PACKAGE}/{PACKAGE}.KeeperService' in enabled

    def resumed_activity(self):
        for line in self.adb('shell', 'dumpsys', 'activity', 'activities').stdout.splitlines():
            if 'mResumedActivity' in line:
                fields = line.split('{', 1)[-1].split()
                return fields[2] if len(fields) > 2 and '/' in fields[2] else None
        return None

    def ensure_service(self):
        """Android 11 only lets a foreground service open the camera if a visible Activity
        started it, so a stopped service is restarted by launching MainActivity with autostart."""
        self.adb('wait-for-device', timeout=60)
        deadline = time.monotonic() + 120
        while self.adb('shell', 'getprop', 'sys.boot_completed').stdout.strip() != '1':
            if time.monotonic() > deadline:
                raise ConnectionError('Echo still booting after 120 s')
            time.sleep(3)
        if self.service_running():
            return
        if self.keeper_enabled():
            # The app's accessibility keeper restarts the service from the background once Android
            # rebinds it (1-16 s restart backoff); only fall back to the Activity if it doesn't.
            deadline = time.monotonic() + KEEPER_GRACE
            while time.monotonic() < deadline:
                time.sleep(1)
                if self.service_running():
                    log('Echo camera service restarted by its keeper')
                    return
            log(f'Keeper did not restart the service within {KEEPER_GRACE} s')
        previous = self.resumed_activity()
        log(f'Echo camera service not running; autostarting (foreground was {previous})')
        launch = self.adb('shell', 'am', 'start', '-n', f'{PACKAGE}/.MainActivity', '--ez', 'autostart', 'true')
        if launch.returncode or 'Error' in launch.stdout:
            raise ConnectionError('could not launch EchoCameraStreamer: ' + (launch.stderr.strip() or launch.stdout.strip()))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            time.sleep(1)
            if self.service_running():
                log('Echo camera service started')
                self.restore_foreground(previous)
                return
        raise ConnectionError('Echo camera service did not start within 30 s (camera permission granted?)')

    def restore_foreground(self, previous):
        if not previous or previous.startswith(PACKAGE + '/'):
            return
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            current = self.resumed_activity()
            if current == previous or (current and not current.startswith(PACKAGE + '/')):
                return
            time.sleep(1)
        self.adb('shell', 'am', 'start', '-n', previous)

    def read_exact(self, sock, size):
        data = bytearray(size)
        view = memoryview(data)
        while view:
            count = sock.recv_into(view)
            if not count:
                raise ConnectionError('Echo closed the stream')
            view = view[count:]
        return bytes(data)

    def source_once(self):
        self.ensure_service()
        forward = self.adb('forward', f'tcp:{self.args.adb_port}', 'localabstract:echo_camera')
        if forward.returncode:
            raise ConnectionError('adb forward failed: ' + forward.stderr.strip())
        with socket.create_connection(('127.0.0.1', self.args.adb_port), timeout=10) as sock:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(10)
            header = self.read_exact(sock, 24)
            if header != HEADER:
                raise ConnectionError(f'unexpected ECH0 header {header.hex()}')
            log('Connected to Echo camera')
            self.connected = True
            frames, last_report = 0, time.monotonic()
            while True:
                size, pts, flags = struct.unpack('>IQI', self.read_exact(sock, 16))
                if size > MAX_PAYLOAD:
                    raise ConnectionError(f'packet of {size} bytes exceeds limit')
                payload = self.read_exact(sock, size)
                if flags & EOS:
                    raise ConnectionError('encoder sent end of stream')
                if not payload:
                    continue
                if not payload.startswith((b'\x00\x00\x00\x01', b'\x00\x00\x01')):
                    # The MTK encoder emits Annex-B (see EchoCameraStreamer README); anything else
                    # would need the receiver's AVCC conversion, which the hub doesn't do.
                    raise ConnectionError(f'non-Annex-B payload {payload[:8].hex()}')
                self.broadcast((pts, flags, payload))
                if not flags & CONFIG:
                    frames += 1
                    self.frames += 1
                    self.last_frame = time.time()
                now = time.monotonic()
                if now - last_report >= 300:
                    log(f'{frames / (now - last_report):.1f} fps from Echo, {len(self.clients)} clients')
                    frames, last_report = 0, now

    def status_loop(self, every=5):
        if not os.path.isdir(os.path.dirname(STATUS)):
            return  # not running under systemd (no RuntimeDirectory)
        while True:
            time.sleep(every)
            frames, self.frames = self.frames, 0
            status = {'connected': self.connected and time.time() - self.last_frame < 10,
                      'fps': round(frames / every, 1), 'clients': len(self.clients),
                      'last_frame': round(self.last_frame), 'updated': round(time.time())}
            with open(STATUS + '.tmp', 'w') as f:
                json.dump(status, f)
            os.replace(STATUS + '.tmp', STATUS)

    def source_loop(self):
        backoff = 1
        while True:
            started = time.monotonic()
            try:
                self.source_once()
            except (OSError, ConnectionError, subprocess.SubprocessError) as exc:
                log(f'Echo source down: {exc}')
            self.connected = False
            if time.monotonic() - started > 60:
                backoff = 1
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--serial', default='G0911B0594572B24')
    parser.add_argument('--adb-port', type=int, default=7000)
    parser.add_argument('--ech0-port', type=int, default=7001)
    parser.add_argument('--annexb-port', type=int, default=7002)
    parser.add_argument('--bind', default='127.0.0.1')
    args = parser.parse_args()
    hub = Hub(args)
    threading.Thread(target=hub.serve, args=(args.ech0_port, False), daemon=True).start()
    threading.Thread(target=hub.serve, args=(args.annexb_port, True), daemon=True).start()
    threading.Thread(target=hub.status_loop, daemon=True).start()
    hub.source_loop()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
