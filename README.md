# GStreamer-SEI

基于 GStreamer 的视频推流工具，在 H.264/H.265 码流中注入自定义 SEI NAL 单元，可用于传递 CARLA 仿真时间戳。

因为没找到合适的教程，就从头写了……

## 环境

推荐直接使用 pip 版本的 GStreamer bundle：

```bash
pip install gstreamer-bundle opencv-python numpy
```

如果需要运行 `demo.py --carla`，还需要安装对应版本的 CARLA Python 包。

或使用官方安装程序 (Windows)：

- [GStreamer MSVC x86_64](https://gstreamer.freedesktop.org/download/) 安装runtime + devel
- 安装时自动设置环境变量 `GSTREAMER_1_0_ROOT_MSVC_X86_64`
- `pip install pygobject pycairo`

## 快速开始

```python
from gst_streaming import GStreamerConfig, GStreamerObject

cfg = GStreamerConfig()
cfg.vcodec = "auto"  # 自动检测 nvh265enc，否则回退 x265enc
cfg.output_mode = "rtsp_server"  # 或 "rtsp" 推到 MediaMTX
cfg.output_host = "0.0.0.0"
cfg.output_port = 8554
cfg.rtsp_mount = "/stream/cam_front_left"
cfg.output_url = "rtsp://127.0.0.1:8554/stream/cam_front_left"

gst = GStreamerObject(cfg)
gst.initialize_pipe()

# 推流 BGRA 帧
gst.send_frame_in_bytes(bgra_bytes)

gst.destroy_pipe()
```

完整示例见 `demo.py`。

## 运行 demo

`demo.py` 支持两种模式：

```bash
python demo.py --video
python demo.py --carla
```

默认 `--video` 模式会读取：

```text
D:\Navigation\Code\gst\test.mp4
```

默认会启动内置 RTSP Server，监听：

```text
rtsp://127.0.0.1:8554/stream/cam_front_left
```

拉流测试命令：

```bash
ffplay -fflags nobuffer -flags low_delay -framedrop rtsp://127.0.0.1:8554/stream/cam_front_left
```

或：

```bash
gst-launch-1.0 rtspsrc location=rtsp://127.0.0.1:8554/stream/cam_front_left latency=0 drop-on-latency=true buffer-mode=3 ! rtph265depay ! h265parse ! nvh265dec ! d3d11videosink sync=false
```

如果想改回推流到 [MediaMTX](https://github.com/bluenviron/mediamtx)，将 `demo.py` / `GStreamerConfig` 中的输出模式改为：

```python
cfg.output_mode = "rtsp"
cfg.output_url = "rtsp://127.0.0.1:8554/stream/cam_front_left"
```

并先启动 MediaMTX。

## PyInstaller 打包

仓库提供了 `demo.spec`，用于将 `demo.py` 打包为单文件 exe：

```bash
pyinstaller --noconfirm demo.spec
```

`gst_streaming.py` 会在 `import gi` 前执行：

```python
import gstreamer_libs
gstreamer_libs.setup_python_environment()
```

这用于恢复 `pip install gstreamer-bundle` 在冻结程序中的运行时环境。`demo.spec` 同时会收集以下 GStreamer wheel 包：

- `gstreamer_libs`
- `gstreamer_plugins`
- `gstreamer_plugins_libs`
- `gstreamer_plugins_restricted`
- `gstreamer_plugins_gpl`
- `gstreamer_plugins_gpl_restricted`
- `gstreamer_python`
- `gstreamer_ext_runtime`

在没有 NVIDIA GPU 的机器上，GStreamer 插件扫描或硬件编码器探测可能产生 D3D11 / MediaFoundation 相关 warning。若需要无 GPU 机器稳定运行，建议在 spec 中进一步裁剪不需要的硬件相关插件，或将 `vcodec` 固定为软件编码器 `x265enc`。

## 推流模式

| 模式 | 说明 | 测试状态 |
|---|---|---|
| `udp` | RTP over UDP，无连接低延迟 | AI写的，我没试 |
| `tcp` | RTP over TCP，可靠传输 | 同上 |
| `srt` | SRT 协议，低延迟可靠，适合公网 | 同上 |
| `rtsp` | 使用 `rtspclientsink` 推流到 MediaMTX 等 RTSP 服务器 | 已测试 |
| `rtsp_server` | 本进程启动 `gst-rtsp-server`，客户端直接拉流 | 已测试 |

`rtsp_server` 模式中 payloader 必须命名为 `pay0`，这是 `gst-rtsp-server` 的约定，用于把该 payloader 映射为第 0 路 RTSP media track。

编码流 caps 统一显式指定为：

```text
video/x-h265,stream-format=byte-stream,alignment=au
```

或 H.264：

```text
video/x-h264,stream-format=byte-stream,alignment=au
```

`alignment=au` 表示 parser 输出按 access unit（完整访问单元/帧语义）对齐，比默认协商更稳定，尤其适合在 parser 后插入 SEI 再交给 RTP payloader。

## SEI 格式

```
起始码:    00 00 00 01
NAL 头:    4E 01     (H.265 PREFIX_SEI, type=39)
          / 06         (H.264 SEI, type=6)
payloadType: C8        (200, 自定义)
payloadSize: 09
payload:    3B + 8 字节 big-endian uint64 UTC 微秒时间戳
```

发送端会在 parser 的 src pad 后注入 SEI。若使用 `vcodec="auto"`，代码会保存实际选择的编码器，避免 H.265 流误注入 H.264 SEI header。

## Windows 日志说明

使用 `gstreamer-bundle` 的 `gst-launch-1.0` 拉流时，可能看到类似：

```text
giolibproxy.dll: 找不到指定的模块
Failed to load module ... giolibproxy.dll
```

这通常是 GIO proxy 模块的依赖警告，对本机 RTSP 拉流一般无影响。若使用 `d3d11videosink` / `nvh265dec`，还会打印 D3D11、CUDA context 信息，也属于客户端显示/解码侧日志。

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
