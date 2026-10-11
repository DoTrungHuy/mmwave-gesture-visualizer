#!/usr/bin/env python3
"""串口诊断：先关闭网页版/桌面查看器，再运行 python check_ports.py。
默认仅探测、监听；--configure 会发送稳定配置，退出前停止本脚本启动的雷达。
"""
import argparse
import queue
import threading
import time

from serial.tools import list_ports
from realtime_viewer import DEFAULT_CFG, FrameParser, reader_thread, resolve_ports, send_config, probe_cli


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cli")
    ap.add_argument("--data")
    ap.add_argument("--configure", action="store_true", help="下发15fps完整配置，结束时停止雷达")
    ap.add_argument("--seconds", type=float, default=5)
    args = ap.parse_args()
    if not 0 < args.seconds <= 300:
        ap.error("seconds必须在0到300秒之间")
    for port in list_ports.comports():
        print(port.device, port.description, "VID=", port.vid, "PID=", port.pid)
    cli, data = resolve_ports(args)
    if not cli or not data:
        return 1
    print("CLI:", cli, "Data:", data)
    print("CLI回应:", probe_cli(cli))
    parser, frames, stop = FrameParser(), queue.Queue(maxsize=100), threading.Event()
    ready = threading.Event()
    thread = threading.Thread(target=reader_thread, args=(data, frames, stop, parser, ready), daemon=True)
    thread.start()
    ready.wait(3)
    configured = False
    try:
        if stop.is_set() or not ready.is_set():
            return 1
        if args.configure:
            configured = True
            failed = send_config(cli, DEFAULT_CFG)
            if failed:
                print("配置失败:", failed)
                return 1
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline and not stop.wait(0.1):
            pass
        print("收到 %.3f MB / %d 完整帧" % (parser.n_bytes / 1e6, parser.n_frames))
        if parser.n_frames == 0:
            print("无有效帧：请检查雷达是否启动、固件格式、USB连接和串口占用。")
            return 1
        return 0
    finally:
        if configured:
            try:
                send_config(cli, "sensorStop", verbose=False)
            except Exception as exc:
                print("停止雷达失败:", exc)
        stop.set()
        thread.join(2)


if __name__ == "__main__":
    raise SystemExit(main())
