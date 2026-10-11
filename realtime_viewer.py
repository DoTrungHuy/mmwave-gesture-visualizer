#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IWR6843AOP 实时点云显示 —— 自己的可视化（不用 TI 的网站）

这个程序干三件事（就这三件）：
    1. 往 CLI 口发配置，让板子开始工作
    2. 从 Data 口不停收字节，切成一帧一帧
    3. 把每一帧里的点画出来

运行前必须先关掉 TI 的可视化网页！串口是独占的，被占用会打不开。

用法：
    D:\\mmwave\\.venv\\Scripts\\python.exe realtime_viewer.py
    D:\\mmwave\\.venv\\Scripts\\python.exe realtime_viewer.py --cli COM7 --data COM8
    D:\\mmwave\\.venv\\Scripts\\python.exe realtime_viewer.py --no-config   # 板子已经在跑
"""

import argparse
import queue
import struct
import sys
import threading
import time

import numpy as np
import serial
import serial.tools.list_ports as list_ports

import matplotlib
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

# 中文显示
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


# ============================================================ 常量

# 帧头开头的魔术字。数据流里搜到这 8 个字节，就是某一帧的起点。
MAGIC = b"\x02\x01\x04\x03\x06\x05\x08\x07"

SILABS_VID = 0x10C4   # Silicon Labs（CP2105 属于这家）
CLI_BAUD = 115200     # Enhanced COM：发配置用
DATA_BAUD = 921600    # Standard COM：收数据用

TLV_POINTS = 1        # 点云：每个点 4 个 float32 = x, y, z, velocity
TLV_SIDE_INFO = 7     # 每个点的 snr / noise：2 个 int16

# ============================================================ 推荐手势识别配置（近场优化 + 高速捕获 + 瀑布图支持）

# 20fps 主用配置：3TX全开3D，多普勒32环(0.22m/s速度高分辨率)，
# 近场FOV裁至0.05~1.2m彻底滤除背景杂散点，速度上限放宽至±3.5m/s，CFAR门限优化为10dB，
# 并开启距离谱(guiMonitor第3参=1)供网页端距离-时间瀑布图(RTM)使用。
CFG_GESTURE_20FPS = """
sensorStop
flushCfg
dfeDataOutputMode 1
channelCfg 15 7 0
adcCfg 2 1
adcbufCfg -1 0 1 1 1
profileCfg 0 60 300 7 57.14 0 0 70 1 256 5209 0 0 158
chirpCfg 0 0 0 0 0 0 0 1
chirpCfg 1 1 0 0 0 0 0 2
chirpCfg 2 2 0 0 0 0 0 4
frameCfg 0 2 32 0 50 1 0
lowPower 0 0
guiMonitor -1 1 1 0 0 0 1
cfarCfg -1 0 2 8 4 3 0 10 0
cfarCfg -1 1 0 4 2 3 1 15 1
multiObjBeamForming -1 1 0.5
clutterRemoval -1 0
calibDcRangeSig -1 0 -5 8 256
extendedMaxVelocity -1 0
lvdsStreamCfg -1 0 0 0
compRangeBiasAndRxChanPhase 0.0 1 0 1 0 1 0 1 0 1 0 1 0 1 0 1 0 1 0 1 0 1 0 1 0
measureRangeBiasAndRxChanPhase 0 1.5 0.2
CQRxSatMonitor 0 3 5 121 0
CQSigImgMonitor 0 127 4
analogMonitor 0 0
aoaFovCfg -1 -90 90 -90 90
cfarFovCfg -1 0 0.05 1.20
cfarFovCfg -1 1 -3.50 3.50
sensorStart
"""

# 15fps 保守备用配置（帧周期 66ms，放宽板载 DSP 处理时间，防止长时间高发热丢帧）
CFG_GESTURE_15FPS = """
sensorStop
flushCfg
dfeDataOutputMode 1
channelCfg 15 7 0
adcCfg 2 1
adcbufCfg -1 0 1 1 1
profileCfg 0 60 300 7 57.14 0 0 70 1 256 5209 0 0 158
chirpCfg 0 0 0 0 0 0 0 1
chirpCfg 1 1 0 0 0 0 0 2
chirpCfg 2 2 0 0 0 0 0 4
frameCfg 0 2 32 0 66 1 0
lowPower 0 0
guiMonitor -1 1 1 0 0 0 1
cfarCfg -1 0 2 8 4 3 0 10 0
cfarCfg -1 1 0 4 2 3 1 15 1
multiObjBeamForming -1 1 0.5
clutterRemoval -1 0
calibDcRangeSig -1 0 -5 8 256
extendedMaxVelocity -1 0
lvdsStreamCfg -1 0 0 0
compRangeBiasAndRxChanPhase 0.0 1 0 1 0 1 0 1 0 1 0 1 0 1 0 1 0 1 0 1 0 1 0 1 0
measureRangeBiasAndRxChanPhase 0 1.5 0.2
CQRxSatMonitor 0 3 5 121 0
CQSigImgMonitor 0 127 4
analogMonitor 0 0
aoaFovCfg -1 -90 90 -90 90
cfarFovCfg -1 0 0.05 1.20
cfarFovCfg -1 1 -3.50 3.50
sensorStart
"""

# 20fps 在当前实板上会停止响应；默认使用已通过连续采集验证的 15fps。
DEFAULT_CFG = CFG_GESTURE_15FPS



# ============================================================ 第一步：找串口

def find_ports():
    """列出 Silicon Labs 的串口（CP2105 会出两个）"""
    out = []
    for p in list_ports.comports():
        if getattr(p, "vid", None) == SILABS_VID:
            out.append(p)
    return sorted(out, key=lambda p: p.device)


def probe_cli(port, timeout=0.8):
    """
    判断某个串口是不是 CLI 口。
    办法：往里发个回车，正在跑 demo 的板子会回显 'mmwDemo:/>'。
    数据口只会回一堆二进制乱码，不会出现这个字符串。
    """
    try:
        with serial.Serial(port, CLI_BAUD, timeout=0.1) as s:
            s.reset_input_buffer()
            s.write(b"\r\n")
            buf = b""
            t0 = time.time()
            while time.time() - t0 < timeout:
                buf += s.read(4096)
            return b"mmwDemo" in buf or b"Done" in buf or b"/>" in buf
    except Exception:
        return False


def resolve_ports(args):
    if args.cli and args.data:
        return args.cli, args.data

    silabs = find_ports()
    explicit = args.cli or args.data
    if explicit:
        selected = next((p for p in silabs if p.device == explicit), None)
        if selected is None:
            return None, None
        peers = [p for p in silabs if p.device != explicit and
                 p.serial_number == selected.serial_number]
        if len(peers) != 1:
            print("[!] 无法唯一确定配对串口，请同时指定 --cli 和 --data")
            return None, None
        return (args.cli or peers[0].device, args.data or peers[0].device)
    identities = {p.serial_number or (p.location or "").split(":")[0] for p in silabs}
    if len(identities) > 1:
        print("[!] 检测到多块板子，请明确指定 --cli 和 --data")
        return None, None
    if not silabs:
        print("[!] 没找到 Silicon Labs 的串口。")
        print("    检查：板子插好了吗？设备管理器里有 CP2105 的两个口吗？")
        print("    当前系统里的串口：")
        for p in list_ports.comports():
            print("      %-8s %s" % (p.device, p.description))
        return None, None

    # CP2105 官方标准命名：Enhanced COM Port 为 CLI 配置口，Standard COM Port 为 Data 数据口
    enhanced = next((p.device for p in silabs if "enhanced" in (p.description or "").lower()), None)
    standard = next((p.device for p in silabs if "standard" in (p.description or "").lower()), None)
    if enhanced and standard:
        print("[*] 依据 CP2105 描述精准匹配：CLI=%s (Enhanced), Data=%s (Standard)" % (enhanced, standard))
        return enhanced, standard

    cands = [p.device for p in silabs]
    print("[*] 找到 Silicon Labs 串口：%s" % ", ".join(cands))
    cli = next((c for c in cands if probe_cli(c)), None)
    if cli is None:
        print("[!] 握手识别不出 CLI 口，按 COM 编号顺序推断。")
        cli = cands[0]
    data = next((c for c in cands if c != cli), None)
    return cli, data


# ============================================================ 第二步：发配置

def comp_range_bias_candidates(line):
    """
    compRangeBiasAndRxChanPhase 的参数个数 = 1（rangeBias）+ 2 × (Tx 数 × Rx 数)。
    6843AOP 是 3Tx × 4Rx = 12 组，但不同 SDK / 不同器件会有差别，
    所以这里生成一串候选，让板子自己挑哪个能接受。
    """
    toks = line.split()
    bias = toks[1] if len(toks) > 1 else "0.0"
    out = []
    for n_pairs in (12, 11, 8, 6, 4, 2, 16, 9):
        out.append("compRangeBiasAndRxChanPhase " + " ".join([bias] + ["1", "0"] * n_pairs))
    return out


def _send_one(s, cmd, timeout=2.0):
    """发一条命令，读到 CLI 提示符为止。返回 (是否成功, 板子回的内容)"""
    s.reset_input_buffer()
    s.write((cmd + "\n").encode())
    buf = b""
    t0 = time.time()
    while time.time() - t0 < timeout:
        chunk = s.read(4096)
        if chunk:
            buf += chunk
            if b"mmwDemo:/>" in buf:
                break
    text = buf.decode("utf-8", errors="replace")
    return ("Done" in text) or ("Ignored" in text), text


def send_config(cli_port, cfg_text, verbose=True):
    """
    把配置一行一行发给板子。

    两点要注意：
      1. 板子会**先回显命令，再回 Done**，所以必须一直读到提示符出现，
         不能"读到一点数据就返回"，否则会把成功的命令误判成失败。
      2. compRangeBiasAndRxChanPhase 的参数个数随器件变化，所以给它几个候选，
         哪个被接受就用哪个。
    """
    lines = [l.strip() for l in cfg_text.splitlines()
             if l.strip() and not l.strip().startswith(("%", "#"))]

    failed = []
    with serial.Serial(cli_port, CLI_BAUD, timeout=0.2) as s:
        time.sleep(0.3)
        s.reset_input_buffer()
        for line in lines:
            # 给这条命令准备候选（普通命令只有一个候选，就是它自己）
            if line.startswith("compRangeBiasAndRxChanPhase"):
                # 自定义标定系数必须优先保留，拒绝后才尝试兼容参数数量。
                candidates = [line]  # 不静默修改标定系数或参数数量；拒绝则报告错误
            else:
                candidates = [line]

            ok, text, used = False, "", line
            for cand in candidates:
                ok, text = _send_one(s, cand)
                if ok:
                    used = cand
                    break

            is_sensor_start = line.lower().startswith("sensorstart")

            if ok:
                n_par = len(used.split()) - 1
                if used != line:
                    print("    [*] %-20s -> Done（自动改用 %d 个参数）"
                          % ("compRangeBiasAndRxChanPhase", n_par))
                elif is_sensor_start:
                    print("    %-24s -> Done（板子开始工作）" % line)
                elif verbose:
                    print("    %-24s -> Done" % line)
            else:
                silent = not text.strip()
                failed.append(line + (" (无响应)" if silent else ""))
                print("    [!] %-20s -> %s" % (line, text.strip()[:150] or "(无响应)"))
                # 不完整的配置不能继续 sensorStart；拒绝或超时均立即终止。
                break

            if is_sensor_start:
                break

    return failed


def switch_frame_rate(cli_port, target_mode):
    """当前固件不支持仅修改 frameCfg 后重新启动，必须下发完整配置。

    当前20fps已实测停止响应，在重新完成稳定性验证前拒绝启用。
    调用方必须在整个配置期间保持数据接收线程运行。
    """
    if target_mode != "15fps":
        return ["20fps 已禁用（实板稳定性未通过）" if target_mode == "20fps"
                else "未知帧率模式: %s" % target_mode]
    return send_config(cli_port, CFG_GESTURE_15FPS)


# ============================================================ 第三步：切帧 + 解析

class FrameParser:
    """
    把连续的字节流切成一帧一帧。

    数据流里帧和帧是首尾相连的，没有分隔符。所以必须：
      1. 在缓冲区里搜魔术字 → 找到帧起点
      2. 从帧头里读出"这一帧有多长"
      3. 凑够这么多字节，才切出一帧
      4. 剩下的字节留在缓冲区，等下一轮
    """

    def __init__(self):
        self.buf = bytearray()
        self.n_frames = 0
        self.n_bytes = 0
        self._layout = None          # 包长度字段的偏移（自动嗅探出来的）
        self._tlv_start = None       # TLV 区从帧头起算的偏移（自动嗅探出来的）

    def feed(self, chunk):
        self.buf += chunk
        self.n_bytes += len(chunk)
        frames = []
        while True:
            frame = self._pop_one()
            if frame is None:
                break
            frames.append(frame)
            self.n_frames += 1
        # 缓冲区太大说明帧同步丢了，清掉重来
        if len(self.buf) > 4_000_000:
            self.buf.clear()
            self._layout = None
            self._tlv_start = None
        return frames

    def _pop_one(self):
        """从缓冲区里切出一帧，切不出来返回 None"""
        i = self.buf.find(MAGIC)
        if i < 0:
            # 没找到魔术字，只保留最后 7 个字节（可能是半个魔术字）
            if len(self.buf) > 8:
                del self.buf[:-7]
            return None
        if i > 0:
            del self.buf[:i]          # 丢掉魔术字之前的垃圾字节

        if len(self.buf) < 12:
            return None               # 还不够读包长度

        plen_off = self._find_packet_len_offset()
        if plen_off is None:
            # 布局还没定下来（需要至少看到两帧才能嗅探）。
            # 这里必须"继续等更多数据"，绝对不能丢字节 ——
            # 否则会把一整帧的帧头扔掉，导致丢帧。
            if len(self.buf) < 65536:
                return None
            # 缓冲区都堆到 64 KB 还定不下来，说明这个魔术字是假的，跳过它
            del self.buf[:8]
            return None

        if len(self.buf) < plen_off + 4:
            return None
        plen = struct.unpack_from("<I", self.buf, plen_off)[0]
        if plen <= 0 or plen > 2_000_000:
            del self.buf[:8]
            return None
        if len(self.buf) < plen:
            return None               # 这一帧还没收全

        frame_bytes = bytes(self.buf[:plen])
        del self.buf[:plen]
        return frame_bytes

    def _find_packet_len_offset(self):
        """
        自动找出"包长度"字段在帧头里的偏移。
        办法：它的值必须等于"到下一个魔术字的距离"。这个条件很难碰巧满足。
        """
        if self._layout is not None:
            return self._layout

        p0 = 0
        p1 = self.buf.find(MAGIC, 8)
        if p1 < 0:
            return None
        gap = p1 - p0
        for off in range(8, 80, 4):
            if off + 4 > len(self.buf):
                break
            if struct.unpack_from("<I", self.buf, off)[0] == gap:
                self._layout = off
                print("[*] 自动识别出帧头布局：包长度在偏移 %d 处" % off)
                return off
        return None

    def _find_tlv_start(self, frame_bytes, plen):
        """
        找出 TLV 区从帧头起算的偏移。

        帧头长度随 SDK 版本变化（40 或 48 字节都可能），所以不能写死。
        用一条很强的约束来试：**从某个偏移开始逐个读 TLV，各段长度之和必须
        恰好等于包长度**。只有真正的位置才满足。

        长度为 0 的 TLV 也算合法（前方没人时点云 TLV 就是空的，上电后头几帧
        很容易全是 0 点帧），但整条链至少要有一个非空 TLV 兜底，否则约束就松了。

        再加一道旁证：帧头里的 numTLVs 字段（在包长度字段后 16 字节处）记录了
        这一帧有几个 TLV，链里数出来的个数要和它相等才优先采纳 —— 没有它，
        可能会选中"提前 8 字节"的偏移（帧头尾部的 numTLVs+padding 恰好被当成
        一个空 TLV），解析结果虽然一样，但没必要。
        """
        if self._tlv_start is not None:
            return self._tlv_start

        # numTLVs 旁证：读不到（布局没定/字段异常）就当没有，不影响兜底逻辑
        n_expect = None
        if self._layout is not None and self._layout + 20 <= plen:
            v = struct.unpack_from("<I", frame_bytes, self._layout + 16)[0]
            if 0 < v <= 64:
                n_expect = v

        best = None
        for ts in range(8, 96, 4):
            if ts + 8 > plen:
                break
            pos, n_tlv, n_nonzero, ok = ts, 0, 0, True
            while pos < plen:
                # TI UART 包可能以不足32字节的零填充对齐。
                if n_tlv > 0 and plen - pos < 32 and not any(frame_bytes[pos:plen]):
                    pos = plen
                    break
                if pos + 8 > plen:
                    ok = False
                    break
                t, ln = struct.unpack_from("<II", frame_bytes, pos)
                if t == 0 or t > 64 or ln % 4 != 0 or ln > plen:
                    ok = False
                    break
                n_tlv += 1
                if ln > 0:
                    n_nonzero += 1
                pos += 8 + ln
            if ok and pos == plen and n_nonzero > 0:
                if best is None:
                    best = ts               # 第一个合法的先记下，兜底用
                if n_expect is not None and n_tlv == n_expect:
                    best = ts               # 和帧头 numTLVs 对上的优先
                    break
        if best is not None:
            self._tlv_start = best
            print("[*] 自动识别出 TLV 起点：帧头 %d 字节" % best)
        return best

    def parse(self, frame_bytes):
        """从一帧里取出点云"""
        plen = struct.unpack_from("<I", frame_bytes, self._layout)[0]

        ts = self._find_tlv_start(frame_bytes, plen)
        if ts is None:
            return [], []

        points, sides = [], []
        pos = ts
        while pos + 8 <= plen:
            t, ln = struct.unpack_from("<II", frame_bytes, pos)
            if t == 0 or ln % 4 != 0 or pos + 8 + ln > plen:
                break
            payload = frame_bytes[pos + 8: pos + 8 + ln]
            if t == TLV_POINTS:
                for k in range(len(payload) // 16):
                    points.append(struct.unpack_from("<4f", payload, k * 16))
            elif t == TLV_SIDE_INFO:
                for k in range(len(payload) // 4):
                    sides.append(struct.unpack_from("<2h", payload, k * 4))
            pos += 8 + ln
        return points, sides


# ============================================================ 第四步：读取线程

def reader_thread(data_port, out_queue, stop_event, parser, ready_event=None):
    """
    独立线程，只管从串口读字节、切帧、塞进队列。
    和画图分开是必须的：读串口是微秒级，画图是几十毫秒级，混在一起必丢数据。
    """
    try:
        ser = serial.Serial(data_port, DATA_BAUD, timeout=0.05)
    except Exception as e:
        print("[!] 打不开数据口 %s：%s" % (data_port, e))
        print("    是不是 TI 的可视化网页还开着？串口是独占的。")
        stop_event.set()
        return

    print("[*] 数据口 %s 已打开，开始接收..." % data_port)
    if ready_event:
        ready_event.set()
    try:
        while not stop_event.is_set():
            chunk = ser.read(8192)
            if not chunk:
                continue
            for frame in parser.feed(chunk):
                pts, sides = parser.parse(frame)
                item = (pts, sides, time.time())
                try:
                    out_queue.put_nowait(item)
                except queue.Full:
                    try:
                        out_queue.get_nowait()
                        out_queue.put_nowait(item)
                    except (queue.Empty, queue.Full):
                        pass
    except Exception as e:
        print("[!] 数据接收/解析出错：%s" % e)
        stop_event.set()
    finally:
        ser.close()


# ============================================================ 主程序

def main():
    ap = argparse.ArgumentParser(description="IWR6843AOP 实时点云显示")
    ap.add_argument("--cli", help="Enhanced COM（配置口），如 COM7")
    ap.add_argument("--data", help="Standard COM（数据口），如 COM8")
    ap.add_argument("--cfg", help="配置文件路径（可视化工具 SAVE CONFIG TO PC 导出的）")
    ap.add_argument("--no-config", action="store_true", help="不重发配置（板子已在跑）")
    ap.add_argument("--seconds", type=float, default=0, help="跑多少秒后自动退出，0=一直跑")
    ap.add_argument("--dump", type=float, default=0,
                    help="诊断模式：只打开数据口收 N 秒，打印收到多少字节，不发配置")
    args = ap.parse_args()

    print("=" * 56)
    print("IWR6843AOP 实时点云显示")
    print("=" * 56)
    print("提醒：如果 TI 的可视化网页开着，先关掉它。\n")

    # --- 找串口 ---
    cli, data = resolve_ports(args)
    if not cli or not data:
        sys.exit(1)
    print("[*] CLI 口 = %s    Data 口 = %s\n" % (cli, data))

    # --- 诊断模式：只收数据，不发配置 ---
    if args.dump > 0:
        print("[*] 诊断模式：只打开数据口 %s 收 %.1f 秒，不发配置" % (data, args.dump))
        print("    （板子得已经在跑，才会收到东西）\n")
        try:
            ser = serial.Serial(data, DATA_BAUD, timeout=0.5)
        except Exception as e:
            sys.exit("[!] 打不开数据口 %s：%s" % (data, e))
        n, t0, last = 0, time.time(), 0.0
        while time.time() - t0 < args.dump:
            n += len(ser.read(65536))
            if time.time() - last >= 1.0:
                last = time.time()
                print("    %.1f 秒  累计收到 %d 字节" % (time.time() - t0, n))
        ser.close()
        print("\n[*] 诊断结束：共收到 %d 字节" % n)
        if n == 0:
            print("[!] 数据口一个字节都没有 —— 说明板子根本没在往外发数据，")
            print("    不是解析的问题。检查：S3 开关是否 OFF、板子固件是否在跑。")
        else:
            print("[*] 数据口有数据！说明板子在跑，问题在配置或解析环节。")
        return

    # 数据接收必须在sensorStart之前就绪。
    parser = FrameParser()
    q = queue.Queue(maxsize=100)
    stop = threading.Event()
    ready = threading.Event()
    th = threading.Thread(target=reader_thread,
                          args=(data, q, stop, parser, ready), daemon=True)
    th.start()
    if not ready.wait(3) or stop.is_set():
        stop.set()
        th.join(2)
        sys.exit("数据口未就绪，未下发配置")

    # --- 发配置 ---
    if not args.no_config:
        cfg_text = DEFAULT_CFG
        if args.cfg:
            with open(args.cfg, encoding="utf-8", errors="replace") as f:
                cfg_text = f.read()
            print("[*] 使用配置文件：%s" % args.cfg)
        else:
            print("[*] 使用内置默认配置")
        print("[*] 正在给板子发配置：")
        failed = send_config(cli, cfg_text)
        if failed:
            print()
            print("!" * 62)
            print("[!] 有 %d 条命令被板子拒绝：" % len(failed))
            for f in failed:
                print("      %s" % f)
            print()
            print("    demo 固件要求配置必须完整才能启动，所以只要有一条失败，")
            print("    sensorStart 就会报 'Full configuration must be provided'，")
            print("    板子根本不会出数据。先解决上面这些命令再往下走。")
            print("!" * 62)
            stop.set()
            th.join(2)
            sys.exit(1)
        print()

    # --- 画图 ---
    fig, ax = plt.subplots(figsize=(7, 7))
    fig.canvas.manager.set_window_title("IWR6843AOP 实时点云（俯视图）")

    ax.set_xlabel("X 左右 (m)")
    ax.set_ylabel("Y 前方 (m)")
    ax.set_xlim(-3, 3)
    ax.set_ylim(-0.5, 6)
    ax.set_aspect("equal")
    ax.grid(True, alpha=0.3)
    ax.axhline(0, color="gray", lw=0.5)
    ax.axvline(0, color="gray", lw=0.5)
    # 雷达所在位置画个标记
    ax.plot(0, 0, marker="^", color="red", markersize=12)
    ax.text(0.1, -0.35, "雷达", color="red", fontsize=11)

    scat = ax.scatter([0], [0], s=25, c=[0], cmap="coolwarm",
                      vmin=-1, vmax=1, edgecolors="none")
    scat.set_offsets(np.empty((0, 2)))      # 先清空，等数据来
    # 状态文字放在坐标区里面：blit 只能刷新坐标区内的元素，窗口标题栏那种不行
    status = ax.text(0.02, 0.98, "等待数据...", transform=ax.transAxes,
                     va="top", fontsize=10,
                     bbox=dict(boxstyle="round", fc="white", ec="0.7", alpha=0.85))
    fig.canvas.draw()      # 先整幅画一次，作为 blit 要用的静态底图

    t_start = time.time()
    counter = {"n": 0}
    warned = {"done": False}
    # 绘制性能统计：实际画出的帧率、每帧从进队列到画上屏的延迟
    perf = {"drawn": 0, "lat_sum": 0.0, "lat_n": 0,
            "fps": 0.0, "lat_ms": 0.0, "t0": time.time()}

    def update(_):
        # 把队列里积压的帧都取出来，只画最新的那一帧
        pts, sides, t_arrive = None, None, None
        while True:
            try:
                pts, sides, t_arrive = q.get_nowait()
            except queue.Empty:
                break

        if pts is not None:
            counter["n"] += 1
            perf["drawn"] += 1
            perf["lat_sum"] += time.time() - t_arrive
            perf["lat_n"] += 1
            if pts:
                a = np.array(pts, dtype=np.float32)
                scat.set_offsets(np.c_[a[:, 0], a[:, 1]])   # x 和 y (TI标准: Y朝前)
                # 用速度上色：红=靠近，蓝=远离
                scat.set_array(a[:, 3])
            else:
                scat.set_offsets(np.empty((0, 2)))

            avg_snr = ""
            if sides:
                avg_snr = "  平均SNR %.1f dB" % (sum(s[0] for s in sides) / len(sides) / 10)

            status.set_text("第 %d 帧   点数 %d   已收 %.2f MB   绘制 %.1f fps   延迟 %.0f ms%s"
                            % (parser.n_frames, len(pts), parser.n_bytes / 1e6,
                               perf["fps"], perf["lat_ms"], avg_snr))

        # 每 2 秒统计一次实际绘制帧率和延迟，顺便往命令行打状态（窗口不动时看这里）。
        # 延迟 = 帧进队列到画上屏，不含雷达本身处理和串口传输的时间。
        now = time.time()
        if now - perf["t0"] >= 2.0:
            span = now - perf["t0"]
            perf["fps"] = perf["drawn"] / span
            perf["lat_ms"] = (perf["lat_sum"] / perf["lat_n"] * 1000) if perf["lat_n"] else 0.0
            perf["drawn"] = 0
            perf["lat_sum"] = 0.0
            perf["lat_n"] = 0
            perf["t0"] = now
            print("    [状态] 收到 %.3f MB / %d 帧，绘制 %.1f fps，延迟 %.0f ms，队列积压 %d"
                  % (parser.n_bytes / 1e6, parser.n_frames, perf["fps"], perf["lat_ms"],
                     q.qsize()))

        # 静默超过 6 秒：自动给出排查建议（只提示一次）
        if parser.n_bytes == 0 and not warned["done"] and now - t_start > 6:
            warned["done"] = True
            print()
            print("!" * 60)
            print("[!] 已经 6 秒没收到任何字节。串口是打开的，但板子没发数据。")
            print()
            print("    最可能的原因：CLI 口和 Data 口选反了。")
            print("    我的识别办法是往每个口发个回车，看哪个回 'mmwDemo:/>'。")
            print("    如果板子当时没在跑固件，两个口都不回，就只能按 COM 编号猜。")
            print()
            print("    请手动交换试一次：")
            print("      --cli %s --data %s" % (data, cli))
            print()
            print("    如果交换后还是 0 字节，那问题不在口上，检查：")
            print("      1. S3 开关是否在 OFF（ON 是烧录模式，固件不运行）")
            print("      2. 上面配置下发那段是不是全是 [!]（说明配置没成功）")
            print("      3. 打开 TI 可视化网页试试 —— 网页也出不来就是板子的问题")
            print("!" * 60)
            print()

        if args.seconds and now - t_start > args.seconds:
            # 不能在本 tick 里直接 plt.close(fig)：update 返回后 matplotlib
            # 还要做一次 blit 收尾，那时画布已销毁会报错。挂个单发定时器，
            # 等这一 tick 结束、回到事件循环后再关窗口。
            if not hasattr(fig, "_close_timer"):
                closer = fig.canvas.new_timer(interval=1)
                closer.single_shot = True
                closer.add_callback(plt.close, fig)
                closer.start()
                fig._close_timer = closer     # 保持引用，防止定时器被回收
        return scat, status

    def init():
        scat.set_offsets(np.empty((0, 2)))
        status.set_text("等待数据...")
        return scat, status

    # blit=True：每个 tick 只重画散点和状态文字（几 ms），网格/坐标轴这些背景
    # 不再整幅重绘（~30ms）。帧率提到 20fps 后依然绰绰有余。
    ani = FuncAnimation(fig, update, init_func=init, interval=50,
                        blit=True, cache_frame_data=False)

    print("\n[*] 窗口已打开。手伸到雷达前试试，应该能看到点跟着动。")
    print("    关掉窗口就退出。\n")
    try:
        plt.show()
    except KeyboardInterrupt:
        pass
    finally:
        if not args.no_config:
            try:
                send_config(cli, "sensorStop", verbose=False)
            except Exception as exc:
                print("[!] 停止雷达失败:", exc)
        stop.set()
        th.join(2)

    print("\n" + "=" * 56)
    print("统计")
    print("=" * 56)
    print("收到字节   : %.2f MB" % (parser.n_bytes / 1e6))
    print("完整帧数   : %d" % parser.n_frames)
    print("绘图刷新   : %d 次" % counter["n"])
    if parser.n_frames == 0:
        print("\n[!] 一帧都没收到。检查：")
        print("    1. TI 的可视化网页关了吗？（串口独占）")
        print("    2. 配置发成功了吗？（看上面有没有 Done）")
        print("    3. --cli / --data 是不是反了？")


if __name__ == "__main__":
    main()
