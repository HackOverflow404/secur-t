# SECUR-T

Turns Clippy's (jailbroken Echo Show 5) camera into a security camera on Mainframe:
live view in Home Assistant, plus continuous recording to S.H.O.D.A.N with a 25 GB ring buffer.

```
Echo (EchoCameraStreamer, ECH0 over ADB)
  └─ secur-t-hub      owns the single Echo connection, fans it out on localhost
       ├─ :7001 ECH0    → laptop virtual camera (/dev/video10) via echo_camera_receiver.py --hub
       └─ :7002 Annex-B → secur-t-go2rtc (RTSP 127.0.0.1:8554/clippy, API/snapshots 127.0.0.1:1984)
                             ├─ Home Assistant (Generic Camera)
                             └─ secur-t-recorder → /mnt/shodan/Security/Clippy/clippy_YYYY-MM-DD_HH-MM-SS.mp4
```

- **Hub** (`hub.py`): EchoCameraStreamer only serves one client, so the hub is now the only thing
  that talks to the Echo. It autostarts the Echo service when needed (same logic as the receiver),
  replays SPS/PPS to new clients and inlines them before every IDR on the Annex-B port (the Echo
  only sends them once per encoder session; go2rtc snapshots need them per keyframe).
- **go2rtc**: static binary in `/usr/local/bin`, localhost only. Remote viewing goes through HA.
- **Recorder** (`recorder.py`): `ffmpeg -c copy` (no re-encode) into 10-minute fragmented MP4s
  aligned to the clock; every 5 s it deletes the oldest `clippy_*.mp4` until the folder is
  ≤ 25 GB (10^9 bytes), the in-progress file included. Restarts ffmpeg if it exits or writes
  nothing for 60 s. At ~3.1 Mb/s (the tuned camera, since v0.4), 25 GB holds about 18 hours.

## Install / uninstall (from the laptop)

```sh
./install     # copies to mainframe:/opt/secur-t, units to /etc/systemd/system, go2rtc to /usr/local/bin, apt ffmpeg
./uninstall   # removes all of that; recordings on SHODAN are kept
```

Change the cap / folder / segment length in `systemd/secur-t-recorder.service`, then `./install`.

## Home Assistant

Settings → Devices & services → Add integration → **Generic Camera**:

- Still image URL: `http://127.0.0.1:1984/api/frame.jpeg?src=clippy`
- Stream source URL: `rtsp://127.0.0.1:8554/clippy`
- RTSP transport: TCP; leave auth empty; "Verify SSL" off.

`ha/apply` (one-time, restarts HA): adds a **SECUR-T** section (live camera + Recordings tile) to the
Home dashboard and recreates the `homeassistant` container with the recordings mounted read-only at
`/media/Clippy`, so they show up under Media → My media → Clippy. It backs up the dashboard and the
container definition, keeps the old container as `homeassistant-old-<timestamp>`, and prints a rollback command.

Status and controls (Recording switch, Restart camera, storage, footage kept, frame rate) come
from mainframe-bridge, which reads `/run/secur-t-hub/status.json` and `/run/secur-t-recorder/status.json`
(written every 5 s; the recorder's survives while it's stopped). The Clippy page's Camera section
and the Home SECUR-T section live in mainframe-bridge's `dashboard.py`.

## Checks

```sh
ssh mainframe 'journalctl -u secur-t-hub -u secur-t-go2rtc -u secur-t-recorder -f'
ssh mainframe 'curl -s 127.0.0.1:1984/api/streams'            # producers/consumers
ssh mainframe 'du -sh /mnt/shodan/Security/Clippy'
```

The laptop virtual camera (`echo-camera-virtual` user service in EchoCameraStreamer) reads from
the hub; its `--no-autostart` flag no longer has an effect because autostart is the hub's job.
