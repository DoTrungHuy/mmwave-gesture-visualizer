"""录制/导出与读取的离线测试，不访问真实串口、不改用户录制目录。"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import web_viewer as viewer


class AcquisitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_export_skips_partial_and_preserves_shapes_and_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            clips = root / 'clips' / 'gesture'
            clips.mkdir(parents=True)
            records = [
                {'clip': 'gesture', 'subject': 's02', 't0': 1, 'cfg_mode': '15fps', 'cfg': 'captured_cfg'},
                {'frame': 1, 't': 1.1, 'pts': [], 'pts_raw': []},
                {'frame': 2, 't': 1.2, 'pts': [[0, .5, 0, .1, 10, 1]], 'pts_raw': [[0, .5, 0, .1, 10, 1]]},
            ]
            (clips / 'complete.jsonl').write_text('\n'.join(json.dumps(x) for x in records), encoding='utf-8')
            (clips / 'incomplete.jsonl.part').write_text('{bad', encoding='utf-8')
            queue = asyncio.Queue()
            with patch.object(viewer, 'REC_DIR', root), patch.object(viewer, 'Q', queue):
                viewer.export_npz(asyncio.get_running_loop())
                await asyncio.sleep(0)
            files = list(root.glob('dataset_*.npz'))
            self.assertEqual(len(files), 1)
            with np.load(files[0], allow_pickle=True) as data:
                meta = json.loads(str(data['meta']))
                key = next(iter(meta['keys']))
                self.assertEqual(data[key].shape, (2,))
                self.assertEqual(data[key][0].shape, (0, 6))
                self.assertEqual(data[key][1].shape, (1, 6))
                self.assertEqual(meta['clip_configs'][key]['text'], 'captured_cfg')
                self.assertEqual(meta['subjects'][key], 's02')
            messages = []
            while not queue.empty():
                messages.append(queue.get_nowait())
            self.assertTrue(any(m.get('export_done', {}).get('n_clips') == 1 for m in messages))
            self.assertEqual(messages[-1]['exporting'], False)

    async def test_invalid_command_does_not_enter_serial_queue(self):
        queue = asyncio.Queue()
        with patch.object(viewer, 'Q', queue), patch.object(viewer, 'CMD') as commands:
            viewer.handle_cmd({'action': 'clip_start', 'label': '../outside'})
            commands.put_nowait.assert_not_called()
            self.assertIn('command_error', queue.get_nowait())

    async def test_reader_closes_on_parse_error(self):
        import queue
        import threading
        import realtime_viewer as realtime
        with patch.object(realtime.serial, 'Serial') as open_port:
            serial = open_port.return_value
            serial.read.return_value = b'data'
            parser = unittest.mock.Mock()
            parser.feed.side_effect = ValueError('bad frame')
            stop, ready = threading.Event(), threading.Event()
            realtime.reader_thread('COM8', queue.Queue(), stop, parser, ready)
            serial.close.assert_called_once()
            self.assertTrue(ready.is_set())
            self.assertTrue(stop.is_set())


if __name__ == '__main__':
    unittest.main()
