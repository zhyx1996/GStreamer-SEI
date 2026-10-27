# GStreamer-SEI

A video streaming tool based on GStreamer that injects custom SEI NAL units into H.264/H.265 bitstreams, which can be used to transmit CARLA simulation timestamps.

Couldn't find a suitable tutorial, so I wrote it from scratch...

## Environment

It is recommended to use the pip version of the GStreamer bundle:

```bash
pip install gstreamer-bundle opencv-python numpy pyyaml
```

If you need to run `demo.py --carla` or `carla_camera.py`, you also need to install the corresponding version of the CARLA Python package.

Or use the official installer (Windows):

- [GStreamer MSVC x86_64](https://gstreamer.freedesktop.org/download/) install runtime + devel
- Automatically sets the `GSTREAMER_1_0_ROOT_MSVC_X86_64` environment variable during installation
- `pip install pygobject pycairo`

## Quick Start

```python
from gst_streaming import GStreamerConfig, GStreamerObject

cfg = GStreamerConfig()
cfg.vcodec = "auto"  # auto-detect nvh265enc, fallback to x265enc
cfg.output_mode = "rtsp_server"  # or "rtsp" to push to MediaMTX
cfg.output_host = "0.0.0.0"
cfg.output_port = 8554
cfg.rtsp_mount = "/stream/cam_front_left"
cfg.output_url = "rtsp://127.0.0.1:8554/stream/cam_front_left"

gst = GStreamerObject(cfg)
gst.initialize_pipe()

# Push BGRA frames
gst.send_frame_in_bytes(bgra_bytes)

gst.destroy_pipe()
```

See `demo.py` for a complete example.

## Running the Demo

`demo.py` supports two modes:

```bash
python demo.py --video
python demo.py --carla
```

By default, the `--video` mode reads:

```text
D:\Navigation\Code\gst\test.mp4
```

By default, it starts a built-in RTSP Server listening on:

```text
rtsp://0.0.0.0:8554/stream/cam_front_left
```

Test pull commands:

```bash
ffplay -fflags nobuffer -flags low_delay -framedrop rtsp://127.0.0.1:8554/stream/cam_front_left
```

or:

```bash
gst-launch-1.0 rtspsrc location=rtsp://127.0.0.1:8554/stream/cam_front_left latency=0 drop-on-latency=true buffer-mode=3 ! rtph265depay ! h265parse ! nvh265dec ! d3d11videosink sync=false
```

If you want to switch back to pushing to [MediaMTX](https://github.com/bluenviron/mediamtx), change the output mode in `demo.py` / `GStreamerConfig` to:

```python
cfg.output_mode = "rtsp"
cfg.output_url = "rtsp://127.0.0.1:8554/stream/cam_front_left"
```

And start MediaMTX first.

## Attaching Cameras to Existing CARLA Vehicles

`carla_camera.py` connects to an existing CARLA world, finds existing vehicles, and attaches one or more cameras to the vehicle for RTSP streaming. It does not create vehicles, set `fixed_delta_seconds`, or call `world.tick()`. It is suitable for scenarios where another host program is responsible for vehicle control and simulation advancement.

By default, it reads `config.yaml` in the current directory:

```bash
python carla_camera.py
```

You can also explicitly specify the configuration:

```bash
python carla_camera.py --config config.yaml
```

Vehicle lookup configuration example:

```yaml
vehicle:
  label: Car_001
  model: vehicle.lincoln.mkz_2020
  role_name: car
```

`role_name` is the recommended matching field. If multiple vehicles in the same world match, please specify `actor_id` in the config. `label` is mainly for readability; CARLA actors may not have this attribute by default.

Camera configuration example:

```yaml
cameras:
  - enable: true
    sensor_type: sensor.camera.rgb
    role_name: car_001_camera_rgb_front_left
    location: { x: 3.0, y: -2.1, z: 1.9 }
    rotation: { pitch: -38.0, yaw: -90.0, roll: 0.0 }
    image_size_x: 1920
    image_size_y: 1080
    sensor_tick: 0.1
    stream:
      mount: /stream/cam_front_left
      label: gst-front-left
```

`sensor_tick` is the camera sampling interval, not the world tick. For multiple cameras, simply append entries in the `cameras` array. The RTSP server reuses the same port and outputs multiple streams through different mounts.

## PyInstaller Packaging

The repo provides `demo.spec` for packaging `demo.py` into a single-file exe:

```bash
pyinstaller --noconfirm demo.spec
```

`carla_camera.py` corresponds to `carla_camera.spec`:

```bash
pyinstaller --noconfirm carla_camera.spec
```

`carla_camera.spec` does not bundle `config.yaml` into the exe. At runtime, place `carla_camera.exe` and `config.yaml` in the same directory, or launch the exe from a directory containing `config.yaml`. This spec outputs the exe to the current directory instead of the default `dist` directory.

`gst_streaming.py` runs before `import gi`:

```python
import gstreamer_libs
gstreamer_libs.setup_python_environment()
```

This restores the runtime environment of `pip install gstreamer-bundle` in frozen programs. `demo.spec` also collects the following GStreamer wheel packages:

- `gstreamer_libs`
- `gstreamer_plugins`
- `gstreamer_plugins_libs`
- `gstreamer_plugins_restricted`
- `gstreamer_plugins_gpl`
- `gstreamer_plugins_gpl_restricted`
- `gstreamer_python`
- `gstreamer_ext_runtime`

On machines without an NVIDIA GPU, GStreamer plugin scanning or hardware encoder detection may produce D3D11 / MediaFoundation-related warnings. If you need stable operation on GPU-less machines, it is recommended to further trim unnecessary hardware-related plugins in the spec, or fix `vcodec` to the software encoder `x265enc`.

## Streaming Modes

| Mode | Description | Test Status |
|---|---|---|
| `udp` | RTP over UDP, connectionless low latency | AI wrote it, I haven't tested |
| `tcp` | RTP over TCP, reliable transmission | Same |
| `srt` | SRT protocol, low latency & reliable, suitable for public networks | Same |
| `rtsp` | Push to RTSP servers like MediaMTX using `rtspclientsink` | Tested |
| `rtsp_server` | Current process starts `gst-rtsp-server`, clients pull directly | Tested |

## SEI Format

```
Start code:    00 00 00 01
NAL header:    4E 01     (H.265 PREFIX_SEI, type=39)
              / 06         (H.264 SEI, type=6)
payloadType:   C8         (200, custom)
payloadSize:   09
payload:       3B + 8 bytes big-endian uint64 UTC nanosecond timestamp
```

The sender injects SEI after the parser's src pad. If `vcodec="auto"` is used, the code saves the actual selected encoder to avoid accidentally injecting an H.264 SEI header into an H.265 stream.

The timestamp source is controlled by sei_use_local_time:

- 	rue (default): sender local system time (UTC nanoseconds). Can be subtracted from the receiver wall clock to get end-to-end latency.
- alse: CARLA simulation time (carla_image.timestamp converted to nanoseconds). Intended for simulation-time alignment; do not subtract from wall clock.

The payload is always nanoseconds in both cases, so parsing is identical.

## Windows Log Notes

When using `gstreamer-bundle`'s `gst-launch-1.0` to pull streams, you may see something like:

```text
giolibproxy.dll: The specified module could not be found
Failed to load module ... giolibproxy.dll
```

This is usually a GIO proxy module dependency warning and generally does not affect local RTSP pulling.

## Decoder Side (C++)

Reference implementation for parsing SEI and extracting timestamps from a parser sink pad probe:

```cpp
void parseSei(GstBuffer* buffer)
{
    if (mCodec != "h264" && mCodec != "h265")
        return;

    GstMapInfo map;
    if (!gst_buffer_map(buffer, &map, GST_MAP_READ))
        return;

    const uint8_t* data = map.data;
    size_t size = map.size;
    const size_t headSize = 4 + (mCodec == "h264" ? 1 : 2);

    for (size_t i = 0; i + headSize + 1 + 1 + 9 <= size; ++i) {
        // Search for start code: 00 00 00 01
        if (data[i] != 0x00 || data[i + 1] != 0x00 ||
            data[i + 2] != 0x00 || data[i + 3] != 0x01)
            continue;

        // NAL type: H.264 type=6, H.265 type=39/40
        int nalType;
        if (mCodec == "h265") {
            nalType = (data[i + 4] >> 1) & 0x3F;
            if (nalType != 39 && nalType != 40) continue;
        } else {
            nalType = data[i + 4] & 0x1F;
            if (nalType != 6) continue;
        }

        // payloadType 200 (0xC8)
        if (data[i + headSize] != 0xC8) break;
        uint8_t payloadSize = data[i + headSize + 1];
        if (payloadSize != 9) break;

        // EBSP -> RBSP: skip emulation prevention bytes (00 00 03)
        const uint8_t* src = &data[i + headSize + 2];
        uint8_t cleanPayload[64];
        size_t maxSrcLen = size - (i + headSize + 2);
        size_t si = 0, di = 0;
        while (si < maxSrcLen && di < payloadSize) {
            if (si + 3 < maxSrcLen &&
                src[si] == 0x00 && src[si + 1] == 0x00 &&
                src[si + 2] == 0x03 && src[si + 3] <= 0x03) {
                cleanPayload[di++] = src[si++];
                cleanPayload[di++] = src[si++];
                si++;  // skip 0x03
            } else {
                cleanPayload[di++] = src[si++];
            }
        }

        if (di < 9 || cleanPayload[0] != ';') break;

        // 8 bytes big-endian uint64 UTC ns
        uint64_t utcNs = 0;
        for (int j = 0; j < 8; ++j)
            utcNs = (utcNs << 8) | cleanPayload[1 + j];

        // Store by PTS
        uint64_t pts = GST_BUFFER_PTS(buffer);
        {
            std::lock_guard<std::mutex> lock(mSeiMutex);
            mPtsToSeiData[pts] = { utcNs };
        }
        break;  // Only take the first SEI per frame
    }

    gst_buffer_unmap(buffer, &map);
}
```

---