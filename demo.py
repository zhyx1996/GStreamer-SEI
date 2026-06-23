#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ==============================================================================
# @file      demo.py
# @function  GStreamer 推流测试 —— 支持 Carla 相机和本地视频两种模式
# @usage     python demo.py --carla          # Carla 相机推流
#            python demo.py --video          # 本地视频推流
# ==============================================================================

# 拉流指令：
# ffplay -fflags nobuffer -flags low_delay -framedrop rtsp://127.0.0.1:8554/stream
# gst-launch-1.0 rtspsrc location=rtsp://127.0.0.1:8554/stream latency=0 drop-on-latency=true buffer-mode=3 ! rtph265depay ! h265parse ! nvh265dec ! d3d11videosink sync=false

import argparse
import queue
import time
import sys
import os

# 把 test 目录加入 sys.path 以导入 gstreamer 模块
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "test"))
from gstreamer import GStreamerConfig, GStreamerObject


# ==================== 配置 ====================
CARLA_HOST = "127.0.0.1"
CARLA_PORT = 2000
CAMERA_WIDTH = 1920
CAMERA_HEIGHT = 1080
CAMERA_FPS = 10
RTSP_URL = "rtsp://127.0.0.1:8554/stream"
VIDEO_PATH = "input.mp4"
# =============================================


def make_gst_config():
    """创建 GStreamer 配置"""
    cfg = GStreamerConfig()
    cfg.input_width = CAMERA_WIDTH
    cfg.input_height = CAMERA_HEIGHT
    cfg.input_fps = CAMERA_FPS
    cfg.input_pix_fmt = "BGRA"
    cfg.vcodec = "nvh265enc"
    cfg.video_bitrate = "4000k"
    cfg.output_mode = "rtsp"
    cfg.output_url = RTSP_URL
    cfg.label = "gst-demo"
    return cfg


# ==============================================================================
#  模式 1: Carla 相机推流 (自己创建相机)
# ==============================================================================

def run_carla_mode():
    """Carla 相机推流模式"""
    import carla

    cfg = make_gst_config()
    gst = GStreamerObject(cfg)
    if not gst.initialize_pipe():
        print("[FATAL] GStreamer pipeline 启动失败")
        return

    try:
        # 连接 Carla
        client = carla.Client(CARLA_HOST, CARLA_PORT)
        client.set_timeout(10.0)
        world = client.get_world()

        # 同步模式
        settings = world.get_settings()
        settings.synchronous_mode = True
        settings.fixed_delta_seconds = 1.0 / CAMERA_FPS
        world.apply_settings(settings)
        world.tick()  # 同步模式下先发一个 tick 让 server 切模式

        # 创建相机
        bp = world.get_blueprint_library().find("sensor.camera.rgb")
        bp.set_attribute("image_size_x", str(CAMERA_WIDTH))
        bp.set_attribute("image_size_y", str(CAMERA_HEIGHT))
        bp.set_attribute("sensor_tick", str(1.0 / CAMERA_FPS))

        camera = world.spawn_actor(
            bp,
            carla.Transform(carla.Location(x=1.5, z=2.4)),
        )
        print(f"[demo] 创建相机: id={camera.id}")

        # 队列
        image_queue = queue.Queue()
        camera.listen(image_queue.put)
        world.tick()  # 让 Carla 处理 sensor 注册，开始生产第一帧

        # 主循环
        print(f"[demo] 开始推流 -> {RTSP_URL} (Carla 模式)")
        while True:
            world.tick()
            image = image_queue.get()
            gst.send_carla_frame(image)

    except KeyboardInterrupt:
        print("\n[demo] 用户中断")
    finally:
        if 'camera' in dir() and camera is not None:
            camera.destroy()
        settings.synchronous_mode = False
        world.apply_settings(settings)
        gst.destroy_pipe()
        print("[demo] 清理完成")


# ==============================================================================
#  模式 2: 本地视频推流
# ==============================================================================

def run_video_mode():
    """本地视频推流模式"""
    import cv2
    import numpy as np

    cfg = make_gst_config()
    gst = GStreamerObject(cfg)
    if not gst.initialize_pipe():
        print("[FATAL] GStreamer pipeline 启动失败")
        return

    try:
        # 打开视频
        cap = cv2.VideoCapture(VIDEO_PATH)
        if not cap.isOpened():
            print(f"[FATAL] 无法打开视频: {VIDEO_PATH}")
            return

        # 获取视频信息
        video_fps = cap.get(cv2.CAP_PROP_FPS)
        video_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        video_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        print(f"[demo] 视频: {VIDEO_PATH}")
        print(f"[demo] 视频尺寸: {video_w}x{video_h}, FPS: {video_fps:.1f}")

        # 主循环
        print(f"[demo] 开始推流 -> {RTSP_URL} (视频模式)")
        frame_interval = 1.0 / CAMERA_FPS
        frame_count = 0

        while True:
            start_time = time.time()

            ret, frame = cap.read()
            if not ret:
                # 视频结束, 循环播放
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                continue

            # BGR -> BGRA (添加 Alpha 通道)
            frame_bgra = cv2.cvtColor(frame, cv2.COLOR_BGR2BGRA)

            # 如果视频尺寸与配置不符, 需要缩放
            if frame_bgra.shape[1] != CAMERA_WIDTH or frame_bgra.shape[0] != CAMERA_HEIGHT:
                frame_bgra = cv2.resize(frame_bgra, (CAMERA_WIDTH, CAMERA_HEIGHT))

            # 打上本地时间戳 (水印)
            now = time.time()
            local_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
            millis = int((now - int(now)) * 1000)
            timestamp_str = f"{local_time}.{millis:03d}"
            cv2.putText(
                frame_bgra,
                timestamp_str,
                (30, 60),                    # 左上角坐标
                cv2.FONT_HERSHEY_SIMPLEX,    # 字体
                1.5,                         # 字号
                (0, 255, 0, 255),            # 绿色 BGRA
                2,                           # 线宽
                cv2.LINE_AA,
            )

            # 推流
            gst.send_frame_in_bytes(frame_bgra.tobytes())
            frame_count += 1

            # 帧率控制
            elapsed = time.time() - start_time
            if elapsed < frame_interval:
                time.sleep(frame_interval - elapsed)

    except KeyboardInterrupt:
        print("\n[demo] 用户中断")
    finally:
        cap.release()
        gst.destroy_pipe()
        print(f"[demo] 清理完成, 共推送 {frame_count} 帧")


# ==============================================================================
#  入口
# ==============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GStreamer 推流测试")
    parser.add_argument("--carla", action="store_true", help="Carla 相机推流模式")
    parser.add_argument("--video", action="store_true", help="本地视频推流模式 (默认)")
    args = parser.parse_args()

    if args.carla:
        run_carla_mode()
    else:
        # 默认使用 video 模式
        run_video_mode()
