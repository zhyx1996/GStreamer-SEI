#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ==============================================================================
# @file      demo.py
# @function  GStreamer 推流测试 —— 支持 Carla 相机和本地视频两种模式
# @usage     python demo.py --carla          # Carla 相机推流
#            python demo.py --video          # 本地视频推流
# ==============================================================================

# 拉流指令：
# ffplay -fflags nobuffer -flags low_delay -framedrop rtsp://127.0.0.1:8554/stream/cam_front_left
# gst-launch-1.0 rtspsrc location=rtsp://127.0.0.1:8554/stream/cam_front_left latency=0 drop-on-latency=true buffer-mode=3 ! rtph265depay ! h265parse ! nvh265dec ! d3d11videosink sync=false

import argparse
import queue
import time
import sys
import os

print("[DEBUG] demo.py: 开始 import gst_streaming...", flush=True)
from gst_streaming import GStreamerConfig, GStreamerObject
print("[DEBUG] demo.py: import gst_streaming OK", flush=True)


# ==================== 配置 ====================
CARLA_HOST = "127.0.0.1"
CARLA_PORT = 3000
CAMERA_WIDTH = 1920
CAMERA_HEIGHT = 1080
CAMERA_FPS = 10
RTSP_SERVER_PORT = 8554
RTSP_MOUNT = "/stream/cam_front_left"
RTSP_URL = "rtsp://127.0.0.1:8554/stream/cam_front_left"
VIDEO_PATH = r"D:\Navigation\Code\gst\test.mp4"
# =============================================


def make_gst_config():
    """创建 GStreamer 配置"""
    cfg = GStreamerConfig()
    cfg.input_width = CAMERA_WIDTH
    cfg.input_height = CAMERA_HEIGHT
    cfg.input_fps = CAMERA_FPS
    cfg.input_pix_fmt = "BGRA"
    cfg.vcodec = "auto"     # 自动检测: NVIDIA GPU → nvh265enc, 否则 → x265enc
    cfg.video_bitrate = "4000k"
    cfg.output_mode = "rtsp_server"  # rtsp_server / rtsp
    cfg.output_host = "0.0.0.0"
    cfg.output_port = RTSP_SERVER_PORT
    cfg.rtsp_mount = RTSP_MOUNT
    cfg.output_url = RTSP_URL
    cfg.label = "gst-demo"
    return cfg


# ==============================================================================
#  模式 1: Carla 相机推流 (自己创建车辆和相机)
# ==============================================================================

def run_carla_mode():
    """Carla 车辆+相机推流模式 (终端键盘手动控制, Speed 模式)
    
    默认参数来自 JSON 配置:
      Vehicle: Lincoln MKZ 2020, 固定位姿, Speed 控制
      Camera1: front_left  (左前侧, 俯仰-38°, 偏航-90°)
      Camera2: top         (车顶, 俯仰-45°, 偏航-90°)
    """
    import carla
    import numpy as np
    import math
    import msvcrt  # Windows 键盘输入

    # ==================== 车辆配置 ====================
    VEHICLE_MODEL = "vehicle.lincoln.mkz_2020"
    VEHICLE_COLOR = "255,0,0"
    VEHICLE_LOCATION = carla.Location(x=221.042, y=-13.0, z=0.1)
    VEHICLE_ROTATION = carla.Rotation(roll=0.0, pitch=0.0, yaw=90.0)

    # Speed 控制参数
    SPEED_MAX_LIMIT = 7.0           # m/s
    SPEED_MIN_LIMIT = 0.0
    MANUAL_DELTA_SPEED = 0.1        # 每次按键速度变化量 (m/s)
    SPEED_KP = 10.0                 # 速度 P 控制器增益

    # 转向参数
    LEFT_STEER_MAX = -70.0          # 度
    RIGHT_STEER_MAX = 70.0
    MANUAL_DELTA_STEER = 5.0        # 每次按键转向角度变化量 (度)

    # ==================== 相机配置 ====================
    # 相机 1: 左前侧 (front_left)
    CAM1_LOCATION = carla.Location(x=3.0, y=-2.1, z=1.9)
    CAM1_ROTATION = carla.Rotation(pitch=-38.0, yaw=-90.0, roll=0.0)
    CAM1_FOV = 120.0
    CAM1_FSTOP = 1.4

    # 相机 2: 车顶 (top)
    CAM2_LOCATION = carla.Location(x=1.0, y=-0.68, z=4.85)
    CAM2_ROTATION = carla.Rotation(pitch=-45.0, yaw=-90.0, roll=0.0)
    CAM2_FOV = 120.0
    CAM2_FSTOP = 1.4

    # 公共相机参数
    CAM_IMAGE_W = 1920
    CAM_IMAGE_H = 1080
    CAM_ISO = 100.0
    CAM_GAMMA = 2.2
    CAM_SENSOR_TICK = 0.1

    # 默认推流使用相机 1 (front_left)
    ACTIVE_CAMERA_INDEX = 0  # 0=front_left, 1=top

    # ---- 初始化 GStreamer ----
    cfg = make_gst_config()
    gst = GStreamerObject(cfg)
    if not gst.initialize_pipe():
        print("[FATAL] GStreamer pipeline 启动失败")
        return

    vehicle = None
    cameras = []
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
        world.tick()

        # ---- 创建车辆 (固定位姿) ----
        vehicle_bp = world.get_blueprint_library().find(VEHICLE_MODEL)
        vehicle_bp.set_attribute("color", VEHICLE_COLOR)
        vehicle_bp.set_attribute("role_name", "car")
        vehicle_transform = carla.Transform(VEHICLE_LOCATION, VEHICLE_ROTATION)
        vehicle = world.spawn_actor(vehicle_bp, vehicle_transform)
        print(f"[demo] 创建车辆: {VEHICLE_MODEL}, pos=({VEHICLE_LOCATION.x}, {VEHICLE_LOCATION.y}, {VEHICLE_LOCATION.z}), yaw={VEHICLE_ROTATION.yaw}")

        # ---- 创建相机列表 ----
        camera_configs = [
            {
                "name": "front_left",
                "location": CAM1_LOCATION,
                "rotation": CAM1_ROTATION,
                "fov": CAM1_FOV,
                "fstop": CAM1_FSTOP,
            },
            {
                "name": "top",
                "location": CAM2_LOCATION,
                "rotation": CAM2_ROTATION,
                "fov": CAM2_FOV,
                "fstop": CAM2_FSTOP,
            },
        ]

        image_queues = []
        for cam_cfg in camera_configs:
            cam_bp = world.get_blueprint_library().find("sensor.camera.rgb")
            cam_bp.set_attribute("image_size_x", str(CAM_IMAGE_W))
            cam_bp.set_attribute("image_size_y", str(CAM_IMAGE_H))
            cam_bp.set_attribute("fov", str(cam_cfg["fov"]))
            cam_bp.set_attribute("fstop", str(cam_cfg["fstop"]))
            cam_bp.set_attribute("iso", str(CAM_ISO))
            cam_bp.set_attribute("gamma", str(CAM_GAMMA))
            cam_bp.set_attribute("sensor_tick", str(CAM_SENSOR_TICK))

            cam_transform = carla.Transform(cam_cfg["location"], cam_cfg["rotation"])
            cam = world.spawn_actor(cam_bp, cam_transform, attach_to=vehicle)
            cameras.append(cam)

            q = queue.Queue()
            cam.listen(q.put)
            image_queues.append(q)
            print(f"[demo] 创建相机 [{cam_cfg['name']}]: id={cam.id}, "
                  f"pos=({cam_cfg['location'].x}, {cam_cfg['location'].y}, {cam_cfg['location'].z}), "
                  f"rot=(p={cam_cfg['rotation'].pitch}, y={cam_cfg['rotation'].yaw}, r={cam_cfg['rotation'].roll})")

        world.tick()

        # Speed 控制状态
        target_speed = 0.0    # 目标速度 (m/s)
        steer_angle = 0.0     # 当前转向角 (度)

        cam_names = [c["name"] for c in camera_configs]
        active_cam = ACTIVE_CAMERA_INDEX

        print("=" * 60)
        print(f"[demo] 开始推流 -> {RTSP_URL} (终端键盘控制模式)")
        print("=" * 60)
        print("控制键:")
        print("  W/S    = 加速/减速 (速度 ±0.1 m/s)")
        print("  A/D    = 左转/右转 (转向 ±5°)")
        print("  Q      = 方向盘回正")
        print("  1/2    = 切换相机 (1=front_left, 2=top)")
        print("  ESC/X  = 退出程序")
        print(f"速度范围: [{SPEED_MIN_LIMIT}, {SPEED_MAX_LIMIT}] m/s")
        print(f"转向范围: [{LEFT_STEER_MAX}, {RIGHT_STEER_MAX}] deg")
        print("=" * 60)

        frame_count = 0
        frame_interval = 1.0 / CAMERA_FPS

        # ---- 主循环 ----
        while True:
            # ---- 终端键盘输入检测 ----
            if msvcrt.kbhit():
                key = msvcrt.getch()
                # 处理特殊键 (方向键等返回 b'\xe0' 或 b'\x00')
                if key in (b'\xe0', b'\x00'):
                    msvcrt.getch()  # 消耗第二个字节
                    continue
                
                key_char = key.decode('utf-8', errors='ignore').lower()
                
                # 退出
                if key_char in ('x', '\x1b'):  # x 或 ESC
                    print("\n[demo] 用户退出")
                    break
                
                # 速度控制
                elif key_char == 'w':
                    target_speed = min(target_speed + MANUAL_DELTA_SPEED, SPEED_MAX_LIMIT)
                    print(f"[控制] 加速 -> 目标速度: {target_speed:.1f} m/s")
                elif key_char == 's':
                    target_speed = max(target_speed - MANUAL_DELTA_SPEED, SPEED_MIN_LIMIT)
                    print(f"[控制] 减速 -> 目标速度: {target_speed:.1f} m/s")
                
                # 转向控制
                elif key_char == 'a':
                    steer_angle = max(steer_angle - MANUAL_DELTA_STEER, LEFT_STEER_MAX)
                    print(f"[控制] 左转 -> 转向角: {steer_angle:.0f}°")
                elif key_char == 'd':
                    steer_angle = min(steer_angle + MANUAL_DELTA_STEER, RIGHT_STEER_MAX)
                    print(f"[控制] 右转 -> 转向角: {steer_angle:.0f}°")
                elif key_char == 'q':
                    steer_angle = 0.0
                    print(f"[控制] 方向盘回正")
                
                # 相机切换
                elif key_char == '1':
                    active_cam = 0
                    print(f"[控制] 切换到相机: {cam_names[active_cam]}")
                elif key_char == '2':
                    active_cam = 1
                    print(f"[控制] 切换到相机: {cam_names[active_cam]}")

            # ---- 速度 P 控制器 → throttle/brake ----
            current_speed = math.sqrt(
                vehicle.get_velocity().x ** 2 +
                vehicle.get_velocity().y ** 2 +
                vehicle.get_velocity().z ** 2
            )
            speed_error = target_speed - current_speed
            throttle_cmd = max(0.0, min(SPEED_KP * speed_error, 1.0))
            brake_cmd = 0.0
            if speed_error < -0.5:
                brake_cmd = min(-speed_error * SPEED_KP * 0.1, 1.0)
                throttle_cmd = 0.0

            # Carla steer 归一化到 [-1, 1]
            steer_norm = max(-1.0, min(steer_angle / abs(LEFT_STEER_MAX), 1.0))

            control = carla.VehicleControl(
                throttle=throttle_cmd,
                steer=steer_norm,
                brake=brake_cmd,
                hand_brake=False,
                reverse=False,
            )
            vehicle.apply_control(control)

            # 推进仿真
            world.tick()

            # 取帧并推流 (使用当前活动相机)
            try:
                image = image_queues[active_cam].get(timeout=1.0)
                gst.send_carla_frame(image)
                frame_count += 1
                # 每 100 帧打印一次状态
                if frame_count % 100 == 0:
                    print(f"[状态] 帧={frame_count} | 速度={current_speed:.1f}/{target_speed:.1f} m/s | 转向={steer_angle:.0f}° | 相机={cam_names[active_cam]}")
            except queue.Empty:
                pass

            # 帧率控制
            time.sleep(frame_interval * 0.5)  # 留一些时间给键盘检测

    except KeyboardInterrupt:
        print("\n[demo] 用户中断")
    finally:
        if vehicle is not None:
            vehicle.destroy()
            print("[demo] 已销毁车辆")
        for cam in cameras:
            cam.destroy()
            print(f"[demo] 已销毁相机 id={cam.id}")
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
