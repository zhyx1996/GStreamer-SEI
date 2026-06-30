#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ==============================================================================
#  Carla 传感器输出 (sensor.camera.rgb)
#  ──────────────────────────────────────────────────────────────────────────
#  raw_data    bytes   BGRA 32-bit packed, 长度 = width × height × 4
#  width       int     图像宽度 (px)
#  height      int     图像高度 (px)
#  timestamp   double  仿真时间 (秒)
#  frame       int     帧序号
# ==============================================================================
#  依赖 (Windows)
#  ──────────────────────────────────────────────────────────────────────────
#  只在python中使用的话，可以直接pip install gstreamer-bundle (python >= 3.9)
#
#  或使用官方安装程序 (Windows)：
#  1. 下载 GStreamer MSVC x86_64 runtime + devel (选 Complete 安装)
#     https://gstreamer.freedesktop.org/download/
#  2. pip install pygobject pycairo
#  3. 环境变量 (MSVC 安装包会自动设 GSTREAMER_1_0_ROOT_MSVC_X86_64)
#     GST_PLUGIN_PATH=<root>\lib\gstreamer-1.0
# ==============================================================================

import os          # os.environ / os.path.join / os.add_dll_directory
import sys         # sys.argv (传给 Gst.init)
import time        # sleep (等待 pipeline 状态切换)
import traceback   # 异常时打印调用栈
import threading   # GLib.MainLoop 运行在 daemon 线程
import collections # deque (SEI 时间戳 FIFO)
import numpy as np # 仅 timestamp overlay 场景用, 不影响模块加载

# 为pyinstaller打包设置环境(仅适用于pip install gstreamer-bundle安装)
# 不打包则可以跳过
import gstreamer_libs
gstreamer_libs.setup_python_environment()

# # 若使用官方安装程序，则在 Python 3.8+ 中，需要显式添加 DLL 搜索路径，否则找不到 gstreamer-1.0.dll
# # pip install gstreamer-bundle 安装 或 python < 3.8 则可以跳过
# if sys.version_info >= (3, 8):
#     gstreamer_root = os.environ.get("GSTREAMER_1_0_ROOT_MSVC_X86_64")
#     if gstreamer_root:
#         dll_path = os.path.join(gstreamer_root, "bin")
#         if os.path.isdir(dll_path):
#             os.add_dll_directory(dll_path)
#             print(f"[DEBUG] gstreamer.py: Added DLL search path: {dll_path}")
#         else:
#             print(f"[WARN] gstreamer.py: DLL path does not exist: {dll_path}")
#     else:
#         print("[WARN] gstreamer.py: GSTREAMER_1_0_ROOT_MSVC_X86_64 not set, may fail to load GStreamer DLLs")

import gi
gi.require_version("Gst", "1.0")     # GStreamer 1.x GI 绑定
gi.require_version("GstApp", "1.0")  # GstApp 绑定 (appsrc 的 Python 接口)
from gi.repository import Gst, GstApp, GLib

# Gst.init 初始化 GStreamer 内部状态: 类型注册 / 插件扫描 / registry 缓存。
# 必须传 sys.argv (新版 pygobject Gst.init(None) 会 TypeError)。
print("[DEBUG] gstreamer.py: 开始 Gst.init...", flush=True)
try:
    Gst.init(sys.argv)
    print("[DEBUG] gstreamer.py: Gst.init OK, 模块加载完成", flush=True)
except Exception as e:
    print(f"[DEBUG] gstreamer.py: Gst.init EXCEPTION: {e}", flush=True)
    traceback.print_exc()
    print("[DEBUG] gstreamer.py: re-raising...", flush=True)
    raise


# ==============================================================================
#  GStreamerConfig — Pipeline 参数配置
# ==============================================================================

class GStreamerConfig:
    """GStreamer pipeline 配置参数。"""

    def __init__(self):
        # ── 输入源 (对应 Carla 相机参数) ──
        self.input_width: int = 1920             # 宽度 (px), 须 = Carla image_size_x
        self.input_height: int = 1080            # 高度 (px), 须 = Carla image_size_y
        self.input_fps: int = 10                 # 帧率, 对应 sensor_tick = 1/fps
        self.input_pix_fmt: str = "BGRA"         # Carla RGB 相机输出格式 (B,G,R,A 各 8bit)

        # ── 编码器 ──
        # vcodec 可选:
        #   "x264enc"   = 软件 H.264 (libx264, 跨平台)
        #   "x265enc"   = 软件 H.265 / HEVC
        #   "nvh264enc" = NVIDIA NVENC 硬件 H.264
        #   "nvh265enc" = NVIDIA NVENC 硬件 H.265
        #   "vah264enc" = VAAPI 硬件 H.264 (Intel/AMD 核显)
        self.vcodec: str = "auto"                  # "auto" → 自动检测 NVIDIA GPU, 无则回退 x265enc
        self.video_bitrate: str = "4000k"        # 目标码率: "4000k" (kbps) 或 "4M" (Mbps)
        self.encoder_preset: str = "ultrafast"   # ultrafast > veryfast > medium > slow
        self.encoder_tune: str = "zerolatency"   # zerolatency = 关 B 帧, 最低延迟

        # ── 输出 (网络 Sink) ──
        # output_mode:
        #   "udp"  = RTP over UDP (无连接, 有丢包风险, 延迟最低)
        #   "tcp"  = RTP over TCP (可靠, 轻微延迟)
        #   "srt"  = SRT 协议 (低延迟+可靠传输, 适合公网)
        #   "rtsp" = 推流到 MediaMTX 等 RTSP 中继服务器
        self.output_mode: str = "rtsp"            # rtsp: 直接推流到 RTSP 服务器
        self.output_host: str = "127.0.0.1"       # RTSP 服务器 IP
        self.output_port: int = 8554              # RTSP 服务器端口
        self.output_url: str = "rtsp://127.0.0.1:8554/stream/cam_front_left"  # RTSP 推流地址
        self.rtsp_mount: str = "/stream/cam_front_left"    # 挂载点路径

        # ── 杂项 ──
        self.label: str = "gst-carla"            # 日志标签, 区分多实例


# ==============================================================================
#  GStreamerObject — 核心推流类
# ==============================================================================

class GStreamerObject:
    """
    GStreamer 视频推流对象

    创建 pipeline 字符串 → Gst.parse_launch()  → appsrc push-buffer

    用法:
        cfg = GStreamerConfig()
        gst = GStreamerObject(cfg)
        if gst.initialize_pipe():
            gst.send_carla_frame(carla_image)    # 循环推流
            gst.destroy_pipe()                   # 清理
    """

    def __init__(self, gst_cfg: GStreamerConfig):
        self._gst_cfg = gst_cfg                  # 传引用，外部修改后 restart 生效
        self._pipeline: Gst.Pipeline = None      # Gst.Pipeline 顶层容器
        self._appsrc: GstApp.AppSrc = None       # 数据入口 (按 name="mysrc" 查找)
        self._main_loop: GLib.MainLoop = None    # GLib 事件循环, 处理信号/回调
        self._loop_thread: threading.Thread = None  # 后台线程, 运行 main_loop.run()
        self._ready: bool = False                # pipeline 是否就绪 (可 push 帧)
        self._sei_ts_queue: collections.deque = collections.deque(maxlen=5)  # FIFO, zerolatency保证1:1
        self._sei_lock = threading.Lock()         # 保护 _sei_ts_queue 的跨线程读写

    # ==========================================================================
    #  Pipeline 字符串构建 —— 拼字符串, 一行 Gst.parse_launch() 解析
    # ==========================================================================

    @staticmethod
    def _is_h265(vcodec: str) -> bool:
        """判定编码器名称是否属于 H.265 / HEVC 系列。"""
        return "265" in vcodec or "hevc" in vcodec.lower()

    @staticmethod
    def _parse_bitrate(bitrate_str: str) -> float:
        """
        解析码率字符串 → kbps 数值。
        "4000k" → 4000.0   "4M" → 4000.0   "2000" → 2000.0
        """
        s = str(bitrate_str).strip().lower()
        if s.endswith("k"):   return float(s[:-1])          # 去 k
        if s.endswith("m"):   return float(s[:-1]) * 1000   # M → k
        return float(s)                                     # 假定 kbps

    @staticmethod
    def _detect_best_encoder() -> str:
        """自动检测最佳编码器: GStreamer 注册表有 nvenc → nvh265enc, 无 → x265enc"""
        # 直接查 GStreamer 注册表，避免 subprocess 调用 nvidia-smi (打包后可能卡死)
        registry = Gst.Registry.get()
        if registry.check_feature_version("nvh265enc", 0, 0, 0):
            print("[gstreamer] 检测到 nvh265enc 插件, 使用硬件编码")
            return "nvh265enc"
        print("[gstreamer] 未检测到 nvh265enc, 回退到软件编码 x265enc")
        return "x265enc"

    def _build_encoder_segment(self, vcodec: str) -> str:
        """
        构建编码器 element 参数字符串。

        不同编码器参数名不同, 需分开处理:
			软件 x264/x265: tune / speed-preset / bitrate / key-int-max
			NVENC:           preset / rc-mode / bitrate / gop-size
			VAAPI:           rate-control / bitrate / key-int-max

        Args:
            vcodec: 已解析的编码器名称 (由 _detect_best_encoder() 或配置直接指定)
        """
        br = int(self._parse_bitrate(self._gst_cfg.video_bitrate))  # kbps
        gop = self._gst_cfg.input_fps  # GOP（两个 I 帧之间的帧数） = 帧率, 即每秒一个 I 帧

        encoder_map = {
            # 软件 H.264 — key-int-max=GOP 长度, zerolatency 调优 (默认 VBR)
            "x264enc":   f"x264enc tune=zerolatency speed-preset=ultrafast bitrate={br} key-int-max={gop} bframes=0",
            # 软件 H.265 (默认 VBR)
            "x265enc":   f"x265enc tune=zerolatency speed-preset=ultrafast bitrate={br} key-int-max={gop} bframes=0",
            # NVIDIA H.264 — NVENC VBR 模式: zerolatency=true, bframes=0
            "nvh264enc": f"nvh264enc bitrate={br} preset=low-latency-hq rc-mode=vbr gop-size={gop} zerolatency=true bframes=0",
            # NVIDIA H.265 — NVENC VBR 模式
            "nvh265enc": f"nvh265enc bitrate={br} preset=low-latency-hq rc-mode=vbr gop-size={gop} zerolatency=true bframes=0",
            # VAAPI H.264 — VBR 模式
            "vah264enc": f"vah264enc bitrate={br} rate-control=vbr key-int-max={gop}",
        }
        return encoder_map.get(vcodec, vcodec)  # 找不到则原样返回 (允许自定义字符串)

    def _build_output_segment(self) -> str:
        """
        根据 output_mode 构建 sink element 字符串。
        sync=false: 不按时钟阻塞, 立即发送 (低延迟)。
        """
        m = self._gst_cfg.output_mode
        h = self._gst_cfg.output_host
        p = self._gst_cfg.output_port
        if m == "udp":   return f"udpsink host={h} port={p} sync=false"
        if m == "tcp":   return f"tcpclientsink host={h} port={p} sync=false"
        if m == "srt":
            url = self._gst_cfg.output_url or f"srt://{h}:{p}"
            return f"srtsink uri={url} sync=false"
        if m == "rtsp":
            url = self._gst_cfg.output_url or f"rtsp://{h}:{p}{self._gst_cfg.rtsp_mount}"
            return f"rtspclientsink location={url} latency=0 protocols=tcp rtx-time=200"
        raise ValueError(f"Unsupported output_mode: {m}")

    def _build_pipeline_string(self) -> str:
        """
        构建完整 GStreamer pipeline 字符串。
        """
        w, h, fps = self._gst_cfg.input_width, self._gst_cfg.input_height, self._gst_cfg.input_fps
        pix = self._gst_cfg.input_pix_fmt
        vcodec = self._gst_cfg.vcodec
        if vcodec == "auto":
            vcodec = self._detect_best_encoder()

        # 根据编码器类型选择对应参数 (H.265 vs H.264)
        is_h265 = self._is_h265(vcodec)
        parser = "h265parse name=parser config-interval=-1" if is_h265 else "h264parse name=parser config-interval=-1"
        stream_caps = "video/x-h265,stream-format=byte-stream" if is_h265 else "video/x-h264,stream-format=byte-stream"
        payloader = "rtph265pay config-interval=1 pt=96" if is_h265 else "rtph264pay config-interval=1 pt=96"

        # NVENC 硬件编码器需要 NV12 格式, 软件编码器用 I420
        enc_fmt = "NV12" if "nv" in vcodec.lower() else "I420"

        # 构建编码器段 (复用已解析的 vcodec)
        encoder_seg = self._build_encoder_segment(vcodec)

        # RTSP 模式不需要 RTP payloader (rtspclientsink 自动处理)
        if self._gst_cfg.output_mode == "rtsp":
            return (
                f"appsrc name=mysrc is-live=true block=true format=time "
                f"caps=video/x-raw,format={pix},width={w},height={h},framerate={fps}/1 ! "
                f"videoconvert ! video/x-raw,format={enc_fmt} ! "
                f"{encoder_seg} ! "
                f"{parser} ! {stream_caps} ! "
                f"{self._build_output_segment()}"
            )
        else:
            return (
                f"appsrc name=mysrc is-live=true block=true format=time "
                f"caps=video/x-raw,format={pix},width={w},height={h},framerate={fps}/1 ! "
                f"videoconvert ! video/x-raw,format={enc_fmt} ! "
                f"{encoder_seg} ! "
                f"{parser} ! "
                f"{payloader} ! "
                f"{self._build_output_segment()}"
            )

    # ==========================================================================
    #  生命周期: 启动 pipeline
    # ==========================================================================

    def initialize_pipe(self) -> bool:
        """
        构建 pipeline → 启动 GLib 主循环 → 设为 PLAYING。

        步骤:
			1. _build_pipeline_string() 生成字符串
			2. Gst.parse_launch() 创建所有 element 并连接 pad
			3. 按 name="mysrc" 取 appsrc
			4. 挂载 bus 监听 (ERROR / EOS / WARNING)
			5. daemon 线程启动 GLib.MainLoop (处理 GStreamer 事件)
			6. set_state(PLAYING) 启动 pipeline

        Returns: True 成功 / False 失败
        """
        try:
            pipeline_str = self._build_pipeline_string()
            print(f"[{self._gst_cfg.label}] pipeline:\n  {pipeline_str}")

            # 一行解析 string → Gst.Pipeline
            self._pipeline = Gst.parse_launch(pipeline_str)
            # 按 name 获取 appsrc
            self._appsrc = self._pipeline.get_by_name("mysrc")
            if not self._appsrc:
                raise RuntimeError("appsrc 'mysrc' not found")

            # 挂载 SEI 注入 probe 到 parser 的 src pad
            # 在 parser 处理完码流之后再注入 SEI，避免 parser 截断 SEI NAL
            parser = self._pipeline.get_by_name("parser")
            if parser:
                src_pad = parser.get_static_pad("src")
                if src_pad:
                    src_pad.add_probe(Gst.PadProbeType.BUFFER, self._on_encoder_output, None)
                    print(f"[{self._gst_cfg.label}] SEI probe attached to parser src pad")

            # 监听 bus (异步, GLib 线程触发)
            bus = self._pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect("message", self._on_bus_message)

            # daemon 线程运行 GLib 主循环
            self._main_loop = GLib.MainLoop.new(None, False)
            self._loop_thread = threading.Thread(
                target=self._main_loop.run,
                name=f"gst-loop-{self._gst_cfg.label}",
                daemon=True,
            )
            self._loop_thread.start()

            # NULL → READY → PAUSED → PLAYING
            if self._pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("Failed to set PLAYING")
            time.sleep(0.5)  # 等异步状态切换完成

            self._ready = True
            print(  f"[{self._gst_cfg.label}] PLAYING, "
                    f"-> {self._gst_cfg.output_host}:{self._gst_cfg.output_port} "
                    f"({self._gst_cfg.output_mode})")
            return True
        except Exception as e:
            traceback.print_exc()
            print(f"[{self._gst_cfg.label}] initialize_pipe failed: {e}")
            self._cleanup_internal()
            return False

    # ==========================================================================
    #  发送帧 (对外接口)
    # ==========================================================================

    def send_frame_in_bytes(self, data: bytes) -> None:
        """
        发送一帧原始像素 (bytes)。

        data 布局须与 input_pix_fmt 一致 (BGRA: width×height×4 字节)
        Gst.Buffer.new_wrapped() 零拷贝 → appsrc.emit("push-buffer") 推入 pipeline
        pipeline 未就绪时静默丢弃
        """
        if not self._ready or self._appsrc is None:
            return
        try:
            buf = Gst.Buffer.new_wrapped(data)             # 零拷贝, 共享内存
            ret = self._appsrc.emit("push-buffer", buf)    # 推入 pipeline
            if ret != Gst.FlowReturn.OK:                   # 非 OK → 下游出错
                print(f"[WARN] {self._gst_cfg.label} push-buffer: {ret}")
        except Exception as e:
            print(f"[ERROR] {self._gst_cfg.label} send_frame_in_bytes: {e}")

    def send_carla_frame(self, carla_image) -> None:
        """
        发送 Carla 传感器图像。

        直接取 carla_image.raw_data (BGRA bytes), 不经 numpy 编解码。

        carla_image 属性:
			raw_data   (bytes)   BGRA 像素
			width      (int)     须 = input_width
			height     (int)     须 = input_height
			timestamp  (double)  仿真时间戳 (秒, 用于 SEI 注入)
			frame      (int)     帧序号
        """
        if not self._ready or self._appsrc is None:
            return
        try:
            buf = Gst.Buffer.new_wrapped(carla_image.raw_data)
            # FIFO 入队时间戳 (微秒), probe 按帧顺序出队
            ts_us = int(carla_image.timestamp * 1_000_000)
            with self._sei_lock:
                self._sei_ts_queue.append(ts_us)
            print(f"[{self._gst_cfg.label}] Carla ts={carla_image.timestamp:.6f}s  frame={carla_image.frame}  SEI_us={ts_us}")
            ret = self._appsrc.emit("push-buffer", buf)
            if ret != Gst.FlowReturn.OK:
                print(f"[WARN] {self._gst_cfg.label} push-buffer: {ret}")
        except Exception as e:
            print(f"[ERROR] {self._gst_cfg.label} send_carla_frame: {e}")

    # ==========================================================================
    #  内部: SEI 注入 & Bus 消息处理 & 资源清理
    # ==========================================================================

    def _inject_sei(self, buffer: Gst.Buffer) -> Gst.Buffer:
        """
        在编码后的 buffer 前插入 SEI NAL (H.264/H.265)。
        
        FIFO 队列出队取时间戳: zerolatency+bframes=0 保证 push-buffer 与 probe 1:1 对应。
        
        SEI 格式 (匹配接收端 parseSei 解析):
            start code: 00 00 00 01
            NAL header:
                H.265: 4E 01 (prefix SEI, type=39, 2字节)
                H.264: 06    (SEI, type=6, 1字节)
            payloadType: C8 (200, 自定义)
            payloadSize: 09
            payload: 3B (';') + 8字节 big-endian uint64 UTC 微秒时间戳
        """
        # FIFO 出队: zerolatency+bframes=0 保证 push-buffer 与 probe 1:1
        with self._sei_lock:
            ts_us = self._sei_ts_queue.popleft() if self._sei_ts_queue else 0
        print(f"[{self._gst_cfg.label}] probe SEI us={ts_us}  queue_left={len(self._sei_ts_queue)}")

        is_h265 = self._is_h265(self._gst_cfg.vcodec)

        # 构造 SEI payload (RBSP, 需做防竞争处理)
        # HACK: 理应对NAL整体做防竞争处理, 但本SEI中payload之外不会出现起始码
        # payload = 0x3B prefix + 8字节 big-endian uint64 时间戳
        sei_payload = bytearray([
            0x3B,                    # ';' prefix
            (ts_us >> 56) & 0xFF,    # 8字节 big-endian uint64
            (ts_us >> 48) & 0xFF,
            (ts_us >> 40) & 0xFF,
            (ts_us >> 32) & 0xFF,
            (ts_us >> 24) & 0xFF,
            (ts_us >> 16) & 0xFF,
            (ts_us >> 8) & 0xFF,
            ts_us & 0xFF,
        ])

        # emulation prevention: 防止 payload 中出现起始码
        # 标准算法: 追踪已输出的连续零计数，当 count==2 且当前字节 ≤ 0x03 时，
        # 先插 03（将 00 00 {0,1,2,3} 变为 00 00 03 {0,1,2,3}），插完后零计数器归零
        ebsp_payload = bytearray()
        zero_count = 0
        for b in sei_payload:
            # 已经连续出现两个 0x00，且当前字节会导致竞争
            if zero_count == 2 and b <= 0x03:
                ebsp_payload.append(0x03) # 插入防竞争字节
                zero_count = 0 # 插入 0x03 后，连续 0x00 的计数重置
            # 写入当前字节
            ebsp_payload.append(b)
            # 更新连续 0x00 的计数器
            if b == 0x00:
                zero_count += 1
            else:
                zero_count = 0

        # 构造 SEI NAL
        sei_nal = bytearray([
            0x00, 0x00, 0x00, 0x01,  # start code
        ])
        if is_h265:
            sei_nal += bytearray([0x4E, 0x01])  # H.265 NAL header (type=39, layer=0, tid=1)
        else:
            sei_nal += bytearray([0x06])         # H.264 NAL header (type=6)
        sei_nal += bytearray([0xC8])             # payloadType = 200
        sei_nal += bytearray([len(sei_payload)]) # payloadSize (RBSP 大小, 固定 9)
        sei_nal += ebsp_payload                  # 已做防竞争处理的 payload (EBSP)

        # 创建 SEI buffer
        sei_buf = Gst.Buffer.new_allocate(None, len(sei_nal), None)
        sei_buf.fill(0, sei_nal)

        # 合并: SEI + 原 buffer
        merged = Gst.Buffer.append(sei_buf, buffer)
        # append 产生新 buffer, 元数据不会自动继承, 需手动复制
        # size=0: 不复制内存 (SEI 已 append), 只复制 pts/dts/duration/offset
        Gst.Buffer.copy_into(merged, buffer, Gst.BufferCopyFlags.TIMESTAMPS | Gst.BufferCopyFlags.FLAGS, 0, 0)

        return merged

    def _on_encoder_output(self, pad: Gst.Pad, info: Gst.PadProbeInfo, udata) -> Gst.PadProbeReturn:
        """
        encoder 输出 probe 回调, 在编码后的 buffer 前注入 SEI。

        在 parser 的 sink pad 上拦截每个 buffer, 读取 _last_timestamp_us,
        构造 SEI NAL 并插入到 buffer 前面。
        """
        buffer = info.get_buffer()
        new_buf = self._inject_sei(buffer)
        info.set_buffer(new_buf)
        return Gst.PadProbeReturn.OK

    def _on_bus_message(self, bus, msg):
        """
        GStreamer bus 消息回调 (GLib 主循环线程中调用)。

        ERROR   → 打印 + _ready=False
        EOS     → 打印 + _ready=False
        WARNING → 打印, 不影响状态
        """
        t = msg.type
        if t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print(f"[ERROR] {self._gst_cfg.label} bus: {err.message}  ({dbg})")
            self._ready = False
        elif t == Gst.MessageType.EOS:
            print(f"[INFO] {self._gst_cfg.label} bus: EOS")
            self._ready = False
        elif t == Gst.MessageType.WARNING:
            err, dbg = msg.parse_warning()
            print(f"[WARN] {self._gst_cfg.label} bus: {err.message}")

    def _cleanup_internal(self):
        """内部清理, 安全可重入。"""
        self._ready = False
        # pipeline → NULL: 释放编码器上下文、网络连接等
        # 用线程 + 超时防止阻塞 (rtspclientsink 网络超时可能很久)
        if self._pipeline:
            def _set_null():
                try:
                    self._pipeline.set_state(Gst.State.NULL)
                except Exception:
                    pass
            t = threading.Thread(target=_set_null, daemon=True)
            t.start()
            t.join(timeout=2.0)  # 最多等 2 秒
            if t.is_alive():
                print(f"[{self._gst_cfg.label}] WARNING: set_state(NULL) timeout, forcing...")
            self._pipeline = None
        self._appsrc = None
        # 退出 GLib 主循环: 让后台线程从 main_loop.run() 返回
        if self._main_loop:
            try:
                self._main_loop.quit()
            except Exception:
                pass
            self._main_loop = None
        self._loop_thread = None

    def destroy_pipe(self) -> None:
        """
        关闭 pipeline (快速退出, 不发 EOS)。

        1. 标记 _ready=False 阻止继续 push
        2. _cleanup_internal() → 带超时地释放所有资源
        """
        if not self._pipeline:
            return
        print(f"[{self._gst_cfg.label}] destroy_pipe...")
        self._cleanup_internal()
        print(f"[{self._gst_cfg.label}] destroy_pipe: done.")

    @property
    def is_ready(self) -> bool:
        """pipeline 是否就绪 (可 push 帧)。"""
        return self._ready
