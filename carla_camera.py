#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import queue
import sys
import time
from pathlib import Path

import yaml

from gst_streaming import GStreamerConfig, GStreamerObject


def default_config_path():
    local_path = Path.cwd() / "config.yaml"
    if local_path.exists():
        return local_path
    if getattr(sys, "frozen", False):
        exe_path = Path(sys.executable).with_name("config.yaml")
        if exe_path.exists():
            return exe_path
    return Path(__file__).with_name("config.yaml")


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def make_gst_config(stream_cfg, camera_cfg):
    image_w = int(camera_cfg.get("image_size_x", stream_cfg.get("input_width", 1920)))
    image_h = int(camera_cfg.get("image_size_y", stream_cfg.get("input_height", 1080)))
    sensor_tick = float(camera_cfg.get("sensor_tick", 0.1))
    fps = int(round(1.0 / sensor_tick)) if sensor_tick > 0 else int(stream_cfg.get("input_fps", 10))

    mount = stream_cfg.get("mount")
    if not mount:
        role_name = camera_cfg.get("role_name") or camera_cfg.get("name") or f"camera_{camera_cfg.get('id', 0)}"
        mount = f"/stream/{role_name}"
    if not mount.startswith("/"):
        mount = f"/{mount}"

    cfg = GStreamerConfig()
    cfg.input_width = image_w
    cfg.input_height = image_h
    cfg.input_fps = fps
    cfg.input_pix_fmt = stream_cfg.get("input_pix_fmt", "BGRA")
    cfg.vcodec = stream_cfg.get("vcodec", "auto")
    cfg.video_bitrate = stream_cfg.get("video_bitrate", "4000k")
    cfg.output_mode = stream_cfg.get("output_mode", "rtsp_server")
    cfg.output_host = stream_cfg.get("output_host", "0.0.0.0")
    cfg.output_port = int(stream_cfg.get("output_port", 8554))
    cfg.rtsp_mount = mount
    cfg.output_url = stream_cfg.get("output_url") or f"rtsp://127.0.0.1:{cfg.output_port}{mount}"
    cfg.label = stream_cfg.get("label") or f"gst-{camera_cfg.get('role_name', camera_cfg.get('id', 'camera'))}"
    return cfg


def put_latest(q, item):
    try:
        q.get_nowait()
    except queue.Empty:
        pass
    try:
        q.put_nowait(item)
    except queue.Full:
        pass


def actor_matches_vehicle(actor, vehicle_cfg):
    if not actor.type_id.startswith("vehicle."):
        return False

    model = vehicle_cfg.get("model")
    if model and actor.type_id != model:
        return False

    role_name = vehicle_cfg.get("role_name")
    if role_name and actor.attributes.get("role_name") != role_name:
        return False

    return True


def find_vehicle(world, vehicle_cfg):
    actors = world.get_actors()
    vehicle_id = vehicle_cfg.get("actor_id") or vehicle_cfg.get("id")
    if vehicle_id is not None:
        actor = actors.find(int(vehicle_id))
        if actor and actor.type_id.startswith("vehicle."):
            return actor
        raise RuntimeError(f"找不到 vehicle actor id={vehicle_id}")

    matched = [a for a in actors if actor_matches_vehicle(a, vehicle_cfg)]
    if len(matched) == 1:
        return matched[0]
    if not matched:
        raise RuntimeError(f"找不到匹配车辆: role_name={vehicle_cfg.get('role_name')!r}, model={vehicle_cfg.get('model')!r}")

    desc = ", ".join(
        f"id={a.id}, type={a.type_id}, role_name={a.attributes.get('role_name')}" for a in matched
    )
    raise RuntimeError(f"匹配到多辆车，请在配置里填写 actor_id。候选: {desc}")


def make_sensor_callback(q):
    def _on_sensor_image(image):
        put_latest(q, image)

    return _on_sensor_image


def make_transform(carla, cfg):
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
    attr_map = {
        "role_name": cam_cfg.get("role_name"),
        "image_size_x": cam_cfg.get("image_size_x"),
        "image_size_y": cam_cfg.get("image_size_y"),
        "fov": cam_cfg.get("fov"),
        "fstop": cam_cfg.get("fstop"),
        "iso": cam_cfg.get("iso"),
        "gamma": cam_cfg.get("gamma"),
        "sensor_tick": cam_cfg.get("sensor_tick"),
    }
    for key, value in attr_map.items():
        if value is not None:
            cam_bp.set_attribute(key, str(value))


def main():
    parser = argparse.ArgumentParser(description="读取配置，将 CARLA 相机挂载到现有车辆并推 RTSP")
    parser.add_argument("--config", default=str(default_config_path()), help="YAML 配置文件路径")
    args = parser.parse_args()

    import carla

    config = load_config(args.config)
    carla_cfg = config.get("carla", {})
    vehicle_cfg = config.get("vehicle", {})
    cameras_cfg = [c for c in config.get("cameras", []) if c.get("enable", True)]
    stream_defaults = config.get("stream", {})

    if not cameras_cfg:
        raise RuntimeError("配置里没有启用的相机")

    client = carla.Client(carla_cfg.get("host", "127.0.0.1"), int(carla_cfg.get("port", 3000)))
    client.set_timeout(float(carla_cfg.get("timeout", 10.0)))
    world = client.get_world()

    vehicle = find_vehicle(world, vehicle_cfg)
    print(
        f"[carla-camera] 使用车辆: id={vehicle.id}, type={vehicle.type_id}, "
        f"role_name={vehicle.attributes.get('role_name')}, label={vehicle_cfg.get('label')}"
    )

    gst_streams = []
    cameras = []
    image_queues = []

    try:
        for cam_cfg in cameras_cfg:
            stream_cfg = dict(stream_defaults)
            stream_cfg.update(cam_cfg.get("stream", {}))
            gst = GStreamerObject(make_gst_config(stream_cfg, cam_cfg))
            if not gst.initialize_pipe():
                raise RuntimeError(f"GStreamer 初始化失败: {cam_cfg.get('role_name') or cam_cfg.get('id')}")
            gst_streams.append(gst)
            print(f"[carla-camera] RTSP[{cam_cfg.get('role_name') or cam_cfg.get('id')}]: {gst._gst_cfg.output_url}")

        blueprint_library = world.get_blueprint_library()
        for cam_cfg in cameras_cfg:
            sensor_type = cam_cfg.get("sensor_type", "sensor.camera.rgb")
            cam_bp = blueprint_library.find(sensor_type)
            configure_camera_blueprint(cam_bp, cam_cfg)

            cam = world.spawn_actor(cam_bp, make_transform(carla, cam_cfg), attach_to=vehicle)
            q = queue.Queue(maxsize=1)
            cam.listen(make_sensor_callback(q))

            cameras.append(cam)
            image_queues.append(q)
            print(f"[carla-camera] 已挂载相机: id={cam.id}, role_name={cam_cfg.get('role_name')}")

        frame_count = 0
        print("[carla-camera] 开始推流，Ctrl+C 退出")
        while True:
            stats = []
            for idx, cam_cfg in enumerate(cameras_cfg):
                name = cam_cfg.get("role_name") or cam_cfg.get("id") or idx
                get_t0 = time.perf_counter()
                try:
                    image = image_queues[idx].get(timeout=1.0)
                except queue.Empty:
                    stats.append(f"{name}: 取图超时")
                    continue
                get_ms = (time.perf_counter() - get_t0) * 1000.0

                send_t0 = time.perf_counter()
                gst_streams[idx].send_carla_frame(image)
                send_ms = (time.perf_counter() - send_t0) * 1000.0
                stats.append(f"{name}: 帧={image.frame} 取图={get_ms:.3f}ms 推流={send_ms:.3f}ms")

            frame_count += 1
            # print(f"[carla-camera] 已推帧={frame_count} " + " | ".join(stats), flush=True)

    except KeyboardInterrupt:
        print("\n[carla-camera] 用户中断")
    finally:
        for cam in cameras:
            try:
                cam.stop()
                cam.destroy()
                print(f"[carla-camera] 已销毁相机 id={cam.id}")
            except Exception as e:
                print(f"[carla-camera] 销毁相机失败: {e}")
        for gst in gst_streams:
            gst.destroy_pipe()
        print("[carla-camera] 清理完成")


if __name__ == "__main__":
    main()
