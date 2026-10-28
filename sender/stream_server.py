"""Captures a monitor and serves it as DCT-coded frames for Roblox (see codec.py).

    python stream_server.py --token my-secret             # "live" preset (default)
    python stream_server.py --token my-secret --preset max

Every frame is a record:  seq u32 | kind u8 (0 = keyframe, 1 = inter) | len u32 | payload

GET /frames?t=TOKEN&since=SEQ -> base64 of every record newer than SEQ. If SEQ is too
old (or 0), the reply starts at the most recent keyframe so the reader can rebuild.
Response headers X-Stream-Width / X-Stream-Height / X-Stream-Fps tell the Roblox side
the picture size, so it never needs editing when the preset changes.

Bandwidth cap: a token bucket refilled at 3/4 of the budget per second, holding at
most 1/4 of it, so no 1 s window can exceed the budget. Each frame is encoded at the
best quality that fits the tokens available; when nothing fits, the frame is skipped.
A still screen keeps refining towards near-lossless, since unused tokens buy detail.
"""
import argparse
import base64
import struct
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import mss
import numpy as np
from PIL import Image

import codec

PRESETS = {
    # Safe for published games: Roblox servers throttle large per-player traffic.
    "live": {"size": (320, 176), "fps": 15, "budget_kb": 40},
    # Studio only, where server and client share one PC (640 KB/s measured fine).
    "max": {"size": (640, 352), "fps": 15, "budget_kb": 320},
}
KEYFRAME_SECONDS = 2
RECORD_HEADER = 9

# Set from the command line in main().
W, H = PRESETS["live"]["size"]
FPS = PRESETS["live"]["fps"]
BUDGET = PRESETS["live"]["budget_kb"] * 1024  # bytes per second sent to each player
TOKEN = ""
MONITOR = 1

records = deque(maxlen=64)  # (seq, kind, bytes)
lock = threading.Lock()
latest_picture = None  # newest captured frame, already W x H RGB


def grab():
    """Screen capture runs on its own thread: a full-screen grab alone can take ~50 ms."""
    global latest_picture
    with mss.MSS() as sct:
        mon = sct.monitors[MONITOR]
        while True:
            start = time.perf_counter()
            shot = sct.grab(mon)
            img = Image.frombuffer("RGBA", shot.size, shot.bgra, "raw", "BGRA", 0, 1)
            small = img.resize((W, H), Image.Resampling.LANCZOS, reducing_gap=2.0).convert("RGB")
            latest_picture = np.asarray(small)
            time.sleep(max(0, 1 / FPS - (time.perf_counter() - start)))


def encode_loop():
    encoder = codec.Encoder(W, H)
    seq = 0
    last_key = -KEYFRAME_SECONDS
    rate, cap = BUDGET * 0.75, BUDGET * 0.25
    tokens = cap
    stats_at = time.perf_counter()
    stats = {"sent": 0, "bytes": 0, "skipped": 0}
    while latest_picture is None:
        time.sleep(0.05)
    while True:
        start = time.perf_counter()
        rgb = latest_picture

        tokens = min(cap, tokens + rate / FPS)
        intra = start - last_key >= KEYFRAME_SECONDS
        out = encoder.encode(rgb, max_bytes=max(0, int(tokens)) - RECORD_HEADER, force_intra=intra)
        if out:
            kind, payload = out
            seq += 1
            if intra:
                last_key = start
            record = struct.pack("<IBI", seq, kind, len(payload)) + payload
            tokens -= len(record)
            stats["sent"] += 1
            stats["bytes"] += len(record)
            with lock:
                records.append((seq, kind, record))
        else:
            stats["skipped"] += 1

        if start - stats_at >= 1:
            print(f"{stats['sent']} fps  {stats['bytes'] / 1024:.1f} KB/s  skipped {stats['skipped']}", flush=True)
            stats = {"sent": 0, "bytes": 0, "skipped": 0}
            stats_at = start

        time.sleep(max(0, 1 / FPS - (time.perf_counter() - start)))


def records_since(since):
    with lock:
        snapshot = list(records)
    if not snapshot:
        return b""
    if since < snapshot[0][0] or since > snapshot[-1][0]:
        # Reader is new, fell behind, or saw a previous run of this server:
        # start from the latest keyframe.
        keys = [i for i, (_, kind, _) in enumerate(snapshot) if kind == 0]
        if not keys:
            return b""
        return b"".join(r for _, _, r in snapshot[keys[-1]:])
    return b"".join(r for seq, _, r in snapshot if seq > since)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path != "/frames" or query.get("t", [""])[0] != TOKEN:
            self.send_error(403)
            return
        try:
            since = int(query.get("since", ["0"])[0])
        except ValueError:
            since = 0
        body = base64.b64encode(records_since(since))
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Stream-Width", str(W))
        self.send_header("X-Stream-Height", str(H))
        self.send_header("X-Stream-Fps", str(FPS))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    global W, H, FPS, BUDGET, TOKEN, MONITOR
    parser = argparse.ArgumentParser(description="Stream your screen to Roblox.")
    parser.add_argument("--token", required=True, help="shared secret; must match TOKEN in StreamServer")
    parser.add_argument("--preset", choices=PRESETS, default="live", help="live (default) or max (Studio only)")
    parser.add_argument("--budget", type=int, help="override the bandwidth cap, in KB/s per player")
    parser.add_argument("--fps", type=int, help="override the frame rate")
    parser.add_argument("--monitor", type=int, default=1, help="monitor number (1 = primary)")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    preset = PRESETS[args.preset]
    W, H = preset["size"]
    FPS = args.fps or preset["fps"]
    BUDGET = (args.budget or preset["budget_kb"]) * 1024
    TOKEN = args.token
    MONITOR = args.monitor

    threading.Thread(target=grab, daemon=True).start()
    threading.Thread(target=encode_loop, daemon=True).start()
    print(f"Serving {W}x{H} @ {FPS} fps, {BUDGET // 1024} KB/s cap, on http://localhost:{args.port}/frames")
    # Without this, Windows lets a second copy bind the same port and the old copy
    # keeps answering, so fail loudly instead.
    ThreadingHTTPServer.allow_reuse_address = False
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        sys.exit(f"Port {args.port} is in use (is another copy running?): {e}")
    server.serve_forever()


if __name__ == "__main__":
    main()
