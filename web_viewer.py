#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
web_viewer —— 网页版实时点云（阶段 1）

架构（串口和解析全部复用 realtime_viewer 里调试好的代码）：
    板子 COM7/COM8 → 本脚本（串口线程 + FrameParser）→ WebSocket → 浏览器页面

用法：
    D:\\mmwave\\.venv\\Scripts\\python.exe web_viewer.py
    D:\\mmwave\\.venv\\Scripts\\python.exe web_viewer.py --no-config   # 板子已在跑
    然后浏览器打开  http://127.0.0.1:8080

注意：
    * 串口是独占的。跑本脚本前先关掉桌面版 realtime_viewer 和 TI 的可视化网页。
    * 串口被占用时不用重启：页面会显示红色"未连接"，这边每 3 秒自动重试，
      占用程序一关它就自己连上。
    * "开始录制"后每帧写一行 JSON 到 recordings/rec_*.jsonl（打标也写进同一文件），
      以后给深度学习当训练数据。
"""

import argparse
import asyncio
import json
import math
import queue as pyqueue
import struct
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
import re
from pathlib import Path

import numpy as np
import serial
import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from realtime_viewer import (
    CFG_GESTURE_15FPS,
    CFG_GESTURE_20FPS,
    DEFAULT_CFG,
    FrameParser,
    resolve_ports,
    send_config,
    switch_frame_rate,
)

INDEX = Path(__file__).parent / "web" / "index.html"
REC_DIR = Path(__file__).parent / "recordings"

Q = None                    # asyncio.Queue：串口线程 → 所有浏览器
CMD = pyqueue.Queue(maxsize=100)       # 浏览器 → 串口线程（录制/打标命令）
STOP = threading.Event()
WRITE_LOCK = threading.Lock()
EXPORT_LOCK = threading.Lock()

ACTIVE_CFG_MODE = "15fps"   # 稳定优先：20fps 在当前板子上曾导致数据与 CLI 停止
ACTIVE_CFG_TEXT = DEFAULT_CFG

# 新浏览器连接时立即提供当前状态，只缓存持续状态，不重播一次性事件。
LAST_STATUS = {"type": "status", "connected": False, "phase": "后台启动中"}
STATUS_FIELDS = {
    "connected", "phase", "error", "cli", "data", "cfg_mode", "recording",
    "rec_file", "rec_frames", "fps", "kbps", "latency_ms", "points", "frame",
    "frames_total", "bytes_total", "clip_recording", "samples", "can_undo", "exporting",
}


# ============================================================ 帧解析（在 FrameParser 基础上多取距离谱/距离-多普勒图）

RD_DOPPLER_BINS = 32      # 当前配置 frameCfg numLoops=32
RD_RANGE_BINS = 128       # 只取前 128 个距离 bin = 0~4.46m（手势活动区）
DV_BINS = 33              # 点云速度直方图 bin 数（多普勒-时间瀑布用）

# 自然手势黄金空间 ROI 包围盒（米）：X左右 ±0.5m(全宽1m)，Y深度 0.05~1.15m(全深1.1m)，Z高度 -0.4~0.5m(全高0.9m)
ROI_BOX = {"x": [-0.5, 0.5], "y": [0.05, 1.15], "z": [-0.4, 0.5]}


def filter_roi(pts, roi=ROI_BOX):
    """过滤出 ROI 包围盒内的手势点云。"""
    if not pts:
        return []
    filtered = []
    for p in pts:
        x, y, z = p[0], p[1], p[2]
        if (roi["x"][0] <= x <= roi["x"][1] and
            roi["y"][0] <= y <= roi["y"][1] and
            roi["z"][0] <= z <= roi["z"][1]):
            filtered.append(p)
    return filtered


def parse_all(parser, fb):
    """一次 TLV 遍历：6维点云(x,y,z,v,snr,noise) + 距离谱 + 距离-多普勒图 + 速度直方图。"""
    plen = struct.unpack_from("<I", fb, parser._layout)[0]
    ts = parser._find_tlv_start(fb, plen)
    raw, snrs, noises, rp, rd = [], [], [], None, None
    if ts is not None:
        pos = ts
        while pos + 8 <= plen:
            t, ln = struct.unpack_from("<II", fb, pos)
            if t == 0 or ln % 4 != 0 or pos + 8 + ln > plen:
                break
            payload = fb[pos + 8: pos + 8 + ln]
            if t == 1:
                for k in range(len(payload) // 16):
                    raw.append(struct.unpack_from("<4f", payload, k * 16))
            elif t == 7:
                for k in range(len(payload) // 4):
                    s, n = struct.unpack_from("<2h", payload, k * 4)
                    snrs.append(s / 10.0)
                    noises.append(n / 10.0)
            elif t == 2 and len(payload) >= 4:
                rp = [int(v) for v in
                      struct.unpack_from("<%dH" % (len(payload) // 2), payload)][:RD_RANGE_BINS]
            elif t == 5 and len(payload) >= RD_DOPPLER_BINS * 2:
                # 距离-多普勒热图：(numRangeBins × numLoops) uint16，行主序。
                # 多普勒维做 fftshift（负速度在前），距离维只留手势区。
                n_r = (len(payload) // 2) // RD_DOPPLER_BINS
                if n_r > 0:
                    vals = struct.unpack_from("<%dH" % (n_r * RD_DOPPLER_BINS), payload)
                    half = RD_DOPPLER_BINS // 2
                    rd = []
                    for r in range(min(n_r, RD_RANGE_BINS)):
                        row = list(vals[r * RD_DOPPLER_BINS:(r + 1) * RD_DOPPLER_BINS])
                        rd.append(row[half:] + row[:half])
            pos += 8 + ln
    pts = []
    for i, (x, y, z, v) in enumerate(raw):
        # 丢弃固件偶发异常点，避免 int(NaN) 触发整条串口链路重连。
        if not all(math.isfinite(value) for value in (x, y, z, v)):
            continue
        s = round(snrs[i], 1) if i < len(snrs) else None
        n = round(noises[i], 1) if i < len(noises) else None
        pts.append([round(x, 3), round(y, 3), round(z, 3), round(v, 3), s, n])
    # 速度直方图（点云版"微多普勒"，匹配新配置 ±3.5m/s 速度量程）
    dv = [0] * DV_BINS
    for p in pts:
        v = p[3]
        if -3.5 <= v <= 3.5:
            b = min(DV_BINS - 1, int((v + 3.5) / 7.0 * DV_BINS))
            dv[b] += 1
    return pts, rp, rd, dv


# ============================================================ 串口工作线程

def samples_count():
    """每个标签下已采集的片段数：recordings/clips/<标签>/*.jsonl"""
    return {d.name: len(list(d.glob("*.jsonl")))
            for d in REC_DIR.glob("clips/*") if d.is_dir()}


def export_npz(loop):
    """把 recordings/clips/<标签>/*.jsonl 打包成训练用的 .npz 数据集。"""
    import numpy as np

    def push(msg):
        def put():
            if Q.full():
                Q.get_nowait()
            Q.put_nowait(msg)
        loop.call_soon_threadsafe(put)

    if not EXPORT_LOCK.acquire(blocking=False):
        push({"type": "status", "export_done": {"error": "正在打包中，请勿重复操作"}})
        return

    push({"type": "status", "exporting": True})

    try:
        entries, manifest, subjects_map, clip_configs = {}, {}, {}, {}
        for d in sorted(REC_DIR.glob("clips/*")):
            if not d.is_dir():
                continue
            clips = []
            for f in sorted(d.glob("*.jsonl")):
                frames_roi, frames_raw = [], []
                frame_nos, rel_times = [], []
                t0_clip = 0.0
                subj = "s01"
                source_cfg = {"mode": "unknown", "text": None}
                # 从文件名尝试解析被试：<label>_<subject>_<time>.jsonl
                parts = f.stem.split("_")
                if len(parts) >= 3 and parts[-2].startswith("s") and len(parts[-2]) <= 4:
                    subj = parts[-2]

                for line in f.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    rec = json.loads(line)
                    if "clip" in rec:
                        if rec.get("subject"):
                            subj = rec["subject"]
                        t0_clip = float(rec.get("t0", 0.0))
                        source_cfg = {"mode": rec.get("cfg_mode", "unknown"), "text": rec.get("cfg")}
                    elif rec.get("pts") is not None:
                        roi_pts = np.asarray(rec["pts"], dtype=np.float32).reshape(-1, 6)
                        raw_pts = np.asarray(rec.get("pts_raw", rec["pts"]), dtype=np.float32).reshape(-1, 6)
                        frames_roi.append(roi_pts)
                        frames_raw.append(raw_pts)
                        frame_nos.append(int(rec.get("frame", 0)))
                        t_frame = float(rec.get("t", 0.0))
                        rel_times.append(round(t_frame - t0_clip, 3) if t0_clip > 0 else 0.0)

                if frames_roi:
                    clips.append({
                        "file": f.name,
                        "subject": subj,
                        "cfg": source_cfg,
                        "n_frames": len(frames_roi),
                        "points": frames_roi,
                        "points_raw": frames_raw,
                        "frames": np.array(frame_nos, dtype=np.int32),
                        "times": np.array(rel_times, dtype=np.float32),
                    })
            if clips:
                manifest[d.name] = clips

        if not manifest:
            push({"type": "status", "export_done": {"error": "没有任何片段，先去采集"}})
            return

        for label, clips in manifest.items():
            for i, c in enumerate(clips):
                key = "%s__%03d" % (label, i)
                entries[key] = np.empty(len(c["points"]), dtype=object)
                entries[key][:] = c["points"]
                entries[key + "__raw"] = np.empty(len(c["points_raw"]), dtype=object)
                entries[key + "__raw"][:] = c["points_raw"]
                entries[key + "__frames"] = c["frames"]
                entries[key + "__times"] = c["times"]
                subjects_map[key] = c["subject"]
                clip_configs[key] = c["cfg"]

        meta = {
            "labels": {lbl: [c["n_frames"] for c in cs] for lbl, cs in manifest.items()},
            "keys": {k: k.rsplit("__", 1)[0] for k in entries if not k.endswith("__raw") and not k.endswith("__frames") and not k.endswith("__times")},
            "subjects": subjects_map,
            "columns": ["x", "y", "z", "doppler", "snr", "noise"],
            "roi": ROI_BOX,
            "cfg_mode": "per_clip",
            "cfg": None,
            "clip_configs": clip_configs,
            "schema_version": 1
        }
        path = REC_DIR / ("dataset_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".npz")
        np.savez_compressed(path, meta=json.dumps(meta, ensure_ascii=False),
                            **{k: v for k, v in entries.items()})
        total_clips = sum(len(c) for c in manifest.values())
        total_frames = sum(sum(c["n_frames"] for c in cs) for cs in manifest.values())
        size_kb = round(path.stat().st_size / 1024.0, 1)
        push({"type": "status",
              "export_done": {"path": str(path),
                              "filename": path.name,
                              "download_url": f"/download/{path.name}",
                              "size_kb": size_kb,
                              "n_clips": total_clips,
                              "n_frames": total_frames,
                              "labels": {k: len(v) for k, v in manifest.items()},
                              "samples": samples_count()}})
    except Exception as e:
        push({"type": "status", "export_done": {"error": str(e)}})
    finally:
        push({"type": "status", "exporting": False})
        EXPORT_LOCK.release()




def serial_worker(loop, args):
    # 函数里会给这两个名字赋值。不声明 global 的话，Python 会把它们当成
    # 局部变量，一启动读 ACTIVE_CFG_MODE 就抛 UnboundLocalError，配置发不出去。
    global ACTIVE_CFG_MODE, ACTIVE_CFG_TEXT
    if args.no_config:
        ACTIVE_CFG_MODE, ACTIVE_CFG_TEXT = "unknown", None
    parser = FrameParser()
    state = {"rec_fh": None, "rec_file": None, "rec_frames": 0}

    def push(msg):
        def _put():
            if Q.full():
                if msg.get("type") == "frame":
                    return
                try:
                    Q.get_nowait()
                except (pyqueue.Empty, asyncio.QueueEmpty):
                    pass
            Q.put_nowait(msg)
        loop.call_soon_threadsafe(_put)

    def status(**kw):
        global ACTIVE_CFG_MODE
        msg = {"type": "status",
               "recording": state["rec_fh"] is not None,
               "rec_file": state["rec_file"],
               "rec_frames": state["rec_frames"],
               "cfg_mode": ACTIVE_CFG_MODE}
        msg.update(kw)
        push(msg)

    while not STOP.is_set():
        # --- 找串口 ---
        status(connected=False, phase="找串口中...")
        cli, data = resolve_ports(args)
        if not cli or not data:
            status(connected=False, phase="没找到 Silicon Labs 串口",
                   error="板子没插好？驱动没装？")
            STOP.wait(3.0)
            continue

        try:
            ser = serial.Serial(data, 921600, timeout=0.1)
            try:
                ser.set_buffer_size(rx_size=1 << 20, tx_size=4096)
            except Exception:
                pass
        except Exception as e:
            status(connected=False, phase="串口打不开",
                   error="%s（被占用？先关掉桌面版/TI 网页，会自动重试）" % e)
            STOP.wait(3.0)
            continue

        # 读串口必须单独一条线程，而且不能被解析/网页堵住。
        # 921600 下硬件 FIFO 只有几百字节，主线程一停超过几毫秒，
        # 板子的 UART 写函数就会卡住，整个固件连 CLI 一起死掉。
        raw_q = pyqueue.Queue(maxsize=128)
        read_err = []
        stop_read = threading.Event()

        def _drain():
            while not stop_read.is_set():
                try:
                    chunk = ser.read(65536)
                except Exception as e:
                    read_err.append(e)
                    return
                if not chunk:
                    continue
                if raw_q.full():
                    try:
                        raw_q.get_nowait()
                    except pyqueue.Empty:
                        pass
                try:
                    raw_q.put_nowait(chunk)
                except pyqueue.Full:
                    pass

        drain_thread = threading.Thread(target=_drain, daemon=True)
        drain_thread.start()

        # 先持续接收，再发配置：重连时板子可能还在输出，不能让数据口无人读取。
        if not args.no_config:
            status(connected=False, phase="正在发配置...", cli=cli, data=data, error=None)
            try:
                if args.cfg:
                    cfg = Path(args.cfg).read_text(encoding="utf-8", errors="replace")
                    ACTIVE_CFG_MODE = "custom"
                else:
                    cfg = CFG_GESTURE_20FPS if ACTIVE_CFG_MODE == "20fps" else CFG_GESTURE_15FPS
                failed = send_config(cli, cfg)
            except Exception as e:
                failed = ["<串口异常> %s" % e]
            if failed:
                stop_read.set()
                ser.close()
                drain_thread.join(timeout=1.0)
                no_answer = any(str(f).endswith("(无响应)") or str(f).startswith("<串口异常>")
                                for f in failed)
                status(connected=False,
                       phase="雷达没有回应" if no_answer else "配置被板子拒绝",
                       error="失败 %d 条: %s%s" % (
                           len(failed), failed[:2],
                           "；若持续无响应，请断开板子所有电源后重插" if no_answer else ""))
                STOP.wait(3.0)
                continue
            ACTIVE_CFG_TEXT = cfg
            # 丢弃重新配置之前的旧流，解析器从新帧重新同步。
            while True:
                try:
                    raw_q.get_nowait()
                except pyqueue.Empty:
                    break

        # --- 读数据主循环 ---
        parser = FrameParser()  # 重连不能沿用上一段字节流的半帧和布局。
        status(connected=False, phase="串口已打开，等待雷达帧", cli=cli, data=data,
               error=None, samples=samples_count())
        last_frame_at = time.monotonic()
        n_bytes_total, n_frames_total = 0, 0
        win_bytes, win_frames = 0, 0
        t_win = time.time()
        lat_ms, pts_last, frame_no = 0.0, 0, 0
        clip = None          # {"fh","label","end","path","n"} 正在录的手势片段
        last_clip_path = None # 最近一次保存的片段路径，供撤销(undo)使用
        try:
            while not STOP.is_set():
                if read_err:
                    raise read_err[0]
                try:
                    chunk = raw_q.get(timeout=0.4)
                except pyqueue.Empty:
                    chunk = b""
                if chunk:
                    t_arrive = time.time()
                    n_bytes_total += len(chunk)
                    win_bytes += len(chunk)
                    for fb in parser.feed(chunk):
                        pts, rp, rd, dv = parse_all(parser, fb)
                        n_frames_total += 1
                        last_frame_at = time.monotonic()
                        win_frames += 1
                        lat_ms = (time.time() - t_arrive) * 1000.0
                        pts_last = len(pts)
                        try:
                            frame_no = struct.unpack_from("<I", fb,
                                                          (20 if parser._layout == 12 else parser._layout + 4))[0]
                        except Exception:
                            frame_no = n_frames_total
                        pts_roi = filter_roi(pts)
                        msg = {"type": "frame", "t": round(t_arrive, 3),
                               "frame": frame_no, "pts": pts, "pts_roi": pts_roi, "dv": dv}
                        if rp:
                            msg["rp"] = rp
                        if rd:
                            msg["rd"] = rd
                        push(msg)
                        if state["rec_fh"] or clip:
                            line = json.dumps({"t": round(t_arrive, 3),
                                               "frame": frame_no,
                                               "pts": pts_roi,
                                               "pts_raw": pts})
                            if state["rec_fh"]:
                                with WRITE_LOCK:
                                    state["rec_fh"].write(line + "\n")
                                    state["rec_frames"] += 1
                            if clip:
                                clip["fh"].write(line + "\n")
                                clip["n"] += 1
                                clip["n_roi_pts"] += len(pts_roi)

                # 处理浏览器发来的命令（录制/打标/片段采集/撤销/导出）
                try:
                    while True:
                        cmd = CMD.get_nowait()
                        act = cmd.get("action")
                        if act == "record_start" and state["rec_fh"] is None:
                            REC_DIR.mkdir(exist_ok=True)
                            state["rec_file"] = str(
                                REC_DIR / datetime.now().strftime("rec_%Y%m%d_%H%M%S_%f.jsonl"))
                            state["rec_fh"] = open(state["rec_file"], "x",
                                                   encoding="utf-8")
                            state["rec_frames"] = 0
                            status(connected=True, phase="运行中", cli=cli,
                                   data=data, samples=samples_count())
                        elif act == "record_stop" and state["rec_fh"]:
                            saved_file = state["rec_file"]
                            saved_frames = state["rec_frames"]
                            state["rec_fh"].close()
                            state["rec_fh"] = None
                            status(connected=True, phase="运行中", cli=cli,
                                   data=data, samples=samples_count(),
                                   rec_stopped={"file": saved_file,
                                                "filename": Path(saved_file).name if saved_file else "",
                                                "frames": saved_frames})
                        elif act == "tag" and state["rec_fh"]:
                            with WRITE_LOCK:
                                state["rec_fh"].write(json.dumps(
                                    {"t": round(time.time(), 3),
                                     "tag": cmd.get("label", "")}) + "\n")
                        elif act == "clip_start" and clip is None:
                            label = (cmd.get("label") or "clip").strip() or "clip"
                            subject = (cmd.get("subject") or "s01").strip() or "s01"
                            dur = max(0.5, min(10.0, float(cmd.get("dur") or 2.0)))
                            d = REC_DIR / "clips" / label
                            d.mkdir(parents=True, exist_ok=True)
                            path = d / (label + "_" + subject + "_" +
                                        datetime.now().strftime("%Y%m%d_%H%M%S_%f.jsonl"))
                            fh = open(path.with_suffix(".jsonl.part"), "x", encoding="utf-8")
                            fh.write(json.dumps(
                                {"clip": label, "subject": subject,
                                 "t0": round(time.time(), 3),
                                 "dur": dur, "roi": ROI_BOX, "schema_version": 1,
                                  "cfg_mode": ACTIVE_CFG_MODE, "cfg": ACTIVE_CFG_TEXT,
                                 "columns": ["x", "y", "z", "doppler", "snr", "noise"]},
                                ensure_ascii=False) + "\n")
                            clip = {"fh": fh, "label": label, "subject": subject,
                                    "end": time.time() + dur, "path": str(path),
                                    "n": 0, "n_roi_pts": 0}
                            status(connected=True, phase="运行中", cli=cli,
                                   data=data, clip_recording=f"{label} ({subject})",
                                   samples=samples_count())
                        elif act == "clip_undo":
                            if EXPORT_LOCK.locked():
                                push({"type": "status", "clip_undo_error": "正在导出，请稍后撤销"})
                                continue
                            if last_clip_path and Path(last_clip_path).is_file():
                                try:
                                    p = Path(last_clip_path)
                                    p.unlink()
                                    undone_name = p.name
                                    last_clip_path = None
                                    status(connected=True, phase="运行中", cli=cli, data=data,
                                           clip_undone={"filename": undone_name},
                                           can_undo=False,
                                           samples=samples_count())
                                except Exception as e:
                                    push({"type": "status", "clip_undo_error": f"撤销失败: {e}"})
                            else:
                                push({"type": "status", "clip_undo_error": "没有可撤销的片段或文件已不存在"})
                        elif act == "export":
                            threading.Thread(target=export_npz, args=(loop,),
                                             daemon=True).start()
                        elif act == "switch_cfg":
                            target_mode = cmd.get("mode", "15fps")
                            if target_mode != "15fps":
                                push({"type": "status", "cfg_error": "20fps 已禁用：当前固件切换不兼容，且该配置稳定性未通过"})
                                continue
                            if state["rec_fh"] or clip:
                                push({"type": "status", "cfg_error": "当前正在录制中，请先停止录制再切换配置"})
                                continue
                            if (target_mode == ACTIVE_CFG_MODE and n_frames_total > 0
                                    and time.monotonic() - last_frame_at < 3.0):
                                status(connected=True, phase="运行中", cli=cli, data=data,
                                       cfg_mode=ACTIVE_CFG_MODE)
                                continue
                            push({"type": "status", "cfg_switching": target_mode})
                            new_cfg = CFG_GESTURE_20FPS if target_mode == "20fps" else CFG_GESTURE_15FPS
                            try:
                                failed = switch_frame_rate(cli, target_mode)
                                if failed:
                                    status(connected=False, phase="配置恢复失败", cli=cli, data=data,
                                           cfg_error=f"雷达拒绝切换: {failed[0]}",
                                           error="配置未完成，雷达可能已停止；请重启后台，若CLI无响应再断电复位")
                                else:
                                    # 只有接收线程操作数据串口，主线程不并发清空驱动缓存。
                                    while True:
                                        try:
                                            raw_q.get_nowait()
                                        except pyqueue.Empty:
                                            break
                                    parser = FrameParser()
                                    n_frames_total = win_frames = win_bytes = 0
                                    last_frame_at = time.monotonic()
                                    ACTIVE_CFG_MODE = target_mode
                                    ACTIVE_CFG_TEXT = new_cfg
                                    status(connected=False, phase="配置完成，等待雷达帧", cli=cli, data=data,
                                           error=None, cfg_mode=ACTIVE_CFG_MODE,
                                           cfg_switched={"mode": ACTIVE_CFG_MODE,
                                                         "fps": 20 if ACTIVE_CFG_MODE == "20fps" else 15})
                            except Exception as e:
                                push({"type": "status", "cfg_error": f"切换配置异常: {e}"})
                except pyqueue.Empty:
                    pass

                # 片段到时自动收尾
                if clip and time.time() >= clip["end"]:
                    clip["fh"].close()
                    Path(clip["path"]).with_suffix(".jsonl.part").replace(clip["path"])
                    n_roi = clip.get("n_roi_pts", 0)
                    info = {"label": clip["label"], "subject": clip.get("subject", "s01"),
                            "path": clip["path"],
                            "filename": Path(clip["path"]).name,
                            "n_frames": clip["n"],
                            "n_roi_pts": n_roi,
                            "quality_warn": "ROI点数偏少 (%d 点)，建议按 U 撤销" % n_roi if n_roi < 40 else ""}
                    last_clip_path = clip["path"]
                    clip = None
                    status(connected=True, phase="运行中", cli=cli, data=data,
                           clip_done=info, can_undo=True, samples=samples_count())

                # 每秒推一次统计
                now = time.time()
                if now - t_win >= 1.0:
                    span = now - t_win
                    streaming = n_frames_total > 0 and time.monotonic() - last_frame_at < 3.0
                    status(connected=streaming,
                           phase="运行中" if streaming else "串口已打开，但未收到有效雷达帧",
                           error=None if streaming else "请检查雷达是否启动；若 CLI 也无回应，请断电重插板子",
                           cli=cli, data=data,
                           fps=round(win_frames / span, 1),
                           kbps=round(win_bytes / span / 1024.0, 1),
                           latency_ms=round(lat_ms),
                           points=pts_last, frame=frame_no,
                           frames_total=n_frames_total,
                           bytes_total=n_bytes_total,
                           clip_recording=clip["label"] if clip else None,
                           samples=samples_count())
                    t_win, win_bytes, win_frames = now, 0, 0
        except Exception as e:
            print("[!] 读数据口出错：%s" % e)
            status(connected=False, phase="串口读取出错",
                   error="%s（会自动重连）" % e)
        finally:
            if clip:
                clip["fh"].close()
                clip = None
            if STOP.is_set() and not args.no_config:
                # 正常关闭后台时先停雷达，再停止数据接收。
                try:
                    send_config(cli, "sensorStop", verbose=False)
                except Exception:
                    pass
            stop_read.set()
            try:
                ser.close()
            except Exception:
                pass
            drain_thread.join(timeout=1.0)
        STOP.wait(1.0)

    if state["rec_fh"]:
        state["rec_fh"].close()


# ============================================================ FastAPI 应用

@asynccontextmanager
async def lifespan(app):
    global Q
    Q = asyncio.Queue(maxsize=500)
    STOP.clear()
    loop = asyncio.get_running_loop()
    worker = threading.Thread(target=serial_worker, args=(loop, ARGS), daemon=True)
    worker.start()
    task = asyncio.create_task(broadcast_loop())
    try:
        yield
    finally:
        STOP.set()
        await asyncio.to_thread(worker.join, 5.0)
        task.cancel()


async def broadcast_loop():
    while True:
        msg = await Q.get()
        if msg.get("type") == "status":
            LAST_STATUS.update({key: value for key, value in msg.items()
                                if key in STATUS_FIELDS})
        text = json.dumps(msg, allow_nan=False)
        dead = []
        for ws in list(CLIENTS):
            try:
                await asyncio.wait_for(ws.send_text(text), timeout=1.0)
            except Exception:
                dead.append(ws)
        for ws in dead:
            CLIENTS.discard(ws)


CLIENTS = set()
ARGS = None


def validate_clip_command(msg):
    """标签/被试只能是安全的单段文件名，错误输入不能影响串口线程。"""
    result = dict(msg)
    for field, default in (("label", "clip"), ("subject", "s01")):
        value = result.get(field, default)
        if not isinstance(value, str):
            raise ValueError("标签和被试必须是文本")
        value = value.strip() or default
        if (len(value) > 64 or not re.fullmatch(r"[\w\-\u4e00-\u9fff]+", value)
                or value.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}):
            raise ValueError("标签/被试仅支持文字、数字、下划线和短横线，最长64字符")
        result[field] = value
    try:
        dur = float(result.get("dur", 2.0))
    except (TypeError, ValueError):
        raise ValueError("录制时长必须是有效数字") from None
    if not math.isfinite(dur) or not 0.5 <= dur <= 10:
        raise ValueError("录制时长必须在0.5到10秒之间")
    result["dur"] = dur
    return result


def handle_cmd(msg):
    if not isinstance(msg, dict):
        return
    action = msg.get("action")
    if action == "switch_cfg" and msg.get("mode", "15fps") != "15fps":
        if Q is not None:
            if not Q.full():
                Q.put_nowait({"type": "status", "cfg_error": "20fps 已禁用：请使用15fps稳定配置"})
        return
    if action == "clip_start":
        try:
            msg = validate_clip_command(msg)
        except ValueError as e:
            if Q is not None and not Q.full():
                Q.put_nowait({"type": "status", "command_error": str(e)})
            return
    if action in ("record_start", "record_stop", "tag",
                  "clip_start", "clip_undo", "export", "switch_cfg"):
        try:
            CMD.put_nowait(msg)
        except pyqueue.Full:
            if Q is not None and not Q.full():
                Q.put_nowait({"type": "status", "command_error": "操作队列已满，请稍后重试"})


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(INDEX)


@app.get("/download/{filename}")
async def download_file(filename: str):
    target = (REC_DIR / filename).resolve()
    try:
        target.relative_to(REC_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="禁止访问非法路径")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(target, filename=filename, media_type="application/octet-stream")


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    origin = ws.headers.get("origin") if hasattr(ws, "headers") else None
    if origin and origin not in {"http://" + ws.headers.get("host", ""), "https://" + ws.headers.get("host", "")}:
        await ws.close(code=1008)
        return
    await ws.accept()
    CLIENTS.add(ws)
    try:
        await ws.send_text(json.dumps({**LAST_STATUS, "cfg_mode": ACTIVE_CFG_MODE}))
    except Exception:
        pass
    try:
        while True:
            try:
                handle_cmd(await ws.receive_json())
            except (json.JSONDecodeError, ValueError):
                await ws.send_text(json.dumps({"type": "status", "command_error": "命令必须是有效JSON"}))
    except WebSocketDisconnect:
        pass
    finally:
        CLIENTS.discard(ws)


def main():
    global ARGS
    ap = argparse.ArgumentParser(description="IWR6843AOP 网页版实时点云")
    ap.add_argument("--cli", help="Enhanced COM（配置口），如 COM7")
    ap.add_argument("--data", help="Standard COM（数据口），如 COM8")
    ap.add_argument("--cfg", help="配置文件路径")
    ap.add_argument("--no-config", action="store_true", help="不重发配置（板子已在跑）")
    ap.add_argument("--http-port", type=int, default=8080, help="网页端口，默认 8080")
    ARGS = ap.parse_args()

    print("=" * 60)
    print("IWR6843AOP 网页版实时点云")
    print("跑起来后用浏览器打开:  http://127.0.0.1:%d" % ARGS.http_port)
    print("提醒：先关掉桌面版 realtime_viewer 和 TI 可视化网页（串口独占）。")
    print("=" * 60)
    uvicorn.run(app, host="127.0.0.1", port=ARGS.http_port, log_level="warning")


if __name__ == "__main__":
    main()
