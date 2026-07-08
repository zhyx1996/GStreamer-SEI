#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CARLA 相机 RTSP 推流工具。

功能概述：
  1. 连接 CARLA 仿真服务器，根据配置找到目标车辆。
  2. 在车辆上挂载多个 RGB 相机传感器，配置分辨率、FOV、帧率等参数。
  3. 实时采集相机图像帧，通过 GStreamer 管道编码并以 RTSP 协议推流。
  4. 支持多路相机同时推流，每路可独立配置编码参数和挂载点。

输入：YAML 配置文件（默认当前目录 config.yaml），包含 CARLA 连接、车辆匹配、
      相机传感器列表、RTSP 推流参数等配置。
输出：RTSP 视频流，推流地址形如 rtsp://<host>:<port>/stream/<camera_name>
"""

import argparse
import queue
import sys
import time
from pathlib import Path

import yaml

from gst_streaming import GStreamerConfig, GStreamerObject


def default_config_path():
    """
    获取默认的 YAML 配置文件路径。

    查找优先级：
      1. 当前工作目录下的 config.yaml
      2. PyInstaller 打包后的可执行文件同目录下的 config.yaml
      3. 本脚本同目录下的 config.yaml（开发模式回退）

    Returns:
        Path: 找到的配置文件路径（不保证文件存在）。
    """
    # 优先使用工作目录下的 config.yaml
    local_path = Path.cwd() / "config.yaml"
    if local_path.exists():
        return local_path
    # PyInstaller 打包后，sys.frozen 为 True
    if getattr(sys, "frozen", False):
        exe_path = Path(sys.executable).with_name("config.yaml")
        if exe_path.exists():
            return exe_path
    # 开发环境回退：脚本所在目录
    return Path(__file__).with_name("config.yaml")


def load_config(path):
    """
    从指定路径加载 YAML 配置文件。

    Args:
        path: YAML 文件路径（字符串或 Path 对象）。

    Returns:
        dict: 解析后的配置字典。
    """
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def make_gst_config(stream_cfg, camera_cfg):
    """
    根据流配置和相机配置构造 GStreamerConfig 对象。

    Args:
        stream_cfg: 流配置字典（包含编码、RTSP 等参数）。
        camera_cfg: 相机传感器配置字典（包含分辨率、sensor_tick 等）。

    Returns:
        GStreamerConfig: 填充好参数的 GStreamer 配置对象。
    """
    # 图像分辨率：优先相机配置，其次流配置，最后默认 1920x1080
    image_w = int(camera_cfg.get("image_size_x", stream_cfg.get("input_width", 1920)))
    image_h = int(camera_cfg.get("image_size_y", stream_cfg.get("input_height", 1080)))

    # 帧率：由 sensor_tick 反算，sensor_tick 表示传感器每帧间隔（秒）
    sensor_tick = float(camera_cfg.get("sensor_tick", 0.1))
    fps = int(round(1.0 / sensor_tick)) if sensor_tick > 0 else int(stream_cfg.get("input_fps", 10))

    # RTSP 挂载路径：默认为 /stream/<role_name>
    mount = stream_cfg.get("mount")
    if not mount:
        role_name = camera_cfg.get("role_name") or camera_cfg.get("name") or f"camera_{camera_cfg.get('id', 0)}"
        mount = f"/stream/{role_name}"
    # 确保挂载路径以 / 开头
    if not mount.startswith("/"):
        mount = f"/{mount}"

    cfg = GStreamerConfig()
    cfg.input_width = image_w
    cfg.input_height = image_h
    cfg.input_fps = fps
    cfg.input_pix_fmt = stream_cfg.get("input_pix_fmt", "BGRA")          # CARLA 相机输出为 BGRA 格式
    cfg.vcodec = stream_cfg.get("vcodec", "auto")                        # 编码器：auto 表示自动选择
    cfg.video_bitrate = stream_cfg.get("video_bitrate", "4000k")         # 视频码率
    cfg.output_mode = stream_cfg.get("output_mode", "rtsp_server")       # 输出模式：RTSP 服务器
    cfg.output_host = stream_cfg.get("output_host", "0.0.0.0")           # RTSP 监听地址
    cfg.output_port = int(stream_cfg.get("output_port", 8554))            # RTSP 监听端口
    cfg.rtsp_mount = mount                                                # RTSP 挂载点
    cfg.output_url = stream_cfg.get("output_url") or f"rtsp://127.0.0.1:{cfg.output_port}{mount}"
    cfg.label = stream_cfg.get("label") or f"gst-{camera_cfg.get('role_name', camera_cfg.get('id', 'camera'))}"
    return cfg


def put_latest(q, item):
    """
    将最新帧放入队列，丢弃旧帧（保持队列中始终只有最新一帧）。

    用于帧率匹配：当采集速度 > 推流速度时，只保留最新帧，避免队列堆积。

    Args:
        q: queue.Queue 对象，maxsize 通常为 1。
        item: 要放入的最新的图像帧。
    """
    try:
        q.get_nowait()           # 尝试取出旧帧，丢弃
    except queue.Empty:
        pass                     # 队列为空，无需丢弃
    try:
        q.put_nowait(item)       # 放入新帧
    except queue.Full:
        pass                     # 极小概率：刚清空又被填满，忽略


def actor_matches_vehicle(actor, vehicle_cfg):
    """
    判断 CARLA 中的 actor 是否匹配配置中描述的车辆。

    匹配规则：
      1. actor.type_id 必须以 "vehicle." 开头。
      2. 若配置了 model，则 type_id 必须完全一致。
      3. 若配置了 role_name，则 actor 属性 role_name 必须一致。

    Args:
        actor: CARLA actor 对象。
        vehicle_cfg: 车辆配置字典，可包含 model、role_name 等字段。

    Returns:
        bool: 是否匹配。
    """
    # 非车辆 actor 直接排除
    if not actor.type_id.startswith("vehicle."):
        return False

    # 车型匹配（如 "vehicle.tesla.model3"）
    model = vehicle_cfg.get("model")
    if model and actor.type_id != model:
        return False

    # role_name 匹配（CARLA 中通过蓝图属性设置的角色名）
    role_name = vehicle_cfg.get("role_name")
    if role_name and actor.attributes.get("role_name") != role_name:
        return False

    return True


def find_vehicle(world, vehicle_cfg):
    """
    在 CARLA 世界中查找目标车辆。

    查找策略：
      1. 若指定了 actor_id / id，直接用 ID 查找。
      2. 否则按 model + role_name 匹配所有车辆 actor，匹配到唯一一辆时返回。
      3. 匹配到多辆时抛出异常并列出所有候选，提示用户填写 actor_id 消歧。

    Args:
        world: CARLA world 对象。
        vehicle_cfg: 车辆配置字典。

    Returns:
        carla.Actor: 匹配到的车辆 actor。

    Raises:
        RuntimeError: 找不到或匹配到多辆时。
    """
    actors = world.get_actors()

    # 策略 1：通过 actor_id 直接查找
    vehicle_id = vehicle_cfg.get("actor_id") or vehicle_cfg.get("id")
    if vehicle_id is not None:
        actor = actors.find(int(vehicle_id))
        if actor and actor.type_id.startswith("vehicle."):
            return actor
        raise RuntimeError(f"找不到 vehicle actor id={vehicle_id}")

    # 策略 2：按 model + role_name 模糊匹配
    matched = [a for a in actors if actor_matches_vehicle(a, vehicle_cfg)]
    if len(matched) == 1:
        return matched[0]
    if not matched:
        raise RuntimeError(f"找不到匹配车辆: role_name={vehicle_cfg.get('role_name')!r}, model={vehicle_cfg.get('model')!r}")

    # 匹配到多辆：列出候选，提示用户指定 actor_id
    desc = ", ".join(
        f"id={a.id}, type={a.type_id}, role_name={a.attributes.get('role_name')}" for a in matched
    )
    raise RuntimeError(f"匹配到多辆车，请在配置里填写 actor_id。候选: {desc}")


def make_sensor_callback(q):
    """
    创建相机传感器回调函数（闭包），将最新图像帧放入指定队列。

    CARLA 相机通过 listen(callback) 注册回调，每产生一帧就调用一次。
    这里将回调绑定到一个固定队列，供主循环取出送入 GStreamer。

    Args:
        q: 用于存储最新帧的 queue.Queue 对象。

    Returns:
        callable: 传感器回调函数，签名为 (carla.Image) -> None。
    """
    def _on_sensor_image(image):
        put_latest(q, image)

    return _on_sensor_image


def make_transform(carla, cfg):
    """
    根据配置字典构造 CARLA Transform（位置 + 旋转）。

    用于设定传感器在车辆上的相对安装位置和朝向。

    Args:
        carla: carla 模块。
        cfg: 包含 location 和 rotation 子字典的配置。

    Returns:
        carla.Transform: CARLA 坐标变换对象。
    """
    loc = cfg.get("location", {})
    rot = cfg.get("rotation", {})
    return carla.Transform(
        carla.Location(
            x=float(loc.get("x", 0.0)),
            y=float(loc.get("y", 0.0)),
            z=float(loc.get("z", 0.0)),
        ),
        carla.Rotation(
            pitch=float(rot.get("pitch", 0.0)),
            yaw=float(rot.get("yaw", 0.0)),
            roll=float(rot.get("roll", 0.0)),
        ),
    )


def configure_camera_blueprint(cam_bp, cam_cfg):
    """
    根据配置设置相机蓝图的属性。

    CARLA 中传感器通过 Blueprint（蓝图）创建，蓝图属性控制分辨率、
    视场角、采样间隔等参数。此函数遍历已知属性名并将其设置到蓝图上，
    None 值跳过（保留 CARLA 默认值）。

    Args:
        cam_bp: CARLA 相机蓝图对象。
        cam_cfg: 相机配置字典。
    """
    # 属性名 → 配置键 的映射，None 值跳过不设置
    attr_map = {
        "role_name": cam_cfg.get("role_name"),       # 角色名，用于 identify
        "image_size_x": cam_cfg.get("image_size_x"), # 图像宽度（像素）
        "image_size_y": cam_cfg.get("image_size_y"), # 图像高度（像素）
        "fov": cam_cfg.get("fov"),                   # 水平视场角（度）
        "fstop": cam_cfg.get("fstop"),               # 光圈大小
        "iso": cam_cfg.get("iso"),                    # ISO 感光度
        "gamma": cam_cfg.get("gamma"),               # Gamma 校正值
        "sensor_tick": cam_cfg.get("sensor_tick"),   # 传感器采样间隔（秒）
    }
    for key, value in attr_map.items():
        if value is not None:
            cam_bp.set_attribute(key, str(value))


def main():
    """
    主函数：解析命令行参数，初始化 CARLA 相机并启动 RTSP 推流主循环。

    执行流程：
      1. 解析 --config 命令行参数，加载 YAML 配置。
      2. 连接 CARLA 仿真服务器，查找目标车辆。
      3. 为每个启用的相机创建 GStreamer RTSP 管道。
      4. 在 CARLA 世界中生成相机传感器，挂载到车辆上。
      5. 进入主循环：逐帧取图 → 送入 GStreamer 管道 → RTSP 推流。
      6. Ctrl+C 或异常退出时，销毁相机并清理 GStreamer 管道。
    """
    # --- 命令行参数解析 ---
    parser = argparse.ArgumentParser(description="读取配置，将 CARLA 相机挂载到现有车辆并推 RTSP")
    parser.add_argument("--config", default=str(default_config_path()), help="YAML 配置文件路径")
    args = parser.parse_args()

    import carla

    # --- 加载配置文件 ---
    config = load_config(args.config)
    carla_cfg = config.get("carla", {})           # CARLA 服务器连接配置
    vehicle_cfg = config.get("vehicle", {})       # 目标车辆匹配配置
    cameras_cfg = [c for c in config.get("cameras", []) if c.get("enable", True)]  # 仅保留启用的相机
    stream_defaults = config.get("stream", {})    # 流媒体默认配置（每路相机可覆盖）

    if not cameras_cfg:
        raise RuntimeError("配置里没有启用的相机")

    # --- 连接 CARLA 服务器 ---
    client = carla.Client(carla_cfg.get("host", "127.0.0.1"), int(carla_cfg.get("port", 3000)))
    client.set_timeout(float(carla_cfg.get("timeout", 10.0)))
    world = client.get_world()

    # --- 查找目标车辆 ---
    vehicle = find_vehicle(world, vehicle_cfg)
    print(
        f"[carla-camera] 使用车辆: id={vehicle.id}, type={vehicle.type_id}, "
        f"role_name={vehicle.attributes.get('role_name')}, label={vehicle_cfg.get('label')}"
    )

    # 资源列表，用于 finally 清理
    gst_streams = []      # GStreamer 流对象列表
    cameras = []          # CARLA 相机 sensor actor 列表
    image_queues = []     # 每路相机对应的帧队列

    try:
        # --- 阶段 1：初始化 GStreamer 管道（每路相机一个） ---
        for cam_cfg in cameras_cfg:
            # 合并默认流配置和相机专属流配置
            stream_cfg = dict(stream_defaults)
            stream_cfg.update(cam_cfg.get("stream", {}))
            gst = GStreamerObject(make_gst_config(stream_cfg, cam_cfg))
            if not gst.initialize_pipe():
                raise RuntimeError(f"GStreamer 初始化失败: {cam_cfg.get('role_name') or cam_cfg.get('id')}")
            gst_streams.append(gst)
            print(f"[carla-camera] RTSP[{cam_cfg.get('role_name') or cam_cfg.get('id')}]: {gst._gst_cfg.output_url}")

        # --- 阶段 2：在 CARLA 中生成相机传感器并挂载 ---
        blueprint_library = world.get_blueprint_library()
        for cam_cfg in cameras_cfg:
            # 查找传感器蓝图（默认 RGB 相机）
            sensor_type = cam_cfg.get("sensor_type", "sensor.camera.rgb")
            cam_bp = blueprint_library.find(sensor_type)
            configure_camera_blueprint(cam_bp, cam_cfg)

            # 创建相机 actor 并挂载到车辆上
            cam = world.spawn_actor(cam_bp, make_transform(carla, cam_cfg), attach_to=vehicle)
            # 为每路相机创建独立队列（maxsize=1，只保留最新帧）
            q = queue.Queue(maxsize=1)
            cam.listen(make_sensor_callback(q))

            cameras.append(cam)
            image_queues.append(q)
            print(f"[carla-camera] 已挂载相机: id={cam.id}, role_name={cam_cfg.get('role_name')}")

        # --- 阶段 3：主推流循环 ---
        frame_count = 0
        print("[carla-camera] 开始推流，Ctrl+C 退出")
        while True:
            stats = []
            for idx, cam_cfg in enumerate(cameras_cfg):
                name = cam_cfg.get("role_name") or cam_cfg.get("id") or idx

                # 从队列取出最新帧（1 秒超时，超时则记录并跳过）
                get_t0 = time.perf_counter()
                try:
                    image = image_queues[idx].get(timeout=1.0)
                except queue.Empty:
                    stats.append(f"{name}: 取图超时")
                    continue
                get_ms = (time.perf_counter() - get_t0) * 1000.0

                # 送入 GStreamer 管道编码推流
                send_t0 = time.perf_counter()
                gst_streams[idx].send_carla_frame(image)
                send_ms = (time.perf_counter() - send_t0) * 1000.0
                stats.append(f"{name}: 帧={image.frame} 取图={get_ms:.3f}ms 推流={send_ms:.3f}ms")

            frame_count += 1
            # 调试用帧统计输出（默认关闭，开启可观察每帧延迟）
            # print(f"[carla-camera] 已推帧={frame_count} " + " | ".join(stats), flush=True)

    except KeyboardInterrupt:
        print(f"\n[carla-camera] 用户中断，共推帧={frame_count}")
    except Exception:
        import traceback
        traceback.print_exc(file=sys.stderr)
    finally:
        # --- 清理资源 ---
        # 销毁 CARLA 相机传感器
        for cam in cameras:
            try:
                cam.stop()          # 停止传感器回调
                cam.destroy()       # 从世界中移除
            except Exception:
                pass
        cameras.clear()

        # 停止 GStreamer 管道
        for gst in gst_streams:
            try:
                gst.stop_pipe()
            except Exception:
                pass
        gst_streams.clear()

        print("[carla-camera] 已退出")


if __name__ == "__main__":
    main()
