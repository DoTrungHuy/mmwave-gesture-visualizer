#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest —— 不插板子，验证 realtime_viewer 的数据通路能不能做到实时。

测三件事：
    1. 正确性：造一批 TI demo 格式的帧，切碎成串口字节流喂给 FrameParser，
       看切出来的帧数、点云、SNR 是否和源数据完全一致
    2. 解析吞吐：reader 线程每秒最多能解多少帧（需求：远大于 20 fps）
    3. 绘图耗时：整幅重绘 vs blit 只重画动着的元素，各要多久
    4. 真窗口冒烟：用默认后端开 3 秒窗口跑 blit 动画（需要显示器）

用法：
    D:\\mmwave\\.venv\\Scripts\\python.exe selftest.py
"""

import random
import struct
import time

import numpy as np

from realtime_viewer import MAGIC, FrameParser


# ============================================================ 造数据

def make_frame(frame_num, xyzv, snr_noise):
    """
    按照 TI mmWave demo 的 UART 输出格式造一帧：
      帧头 40 字节：magic(8) version(4) platform(4) packetLen(4)
                    frameNumber(4) cpuCycles(4) numObj(4) numTLVs(4) padding(4)
      TLV 2 距离谱(256×uint16) + TLV 1 点云(每点 4×float32) + TLV 7 side info(每点 2×int16)
    """
    xyzv = np.asarray(xyzv, dtype=np.float32)
    snr_noise = np.asarray(snr_noise, dtype=np.int16)

    rp = struct.pack("<256H", *range(256))
    tlv_rp = struct.pack("<II", 2, len(rp)) + rp

    p_payload = xyzv.tobytes()
    tlv_pts = struct.pack("<II", 1, len(p_payload)) + p_payload

    s_payload = snr_noise.tobytes()
    tlv_side = struct.pack("<II", 7, len(s_payload)) + s_payload

    body = tlv_rp + tlv_pts + tlv_side
    total = 40 + len(body)
    header = MAGIC + struct.pack(
        "<IIIIIIII",
        0x03000502,      # version
        0xA06843,        # platform
        total,           # packetLen（解析器就是靠它切帧的）
        frame_num,
        123456,          # cpuCycles
        len(xyzv),       # numObj
        3,               # numTLVs
        0,               # padding
    )
    return header + body


def make_stream(n_frames, max_pts=60, seed=1):
    """造 n_frames 帧，返回 (字节流, 期望的每帧点云/side info)"""
    rng = random.Random(seed)
    npr = np.random.default_rng(seed)
    stream, expect = [], []
    for i in range(1, n_frames + 1):
        n = rng.randint(1, max_pts)
        xyzv = npr.uniform([-3, -3, 0, -1], [3, 3, 6, 1], size=(n, 4))
        sn = npr.integers(0, 3000, size=(n, 2))
        stream.append(make_frame(i, xyzv, sn))
        expect.append((xyzv.astype(np.float32), sn.astype(np.int16)))
    return b"".join(stream), expect


# ============================================================ 测试 1：正确性

def test_correctness():
    stream, expect = make_stream(30)
    parser = FrameParser()

    # 模拟串口：按随机大小切块喂进去（串口的字节就是这么一坨一坨到的）
    rng = random.Random(7)
    frames, pos = [], 0
    while pos < len(stream):
        k = rng.randint(1, 3000)
        frames += parser.feed(stream[pos:pos + k])
        pos += k

    assert len(frames) == 30, "切出来的帧数不对：%d != 30" % len(frames)
    assert parser.n_frames == 30, parser.n_frames

    for i, fb in enumerate(frames):
        pts, sides = parser.parse(fb)
        exp_xyzv, exp_sn = expect[i]
        a = np.array(pts, dtype=np.float32)
        assert a.shape == exp_xyzv.shape, "第 %d 帧点数不对：%s != %s" % (
            i + 1, a.shape, exp_xyzv.shape)
        assert np.allclose(a, exp_xyzv, atol=1e-5), "第 %d 帧点云数值不对" % (i + 1)
        s = np.array(sides, dtype=np.int16)
        assert np.array_equal(s, exp_sn), "第 %d 帧 SNR 不对" % (i + 1)

    print("[测试1] 切帧 + 解析正确性 .......... 通过（30/30 帧，点云和 SNR 逐点对上）")

    # 全 0 点的帧不能让解析崩掉
    parser2 = FrameParser()
    empty = make_frame(99, np.zeros((0, 4), np.float32), np.zeros((0, 2), np.int16))
    got = parser2.feed(make_frame(98, np.eye(4, dtype=np.float32),
                                  np.zeros((4, 2), np.int16)) + empty)
    assert len(got) == 2
    pts, sides = parser2.parse(got[1])
    assert pts == [] and sides == []
    print("[测试1] 0 点帧（人离开时很常见）.... 通过（不崩、画布会正确清空）")

    # 边界：上电后开头几帧全是 0 点帧，TLV 起点也必须立刻锁定
    parser3 = FrameParser()
    z_pts, z_sn = np.zeros((0, 4), np.float32), np.zeros((0, 2), np.int16)
    got = parser3.feed(make_frame(6, z_pts, z_sn) + make_frame(7, z_pts, z_sn)
                       + make_frame(8, np.eye(4, dtype=np.float32),
                                    np.zeros((4, 2), np.int16)))
    assert len(got) == 3
    pts, _ = parser3.parse(got[0])
    assert pts == [] and parser3._tlv_start is not None, "前导 0 点帧没能立刻锁定 TLV 起点"
    pts, _ = parser3.parse(got[2])
    assert len(pts) == 4
    print("[测试1] 开头连续 0 点帧 ............ 通过（TLV 起点立即锁定）")


# ============================================================ 测试 2：解析吞吐

def test_throughput():
    stream, expect = make_stream(2000, max_pts=60, seed=2)
    parser = FrameParser()
    n_parsed = 0

    t0 = time.perf_counter()
    pos, rng = 0, random.Random(3)
    while pos < len(stream):
        k = rng.randint(1, 4600)          # ≈921600 波特下每 50ms 到达的字节数
        for fb in parser.feed(stream[pos:pos + k]):
            parser.parse(fb)
            n_parsed += 1
        pos += k
    dt = time.perf_counter() - t0

    per_frame_ms = dt / n_parsed * 1000
    mb = len(stream) / 1e6
    print("[测试2] 解析吞吐 .................... %d 帧 (%.1f MB) 用 %.2f s"
          " → %.2f ms/帧 → 单线程极限 ≈ %d fps（板子 20 fps 只占 %.0f%% 的算力）"
          % (n_parsed, mb, dt, per_frame_ms, 1000 / per_frame_ms,
             20 * per_frame_ms / 10))


# ============================================================ 测试 3：绘图耗时

def bench_draw():
    import matplotlib
    print("[信息] 当前绘图后端：%s" % matplotlib.get_backend())
    matplotlib.use("Agg")            # 测耗时不需要真开窗口
    import matplotlib.pyplot as plt

    rng = np.random.default_rng(5)
    for n_pts in (40, 400):
        fig, ax = plt.subplots(figsize=(7, 7))
        ax.set_xlabel("X"); ax.set_ylabel("Z")
        ax.set_xlim(-3, 3); ax.set_ylim(-0.5, 6)
        ax.set_aspect("equal"); ax.grid(True, alpha=0.3)
        ax.axhline(0, color="gray", lw=0.5); ax.axvline(0, color="gray", lw=0.5)
        ax.plot(0, 0, marker="^", color="red", markersize=12)
        scat = ax.scatter([0], [0], s=25, c=[0], cmap="coolwarm", vmin=-1, vmax=1)
        status = ax.text(0.02, 0.98, "bench", transform=ax.transAxes, va="top")

        pts = rng.uniform([-3, 0, -1], [3, 6, 1], size=(n_pts, 3))
        data = np.c_[pts, rng.uniform(-1, 1, n_pts)]

        # ① 整幅重绘（旧版 realtime_viewer 的画法）
        n = 100
        t0 = time.perf_counter()
        for _ in range(n):
            scat.set_offsets(np.c_[data[:, 0], data[:, 2]])
            scat.set_array(data[:, 3])
            status.set_text("第 %d 帧" % n_pts)
            fig.canvas.draw()
        dt_full = (time.perf_counter() - t0) / n * 1000

        # ② blit 方式（新版 realtime_viewer 的画法）：背景拷一次，
        #    每帧只重画散点和状态文字
        fig.canvas.draw()
        bg = fig.canvas.copy_from_bbox(fig.bbox)
        t0 = time.perf_counter()
        for _ in range(n):
            fig.canvas.restore_region(bg)
            scat.set_offsets(np.c_[data[:, 0], data[:, 2]])
            scat.set_array(data[:, 3])
            status.set_text("第 %d 帧" % n_pts)
            ax.draw_artist(scat)
            ax.draw_artist(status)
            fig.canvas.blit(fig.bbox)
        dt_blit = (time.perf_counter() - t0) / n * 1000

        plt.close(fig)
        print("[测试3] %3d 个点：整幅重绘 %.1f ms/次（最高 %d fps）    "
              "blit 方式 %.1f ms/次（最高 %d fps）"
              % (n_pts, dt_full, int(1000 / dt_full), dt_blit, int(1000 / dt_blit)))


# ============================================================ 测试 4：真窗口冒烟

def test_gui_smoke():
    """用默认后端（tkagg）真开一个窗口跑 3 秒 blit 动画，路径和主程序一致。"""
    import queue as qmod
    import threading

    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation

    fig, ax = plt.subplots(figsize=(7, 7))
    fig.canvas.manager.set_window_title("selftest 冒烟（3 秒后自动关闭）")
    ax.set_xlim(-3, 3); ax.set_ylim(-0.5, 6)
    ax.set_aspect("equal"); ax.grid(True, alpha=0.3)
    ax.plot(0, 0, marker="^", color="red", markersize=12)
    scat = ax.scatter([0], [0], s=25, c=[0], cmap="coolwarm", vmin=-1, vmax=1)
    scat.set_offsets(np.empty((0, 2)))
    status = ax.text(0.02, 0.98, "等待数据...", transform=ax.transAxes, va="top")
    fig.canvas.draw()

    q = qmod.Queue()
    rng = np.random.default_rng(9)

    def producer():
        for _ in range(60):                      # 模拟 20fps 数据源，喂 3 秒
            n = int(rng.integers(0, 60))
            pts = [tuple(p) for p in rng.uniform(
                [-3, -3, 0, -1], [3, 3, 6, 1], size=(n, 4))]
            q.put((pts, [(100, 50)] * n, time.time()))
            time.sleep(0.05)

    threading.Thread(target=producer, daemon=True).start()

    errors = []
    ticks = {"n": 0}

    def update(_):
        ticks["n"] += 1
        if ticks["n"] > 60:
            # 不能在本 tick 里直接关窗（matplotlib 收尾 blit 会碰到已销毁画布），
            # 挂单发定时器等这一 tick 结束后再关 —— 和主程序 --seconds 的做法一致
            if not hasattr(fig, "_close_timer"):
                closer = fig.canvas.new_timer(interval=1)
                closer.single_shot = True
                closer.add_callback(plt.close, fig)
                closer.start()
                fig._close_timer = closer
            return scat, status
        try:
            pts, sides, t_arr = None, None, None
            while True:
                try:
                    pts, sides, t_arr = q.get_nowait()
                except qmod.Empty:
                    break
            if pts is not None:
                if pts:
                    a = np.array(pts, dtype=np.float32)
                    scat.set_offsets(np.c_[a[:, 0], a[:, 2]])
                    scat.set_array(a[:, 3])
                else:
                    scat.set_offsets(np.empty((0, 2)))
                status.set_text("模拟帧 点数 %d" % len(pts))
        except Exception as e:                   # 任何报错都记下来，别静默
            errors.append(repr(e))
            plt.close(fig)
        return scat, status

    ani = FuncAnimation(fig, update, init_func=lambda: (scat, status),
                        interval=50, blit=True, cache_frame_data=False)
    plt.show()

    assert ticks["n"] >= 60, "GUI动画未运行足够tick（可能为无显示后端）"
    assert not errors, "动画过程报错：%s" % errors
    print("[测试4] 真窗口 blit 动画 ............ 通过（3 秒 %d tick 无报错）"
          % ticks["n"])


if __name__ == "__main__":
    print("=" * 60)
    print("selftest：realtime_viewer 数据通路自测")
    print("=" * 60)
    test_correctness()
    test_throughput()
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--gui", action="store_true", help="在有真实显示器时执行GUI冒烟；失败会返回非零")
    args = ap.parse_args()
    if args.gui:
        test_gui_smoke()
    else:
        print("[测试4] 真窗口冒烟 ................ 未执行（使用 --gui 启用）")
    bench_draw()
    print("\n全部测完。")
