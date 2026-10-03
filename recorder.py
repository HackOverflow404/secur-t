#!/usr/bin/env python3
"""Continuous recorder for SECUR-T (Clippy's camera) with a size-capped ring buffer.

Runs ffmpeg (stream copy, no re-encode) from go2rtc's RTSP feed into fixed-length MP4
segments, restarting it on failure or stall. Every few seconds the oldest segments are
deleted until the folder fits under the cap (the segment being written counts too).
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

PREFIX, SUFFIX = 'clippy_', '.mp4'
STATUS = '/run/secur-t-recorder/status.json'  # read by mainframe-bridge for Home Assistant


def log(message):
    print(f'[{time.strftime("%H:%M:%S")}] {message}', file=sys.stderr, flush=True)


def segments(directory):
    files = []
    for entry in os.scandir(directory):
        if entry.name.startswith(PREFIX) and entry.name.endswith(SUFFIX) and entry.is_file():
            files.append((entry.name, entry.stat().st_size, entry.path))
    return sorted(files)  # names embed a sortable timestamp, oldest first


def prune(directory, cap):
    files = segments(directory)
    total = sum(size for _, size, _ in files)
    # Never delete the newest file: it's the one ffmpeg is writing.
    while total > cap and len(files) > 1:
        name, size, path = files.pop(0)
        try:
            os.remove(path)
            log(f'Deleted {name} ({size / 1e6:.0f} MB) to stay under {cap / 1e9:g} GB')
        except FileNotFoundError:
            pass
        total -= size
    return files, total


def write_status(files, total, cap, recording):
    if not os.path.isdir(os.path.dirname(STATUS)):
        return  # not running under systemd (no RuntimeDirectory)
    oldest = None
    if files:
        try:
            oldest = time.mktime(time.strptime(files[0][0], f'{PREFIX}%Y-%m-%d_%H-%M-%S{SUFFIX}'))
        except ValueError:
            pass
    status = {'recording': recording, 'used_bytes': total, 'cap_bytes': cap, 'segments': len(files),
              'oldest': oldest, 'newest': files[-1][0] if files else None, 'updated': round(time.time())}
    with open(STATUS + '.tmp', 'w') as f:
        json.dump(status, f)
    os.replace(STATUS + '.tmp', STATUS)


def ffmpeg_command(args):
    pattern = str(Path(args.dir) / f'{PREFIX}%Y-%m-%d_%H-%M-%S{SUFFIX}')
    return ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-rtsp_transport', 'tcp', '-timeout', '10000000', '-i', args.source,
            '-map', '0:v', '-c', 'copy',
            '-f', 'segment', '-segment_time', str(args.segment_seconds), '-segment_atclocktime', '1',
            '-reset_timestamps', '1', '-strftime', '1', '-segment_format', 'mp4',
            # Fragmented MP4: a segment cut short by a crash or power loss is still playable.
            '-segment_format_options', 'movflags=+frag_keyframe+empty_moov+default_base_moof',
            pattern]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', default='rtsp://127.0.0.1:8554/clippy')
    parser.add_argument('--dir', required=True)
    parser.add_argument('--cap-gb', type=float, default=15, help='Maximum total size in GB (10^9 bytes)')
    parser.add_argument('--segment-seconds', type=int, default=600)
    args = parser.parse_args()
    cap = int(args.cap_gb * 1e9)
    Path(args.dir).mkdir(parents=True, exist_ok=True)

    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    backoff = 2
    while not stopping:
        log(f'Recording {args.source} -> {args.dir} (cap {args.cap_gb:g} GB)')
        started = time.monotonic()
        proc = subprocess.Popen(ffmpeg_command(args))
        last_size, last_growth = None, time.monotonic()
        while not stopping and proc.poll() is None:
            time.sleep(5)
            files, total = prune(args.dir, cap)
            newest = (files[-1][0], files[-1][1]) if files else None
            if newest != last_size:
                last_size, last_growth = newest, time.monotonic()
            elif time.monotonic() - last_growth > 60:
                log('No new video written for 60 s; restarting ffmpeg')
                proc.terminate()
            write_status(files, total, cap, time.monotonic() - last_growth < 20)
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if stopping:
            break
        if time.monotonic() - started > 120:
            backoff = 2
        log(f'ffmpeg exited ({proc.returncode}); retrying in {backoff} s')
        write_status(*prune(args.dir, cap), cap, False)
        for _ in range(backoff):
            if stopping:
                break
            time.sleep(1)
        backoff = min(backoff * 2, 30)
    write_status(*prune(args.dir, cap), cap, False)  # storage stats stay visible while recording is off
    log('Stopped')


if __name__ == '__main__':
    main()
