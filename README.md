# GStreamer-SEI

基于 GStreamer 的视频推流工具（Windows），支持在 H.264/H.265 码流中注入自定义 SEI NAL 单元携带时间戳。

## 环境

- Windows 10/11
- [GStreamer MSVC x86_64](https://gstreamer.freedesktop.org/download/) runtime + devel

## 依赖

1. Python 3.8+
2. `pip install pygobject pycairo`
3. 设置环境变量 `GSTREAMER_1_0_ROOT_MSVC_X86_64` 指向 GStreamer 安装路径（通常自动设置）

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

| 模式 | 说明 | 测试状态 |
|---|---|---|
| `udp` | RTP over UDP，无连接低延迟 | 未测试 |
| `tcp` | RTP over TCP，可靠传输 | 未测试 |
| `srt` | SRT 协议，低延迟可靠，适合公网 | 未测试 |
| `rtsp` | 推流到 RTSP 服务器（自动选 payloader） | 已测试 |

## SEI 格式

```
起始码:    00 00 00 01
NAL 头:    4E 01     (H.265 PREFIX_SEI, type=39)
          / 06         (H.264 SEI, type=6)
payloadType: C8        (200, 自定义)
payloadSize: 09
payload:    3B + 8 字节 big-endian uint64 UTC 微秒时间戳
```

## 解码端 (C++)

从 parser sink pad probe 中解析 SEI 并提取时间戳的参考实现：

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
        // 搜索 start code: 00 00 00 01
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

        // EBSP → RBSP: 跳过 emulation prevention 字节 (00 00 03)
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

        // 8 字节 big-endian uint64 UTC us → ns
        uint64_t utcUs = 0;
        for (int j = 0; j < 8; ++j)
            utcUs = (utcUs << 8) | cleanPayload[1 + j];
        uint64_t utcNs = utcUs * 1000;

        // 按 PTS 存储
        uint64_t pts = GST_BUFFER_PTS(buffer);
        {
            std::lock_guard<std::mutex> lock(mSeiMutex);
            mPtsToSeiData[pts] = { utcNs };
        }
        break;  // 每帧仅取第一个 SEI
    }

    gst_buffer_unmap(buffer, &map);
}
```

## 许可证

MIT
