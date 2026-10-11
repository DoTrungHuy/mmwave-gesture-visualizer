"""实板验证：后台需已运行；只接收数据并切换帧率，不录制。"""
import asyncio
import json
import time
import argparse
import websockets


async def main():
    async with websockets.connect("ws://127.0.0.1:8080/ws") as ws:
        first = json.loads(await ws.recv())
        print("Initial status:", first)
        deadline = time.monotonic() + 10
        while not first.get("connected") and time.monotonic() < deadline:
            first = json.loads(await asyncio.wait_for(ws.recv(), 3))
        assert first.get("connected"), first
        frames = 0
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            message = json.loads(await asyncio.wait_for(ws.recv(), 3))
            if message["type"] == "frame":
                frames += 1
            elif "connected" in message:
                assert message["connected"], message
        print("Frames received in 5 seconds:", frames)
        assert frames > 50
        # 默认只验证稳定模式，避免测试本身再次让已知不稳定的20fps卡死板子。
        for mode in (("15fps", "20fps") if ARGS.experimental_20fps else ("15fps",)):
            await ws.send(json.dumps({"action": "switch_cfg", "mode": mode}))
            deadline = time.monotonic() + 8
            switched = False
            while time.monotonic() < deadline:
                message = json.loads(await asyncio.wait_for(ws.recv(), 3))
                assert not message.get("cfg_error"), message
                if (message.get("cfg_switched", {}).get("mode") == mode or
                        (message.get("connected") and message.get("cfg_mode") == mode)):
                    print("Mode confirmed:", mode)
                    switched = True
                    break
            assert switched, mode
            deadline = time.monotonic() + 3
            resumed = 0
            while time.monotonic() < deadline:
                message = json.loads(await asyncio.wait_for(ws.recv(), 3))
                if message["type"] == "frame":
                    resumed += 1
            print("Frames after switch:", resumed)
            assert resumed > 20
    print("LIVE CONNECTION PASS")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--experimental-20fps", action="store_true", help="会尝试已知不稳定的20fps，可能需要断电复位")
    ARGS = ap.parse_args()
    asyncio.run(main())
