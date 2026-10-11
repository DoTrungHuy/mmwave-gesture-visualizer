"""连接状态回归测试（无需板子）：python -m unittest test_connection -v"""
import asyncio
import json
import unittest
from unittest.mock import patch

import web_viewer as viewer
from fastapi import WebSocketDisconnect
from selftest import make_stream
from realtime_viewer import FrameParser


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_status_keeps_connection_without_replaying_events(self):
        original = viewer.LAST_STATUS.copy()
        queue = asyncio.Queue()
        try:
            viewer.LAST_STATUS.clear()
            viewer.LAST_STATUS.update(type="status", connected=False)
            with patch.object(viewer, "Q", queue), patch.object(viewer, "CLIENTS", set()):
                task = asyncio.create_task(viewer.broadcast_loop())
                queue.put_nowait({"type": "status", "connected": True, "phase": "运行中", "cli": "COM7"})
                queue.put_nowait({"type": "status", "cfg_error": "test"})
                await asyncio.sleep(0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertTrue(viewer.LAST_STATUS["connected"])
            self.assertEqual(viewer.LAST_STATUS["cli"], "COM7")
            self.assertNotIn("cfg_error", viewer.LAST_STATUS)
        finally:
            viewer.LAST_STATUS.clear()
            viewer.LAST_STATUS.update(original)

    async def test_new_browser_receives_full_connection_status(self):
        class Socket:
            sent = []
            async def accept(self):
                pass
            async def send_text(self, text):
                self.sent.append(json.loads(text))
            async def receive_json(self):
                raise WebSocketDisconnect()
        socket = Socket()
        with patch.object(viewer, "LAST_STATUS", {"type": "status", "connected": True, "phase": "运行中", "cli": "COM7", "data": "COM8"}), patch.object(viewer, "CLIENTS", set()):
            await viewer.ws_endpoint(socket)
        self.assertTrue(socket.sent[0]["connected"])
        self.assertEqual(socket.sent[0]["data"], "COM8")

    async def test_invalid_points_do_not_disconnect(self):
        from selftest import make_frame
        parser = FrameParser()
        invalid = make_frame(1, [[0, 1, 0, float("nan")], [float("inf"), 1, 0, 0], [0, 0.5, 0, 0.1]], [[100, 10]] * 3)
        frames = parser.feed(invalid + invalid)
        parsed, _, _, _ = viewer.parse_all(parser, frames[0])
        self.assertEqual(len(parsed), 1)
        json.dumps(parsed, allow_nan=False)

    async def test_20fps_rejected_without_touching_serial(self):
        import realtime_viewer as realtime
        with patch.object(realtime.serial, "Serial") as serial_open:
            self.assertTrue(realtime.switch_frame_rate("COM7", "20fps"))
            serial_open.assert_not_called()
        queue = asyncio.Queue()
        with patch.object(viewer, "Q", queue), patch.object(viewer, "CMD") as commands:
            viewer.handle_cmd({"action": "switch_cfg", "mode": "20fps"})
            commands.put_nowait.assert_not_called()
            self.assertIn("已禁用", queue.get_nowait()["cfg_error"])

    async def test_switch_uses_full_configuration(self):
        import realtime_viewer as realtime
        with patch.object(realtime, "send_config", return_value=[]) as send:
            self.assertEqual(realtime.switch_frame_rate("COM7", "15fps"), [])
            send.assert_called_once_with("COM7", realtime.CFG_GESTURE_15FPS)

    async def test_config_aborts_before_start_on_rejection(self):
        import realtime_viewer as realtime
        with patch.object(realtime.serial, "Serial"), patch.object(realtime.time, "sleep"), patch.object(realtime, "_send_one", side_effect=[(True, "Done"), (False, "Error")]) as send:
            failed = realtime.send_config("COM7", "  % comment\nsensorStop\nflushCfg\nsensorStart", verbose=False)
            self.assertEqual(failed, ["flushCfg"])
            self.assertEqual([c.args[1] for c in send.call_args_list], ["sensorStop", "flushCfg"])

    async def test_clip_validation(self):
        for values in ({"label": "../escape"}, {"label": "D:\\outside"}, {"subject": "a/b"}, {"label": []}, {"dur": "bad"}, {"dur": float("nan")}, {"label": "CON"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                viewer.validate_clip_command({"action": "clip_start", **values})
        valid = viewer.validate_clip_command({"label": "测试手势", "subject": "s01", "dur": 2})
        self.assertEqual(valid["dur"], 2.0)

    async def test_original_calibration_preserved(self):
        import realtime_viewer as realtime
        line = "compRangeBiasAndRxChanPhase 0.2 0.7 0.3"
        with patch.object(realtime.serial, "Serial"), patch.object(realtime.time, "sleep"), patch.object(realtime, "_send_one", return_value=(True, "Done")) as send:
            self.assertEqual(realtime.send_config("COM7", line, verbose=False), [])
            self.assertEqual(send.call_args.args[1], line)

    async def test_padded_frame(self):
        import struct
        from selftest import make_frame
        frame = bytearray(make_frame(1, [[0, 0.5, 0, 0.1]], [[100, 10]]))
        frame.extend(b"\x00" * ((-len(frame)) % 32))
        struct.pack_into("<I", frame, 16, len(frame))
        parser = FrameParser()
        got = parser.feed(bytes(frame) * 2)
        self.assertEqual(len(parser.parse(got[0])[0]), 1)
        self.assertEqual(len(viewer.parse_all(parser, got[0])[0]), 1)

    async def test_stable_default(self):
        from realtime_viewer import DEFAULT_CFG, CFG_GESTURE_15FPS
        self.assertEqual(DEFAULT_CFG, CFG_GESTURE_15FPS)
        self.assertEqual(viewer.ACTIVE_CFG_MODE, "15fps")

    async def test_web_frame_parser(self):
        stream, expected = make_stream(30)
        parser = FrameParser()
        frames = parser.feed(stream)
        self.assertEqual(len(frames), 30)
        for frame, (points, _) in zip(frames, expected):
            parsed, rp, rd, dv = viewer.parse_all(parser, frame)
            self.assertEqual(len(parsed), len(points))
            self.assertEqual(len(rp), viewer.RD_RANGE_BINS)
            self.assertEqual(len(dv), viewer.DV_BINS)


if __name__ == "__main__":
    unittest.main()
