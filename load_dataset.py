#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
load_dataset —— 读网页版"导出数据集"生成的 .npz，给训练脚本用。

集成功能：
    1. 训练数据读取与格式校验
    2. 数据集质检报表统计（样本数/总帧数/均帧/空样本/均点/帧）
    3. 离线 3D 轨迹回放（--replay <序号或key>）

用法：
    D:\\mmwave\\.venv\\Scripts\\python.exe load_dataset.py                    # 自动读取最新数据集并展示质检报表
    D:\\mmwave\\.venv\\Scripts\\python.exe load_dataset.py --replay 0       # 回放第 0 个样本手势轨迹
    D:\\mmwave\\.venv\\Scripts\\python.exe load_dataset.py --replay swipe_right__000
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np


def print_stats(d, meta):
    print("\n" + "=" * 76)
    print("【数据集质检统计报表】")
    print(f"{'手势类别':<14}{'样本数':>6}{'总帧数':>8}{'均帧/样本':>10}{'空样本数':>8}{'均点/帧':>9}{'均点/样本':>12}")
    print("-" * 76)
    total_clips = 0
    total_all_frames = 0
    total_all_points = 0

    for label, counts in meta["labels"].items():
        n_clips = len(counts)
        tot_frames = sum(counts)
        avg_fr = tot_frames / max(1, n_clips)

        empty_cnt = 0
        tot_pts = 0
        for i in range(n_clips):
            key = f"{label}__{i:03d}"
            if key in d:
                clip = d[key]
                pts_cnt = sum(f.shape[0] for f in clip)
                tot_pts += pts_cnt
                if pts_cnt == 0:
                    empty_cnt += 1
        avg_pts_fr = tot_pts / max(1, tot_frames)
        avg_pts_clip = tot_pts / max(1, n_clips)

        print(f"{label:<14}{n_clips:>6}{tot_frames:>8}{avg_fr:>10.1f}{empty_cnt:>8}{avg_pts_fr:>9.1f}{avg_pts_clip:>12.1f}")
        total_clips += n_clips
        total_all_frames += tot_frames
        total_all_points += tot_pts

    print("-" * 76)
    denom = max(1, total_all_frames)
    print(f"总计: {total_clips} 个样本, {total_all_frames} 帧, {total_all_points} 个有效ROI点 (均点/帧: {total_all_points / denom:.1f})")
    print("=" * 76 + "\n")


def replay_clip(d, meta, target_key):
    import matplotlib
    try:
        matplotlib.use("TkAgg")
    except Exception:
        pass
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial"]
    plt.rcParams["axes.unicode_minus"] = False

    keys = list(meta["keys"])
    if target_key.isdigit():
        idx = int(target_key)
        if 0 <= idx < len(keys):
            target_key = keys[idx]
        else:
            print(f"错误: 样本索引越界 (0 ~ {len(keys)-1})")
            return
    elif target_key not in keys:
        match = [k for k in keys if target_key in k]
        if match:
            target_key = match[0]
        else:
            print(f"错误: 找不到样本 key: {target_key}")
            return

    clip = d[target_key]
    label = meta["keys"][target_key]
    subj = meta.get("subjects", {}).get(target_key, "默认")

    times_key = target_key + "__times"
    times = d[times_key] if times_key in d else [i * 0.05 for i in range(len(clip))]

    roi = meta.get("roi", {"x": [-0.5, 0.5], "y": [0.05, 1.15], "z": [-0.4, 0.5]})
    rx, ry, rz = roi["x"], roi["y"], roi["z"]

    fig = plt.figure(figsize=(10, 7), dpi=100)
    ax = fig.add_subplot(111, projection="3d")
    fig.canvas.manager.set_window_title(f"手势回放: {target_key} ({label})")

    ax.set_xlim(rx[0] - 0.15, rx[1] + 0.15)
    ax.set_ylim(ry[0] - 0.1, ry[1] + 0.15)
    ax.set_zlim(rz[0] - 0.15, rz[1] + 0.15)
    ax.set_xlabel("X 左右 (m)")
    ax.set_ylabel("Y 前后 (m, 正前方)")
    ax.set_zlabel("Z 上下 (m)")

    title_txt = fig.text(0.5, 0.95, "", ha="center", fontsize=12)

    def update(i):
        ax.cla()
        ax.set_xlim(rx[0] - 0.15, rx[1] + 0.15)
        ax.set_ylim(ry[0] - 0.1, ry[1] + 0.15)
        ax.set_zlim(rz[0] - 0.15, rz[1] + 0.15)
        ax.set_xlabel("X 左右 (m)")
        ax.set_ylabel("Y 前后 (m)")
        ax.set_zlabel("Z 上下 (m)")

        pts = clip[i]
        n_pts = len(pts)
        if n_pts > 0:
            v = np.clip(pts[:, 3], -2.5, 2.5) if pts.shape[1] > 3 else np.zeros(n_pts)
            ax.scatter(pts[:, 0], pts[:, 1], pts[:, 2], c=v,
                       cmap="coolwarm", vmin=-2.5, vmax=2.5, s=25)
        t_val = times[i] if i < len(times) else 0.0
        title_txt.set_text(f"{target_key} [{label}] 被试:{subj}  帧 {i+1}/{len(clip)}  t={t_val:.2f}s  点数={n_pts}")
        return []

    if not len(clip):
        plt.close(fig)
        print("该片段没有帧，无法回放")
        return
    interval = 1000 / (15 if meta.get("cfg_mode") == "15fps" else 20)
    ani = FuncAnimation(fig, update, frames=range(len(clip)), interval=interval,
                        repeat=True, blit=False, cache_frame_data=False)
    plt.show()


def main():
    p = argparse.ArgumentParser(description="加载并质检手势数据集 (.npz)")
    p.add_argument("--file", default="", help="指定 .npz 文件路径 (默认自动读取最新)")
    p.add_argument("--replay", default="", help="回放样本 3D 轨迹 (如 --replay 0 或 --replay swipe_right__000)")
    args = p.parse_args()

    if args.file:
        path = Path(args.file)
    else:
        files = sorted(Path(__file__).parent.glob("recordings/dataset_*.npz"))
        if not files:
            print("还没有导出的数据集。先在网页版采集几个片段，再点[导出数据集 .npz]。")
            sys.exit(0)
        path = files[-1]

    print("读取数据集:", path)
    d = np.load(path, allow_pickle=True)
    meta = json.loads(str(d["meta"]))

    if args.replay != "":
        replay_clip(d, meta, args.replay)
        return

    cols = meta.get("columns", ["x", "y", "z", "速度", "SNR", "Noise"])
    print("\n数据格式字段:", cols)
    if meta.get("cfg_mode"):
        print("雷达配置模式:", meta["cfg_mode"])
    if meta.get("roi"):
        print("ROI空间包围盒:", meta["roi"])

    subjects = meta.get("subjects", {})
    if subjects:
        print("被试分布统计:", dict(Counter(subjects.values())))

    # 打印质检报表
    print_stats(d, meta)

    print("片段样例:")
    for key in list(meta["keys"])[:3]:
        clip = d[key]
        label = meta["keys"][key]
        subj = subjects.get(key, "默认")
        n_pts = [f.shape[0] for f in clip]
        raw_key = key + "__raw"
        raw_info = ""
        if raw_key in d:
            n_raw = [f.shape[0] for f in d[raw_key]]
            raw_info = " (原始点数 %s)" % n_raw[:5]
        print("  %-24s 标签=%-12s 被试=%-6s 帧数=%-3d ROI点数 %s%s"
              % (key, label, subj, len(clip), n_pts[:5], raw_info))
        if len(clip) and clip[0].shape[0]:
            print("    首帧首点特征值:", clip[0][0])

    print("\n训练代码加载提示:")
    print("  d = np.load('%s', allow_pickle=True)" % path.name)
    print("  for key in meta['keys']: clip = d[key]; label = meta['keys'][key]")


if __name__ == "__main__":
    main()
