# GStreamer-SEI

基于 GStreamer 的通用视频推流工具，支持在 H.264/H.265 码流中注入自定义 SEI NAL 单元携带时间戳或自定义元数据。

## 功能

- 原始帧数据 → GStreamer pipeline → 网络推流
- 支持 x264/x265/NVENC/VAAPI 多种编码器
- 支持 UDP/TCP/SRT/RTSP 多种传输协议
- H.265/H.264 码流中插入 prefix SEI NAL（payloadType=200），携带 8 字节 big-endian uint64 时间戳
- SEI payload 做 emulation prevention 防竞争处理（`00 00` → `00 00 03`），避免起始码误匹配

## 依赖 (Windows)

1. [GStreamer MSVC x86_64](https://gstreamer.freedesktop.org/download/) runtime + devel
2. `pip install pygobject pycairo`
3. 设置环境变量 `GSTREAMER_1_0_ROOT_MSVC_X86_64`

## 快速开始

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

## 推流模式

| 模式 | 说明 |
|---|---|
| `udp` | RTP over UDP，无连接低延迟 |
| `tcp` | RTP over TCP，可靠传输 |
| `srt` | SRT 协议，低延迟可靠，适合公网 |
| `rtsp` | 推流到 RTSP 服务器（自动选 payloader） |

## SEI 格式

```
起始码:    00 00 00 01
NAL 头:    4E 01     (H.265 PREFIX_SEI, type=39)
          / 06         (H.264 SEI, type=6)
payloadType: C8        (200, 自定义)
payloadSize: 09
payload:    3B + 8 字节 big-endian uint64 UTC 微秒时间戳
```

接收端通过 parser sink pad probe 拦截原始字节流，扫描 `00 00 00 01 4E 01 C8`（或 H.264 对应模式）提取 SEI，解码时 `nal_reader_read` 自动剥离 emulation prevention 字节。

## 许可证

MIT
