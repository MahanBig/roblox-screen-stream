# Roblox Screen Stream

Stream your computer screen live onto a part in a Roblox game, using
[EditableImage](https://create.roblox.com/docs/reference/engine/classes/EditableImage).

- Up to **640×352 at 15 fps** in Studio, **320×176 at 15 fps** within a bandwidth cap that's meant to be safe for live servers
- A small video codec (JPEG/MPEG-style) so it looks good at low bandwidth
- Drag-and-drop install: one model file, no scripts to place in different services
- Any number of screens: every part named `StreamScreen` shows the stream

## How it works

```
Your PC                          Roblox server                     Each player
sender/stream_server.py   HTTP   StreamServer (Script)   Remote    StreamClient (client Script)
capture → encode ───────────────▶ polls 5×/s ─────────────────────▶ decode → EditableImage
```

1. **The sender** (Python) captures your screen, shrinks it, and encodes it:
   - **Colour format:** YCbCr 4:2:0, which stores colour at half resolution because eyes notice brightness detail far more than colour.
   - **Compression:** 8×8 DCT blocks with JPEG quantization tables.
   - **Inter frames:** most frames only send what changed since the last one.
   - **Rate control:** each frame is encoded at the best quality that fits a bandwidth cap.
2. **StreamServer** fetches new frames over HTTP (`HttpService`) and forwards them, still compressed, to every player with a RemoteEvent.
3. **StreamClient** decodes them and draws them into an EditableImage on each screen part:
   - **Fast decompression:** zstd, decoded natively by Roblox's `EncodingService`.
   - **No hitching:** each frame's decode is spread over several game frames.
   - **Exact decoding:** the decoder uses only whole-number maths, so it rebuilds every frame bit-for-bit like the sender. Without that, small errors would pile up from frame to frame.

## Requirements

- **Python 3.14 or newer.** The sender uses the built-in `compression.zstd`.
- **Windows, macOS or Linux** for the sender.
- **Roblox Studio.** Your experience must allow the EditableImage API. Roblox may require an ID-verified account to use it in published experiences.

## Quick start (testing in Studio)

1. **Download** this repo (green **Code** button → *Download ZIP*, or `git clone`).
2. **Double-click `sender/ScreenStream.pyw`.** A small window opens with a random token already filled in.
   Press **Start streaming**. The first time, it offers to install the Python packages it needs.
   The bottom line shows the frame rate and bandwidth once a second.
3. **In Studio**, right-click **Workspace** → **Insert from File…** → choose `ScreenStream.rbxm`.
   This adds a `ScreenStream` folder with a screen part and the scripts.
4. **Open `Workspace.ScreenStream.StreamServer`** and set `TOKEN` to the token from the window (use its **Copy** button).
5. **Open Home → Game Settings → Security** and turn on:
   - **Allow HTTP Requests**
   - **Allow Mesh / Image APIs**
6. **Press Play.** The screen 30 studs in front of the origin shows your monitor.

## Screens

- **Placement:** any `BasePart` named `StreamScreen` (or tagged `StreamScreen` with CollectionService) anywhere in Workspace shows the stream. Move, resize or copy the included one freely.
- **Which face:** the picture goes on the part's **Front** face. To use another face, add a string attribute `Face` to the part, for example `Back` or `Top`.
- **Several screens:** all screens share one decoded image, so extra screens cost almost nothing.

## Settings

All settings are in the sender window (or, from a terminal, `python sender/stream_server.py --token ...`). The Roblox side picks up the picture size and frame rate automatically.

| Option | Default | What it does |
|---|---|---|
| `--token` | (required) | Shared secret, letters and digits only; must match `TOKEN` in StreamServer |
| `--size WxH` | from preset | Resolution, e.g. `480x272` (multiples of 16, up to 1024). Roblox picks it up automatically, even mid-game |
| `--preset live` | ✓ | 320×176, 15 fps, 40 KB/s per player. Meant for published games |
| `--preset max` | | 640×352, 15 fps, 320 KB/s per player. **Studio only** |
| `--budget N` | from preset | Bandwidth cap in KB/s per player |
| `--fps N` | 15 | Frames per second |
| `--monitor N` | 1 | Which monitor to capture (1 = primary) |
| `--port N` | 8080 | Local port |

In StreamServer, `URL` is where the sender is reachable and `POLL_INTERVAL` is how often it asks for frames.

## Using it in a published game

Roblox's servers can't reach `localhost` on your PC, so you have to expose the sender to the internet:

1. **Run a tunnel**, for example:
   ```bash
   cloudflared tunnel --url http://localhost:8080
   ```
   It prints a public `https://….trycloudflare.com` address.
2. **Set `URL` in StreamServer** to that address. Use a long random `TOKEN`, because anyone with both the URL and the token can watch your stream.
3. **Keep the `live` preset.** Roblox throttles servers that send a lot of data to each player. `max` is fine in Studio, where everything runs on your PC, but not on real servers.

Not yet tested:
- **Live Roblox servers:** only Studio has been tested so far, so check performance in a real server before relying on it.
- **Phones:** decoding is several times slower on phones. `live` should be fine, but `max` will likely drop frames there.

## Be careful what you stream

- **Images aren't moderated:** whatever is on the captured monitor goes straight to every player in the server, with no Roblox image moderation. That includes notifications, messages and passwords you type. You're responsible for it under the [Roblox Community Standards](https://en.help.roblox.com/hc/articles/203313410).
- **Use a private setup:** use a monitor or screen with nothing private on it. Prefer private servers.
- **People on camera:** don't stream anyone else without their permission.

## Changing the Roblox scripts

The scripts in `roblox/` are the source of truth, and `ScreenStream.rbxm` is built from them. After editing one:
1. Paste it into the matching script in Studio.
2. Right-click the `ScreenStream` folder → **Save to File…** → replace `ScreenStream.rbxm`.

`StreamDecoder.luau` must stay in exact step with the `Decoder` in `sender/codec.py`. If the two disagree by even one pixel value, inter frames drift.

## License

[MIT](LICENSE)
