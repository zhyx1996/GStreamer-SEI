# carla-gst-sei

Carla 仿真相机 GStreamer 推流工具，支持在 H.264/H.265 码流中注入自定义 SEI NAL 单元携带时间戳。

## 功能

- RGB 相机原始帧 BGRA → GStreamer pipeline → 网络推流
- 支持 x264/x265/NVENC/VAAPI 多种编码器
- 支持 UDP/TCP/SRT/RTSP 多种传输协议
- H.265/H.264 码流中插入 prefix SEI NAL（payloadType=200），携带 8 字节 UTC 微秒时间戳
- SEI payload 做 emulation prevention 防竞争处理（`00 00` → `00 00 03`），避免起始码误匹配

## 依赖 (Windows)

1. [GStreamer MSVC x86_64](https://gstreamer.freedesktop.org/download/) runtime + devel
2. `pip install pygobject pycairo`
3. 设置环境变量 `GSTREAMER_1_0_ROOT_MSVC_X86_64`

## 使用

```python
from gstreamer import GStreamerConfig, GStreamerObject

cfg = GStreamerConfig()
cfg.vcodec = "nvh265enc"
cfg.output_mode = "rtsp"
cfg.output_url = "rtsp://127.0.0.1:8554/stream"

gst = GStreamerObject(cfg)
gst.initialize_pipe()

# 推流 BGRA 帧
gst.send_frame_in_bytes(bgra_bytes)

gst.destroy_pipe()
```

完整示例见 `demo.py`。

## SEI 格式

```
起始码:    00 00 00 01
NAL 头:    4E 01     (H.265 PREFIX_SEI, type=39)
          / 06         (H.264 SEI, type=6)
payloadType: C8        (200, 自定义)
payloadSize: 09
payload:    3B + 8 字节 big-endian uint64 UTC 微秒时间戳
```

## 许可证

MIT
