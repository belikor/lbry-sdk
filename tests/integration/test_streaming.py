import aiohttp
import aiohttp.web
import os
import hashlib
from lbrynet.utils import aiohttp_request
from lbrynet.blob.blob_file import MAX_BLOB_SIZE
from lbrynet.testcase import CommandTestCase


def get_random_bytes(n: int) -> bytes:
    result = b''.join(hashlib.sha256(os.urandom(4)).digest() for _ in range(n // 16))
    if len(result) < n:
        result += os.urandom(n - len(result))
    elif len(result) > n:
        result = result[:-(len(result) - n)]
    assert len(result) == n, (n, len(result))
    return result


class RangeRequests(CommandTestCase):
    async def _restart_stream_manager(self):
        self.daemon.stream_manager.stop()
        await self.daemon.stream_manager.start()
        return

    async def _setup_stream(self, data: bytes, save_blobs: bool = True, streaming_only: bool = True):
        self.daemon.conf.save_blobs = save_blobs
        self.daemon.conf.streaming_only = streaming_only
        self.data = data
        await self.stream_create('foo', '0.01', data=self.data)
        if save_blobs:
            self.assertTrue(len(os.listdir(self.daemon.blob_manager.blob_dir)) > 1)
        await self.daemon.jsonrpc_file_list()[0].fully_reflected.wait()
        await self.daemon.jsonrpc_file_delete(delete_from_download_dir=True, claim_name='foo')
        self.assertEqual(0, len(os.listdir(self.daemon.blob_manager.blob_dir)))
        # await self._restart_stream_manager()
        await self.daemon.runner.setup()
        site = aiohttp.web.TCPSite(self.daemon.runner, self.daemon.conf.api_host, self.daemon.conf.api_port)
        await site.start()
        self.assertListEqual(self.daemon.jsonrpc_file_list(), [])

    async def _test_range_requests(self):
        name = 'foo'
        url = f'http://{self.daemon.conf.api_host}:{self.daemon.conf.api_port}/get/{name}'

        async with aiohttp_request('get', url) as req:
            self.assertEqual(req.headers.get('Content-Type'), 'application/octet-stream')
            content_range = req.headers.get('Content-Range')
            content_length = int(req.headers.get('Content-Length'))
            streamed_bytes = await req.content.read()
        self.assertEqual(content_length, len(streamed_bytes))
        return streamed_bytes, content_range, content_length

    async def test_range_requests_2_byte(self):
        self.data = b'hi'
        await self._setup_stream(self.data)
        streamed, content_range, content_length = await self._test_range_requests()
        self.assertEqual(15, content_length)
        self.assertEqual(b'hi\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00', streamed)
        self.assertEqual('bytes 0-14/15', content_range)

    async def test_range_requests_15_byte(self):
        self.data = b'123456789abcdef'
        await self._setup_stream(self.data)
        streamed, content_range, content_length = await self._test_range_requests()
        self.assertEqual(15, content_length)
        self.assertEqual(15, len(streamed))
        self.assertEqual(self.data, streamed)
        self.assertEqual('bytes 0-14/15', content_range)

    async def test_range_requests_0_padded_bytes(self, size: int = (MAX_BLOB_SIZE - 1) * 4,
                                                 expected_range: str = 'bytes 0-8388603/8388604', padding=b''):
        self.data = get_random_bytes(size)
        await self._setup_stream(self.data)
        streamed, content_range, content_length = await self._test_range_requests()
        self.assertEqual(len(self.data + padding), content_length)
        self.assertEqual(streamed, self.data + padding)
        self.assertEqual(expected_range, content_range)

    async def test_range_requests_1_padded_bytes(self):
        await self.test_range_requests_0_padded_bytes(
            ((MAX_BLOB_SIZE - 1) * 4) - 1, padding=b'\x00'
        )

    async def test_range_requests_2_padded_bytes(self):
        await self.test_range_requests_0_padded_bytes(
            ((MAX_BLOB_SIZE - 1) * 4) - 2, padding=b'\x00' * 2
        )

    async def test_range_requests_14_padded_bytes(self):
        await self.test_range_requests_0_padded_bytes(
            ((MAX_BLOB_SIZE - 1) * 4) - 14, padding=b'\x00' * 14
        )

    async def test_range_requests_15_padded_bytes(self):
        await self.test_range_requests_0_padded_bytes(
            ((MAX_BLOB_SIZE - 1) * 4) - 15, padding=b'\x00' * 15
        )

    async def test_range_requests_last_block_of_last_blob_padding(self):
        self.data = get_random_bytes(((MAX_BLOB_SIZE - 1) * 4) - 16)
        await self._setup_stream(self.data)
        streamed, content_range, content_length = await self._test_range_requests()
        self.assertEqual(len(self.data), content_length)
        self.assertEqual(streamed, self.data)
        self.assertEqual('bytes 0-8388587/8388588', content_range)

    async def test_streaming_only_with_blobs(self):
        self.data = get_random_bytes((MAX_BLOB_SIZE - 1) * 4)
        await self._setup_stream(self.data)

        await self._test_range_requests()
        stream = self.daemon.jsonrpc_file_list()[0]
        self.assertTrue(os.path.isfile(self.daemon.blob_manager.get_blob(stream.sd_hash).file_path))
        self.assertIsNone(stream.download_directory)
        self.assertIsNone(stream.full_path)
        files_in_download_dir = list(os.scandir(os.path.dirname(self.daemon.conf.data_dir)))

        # test that repeated range requests do not create duplicate files
        for _ in range(3):
            await self._test_range_requests()
            stream = self.daemon.jsonrpc_file_list()[0]
            self.assertTrue(os.path.isfile(self.daemon.blob_manager.get_blob(stream.sd_hash).file_path))
            self.assertIsNone(stream.download_directory)
            self.assertIsNone(stream.full_path)
            current_files_in_download_dir = list(os.scandir(os.path.dirname(self.daemon.conf.data_dir)))
            self.assertEqual(
                len(files_in_download_dir), len(current_files_in_download_dir)
            )

        # test that a range request after restart does not create a duplicate file
        await self._restart_stream_manager()

        current_files_in_download_dir = list(os.scandir(os.path.dirname(self.daemon.conf.data_dir)))
        self.assertEqual(
            len(files_in_download_dir), len(current_files_in_download_dir)
        )
        stream = self.daemon.jsonrpc_file_list()[0]
        self.assertTrue(os.path.isfile(self.daemon.blob_manager.get_blob(stream.sd_hash).file_path))
        self.assertIsNone(stream.download_directory)
        self.assertIsNone(stream.full_path)

        await self._test_range_requests()
        stream = self.daemon.jsonrpc_file_list()[0]
        self.assertTrue(os.path.isfile(self.daemon.blob_manager.get_blob(stream.sd_hash).file_path))
        self.assertIsNone(stream.download_directory)
        self.assertIsNone(stream.full_path)
        current_files_in_download_dir = list(os.scandir(os.path.dirname(self.daemon.conf.data_dir)))
        self.assertEqual(
            len(files_in_download_dir), len(current_files_in_download_dir)
        )

    async def test_streaming_only_without_blobs(self):
        self.data = get_random_bytes((MAX_BLOB_SIZE - 1) * 4)
        await self._setup_stream(self.data, save_blobs=False)
        await self._test_range_requests()
        stream = self.daemon.jsonrpc_file_list()[0]
        self.assertIsNone(stream.download_directory)
        self.assertIsNone(stream.full_path)
        files_in_download_dir = list(os.scandir(os.path.dirname(self.daemon.conf.data_dir)))

        # test that repeated range requests do not create duplicate files
        for _ in range(3):
            await self._test_range_requests()
            stream = self.daemon.jsonrpc_file_list()[0]
            self.assertIsNone(stream.download_directory)
            self.assertIsNone(stream.full_path)
            current_files_in_download_dir = list(os.scandir(os.path.dirname(self.daemon.conf.data_dir)))
            self.assertEqual(
                len(files_in_download_dir), len(current_files_in_download_dir)
            )

        # test that a range request after restart does not create a duplicate file
        await self._restart_stream_manager()

        current_files_in_download_dir = list(os.scandir(os.path.dirname(self.daemon.conf.data_dir)))
        self.assertEqual(
            len(files_in_download_dir), len(current_files_in_download_dir)
        )
        stream = self.daemon.jsonrpc_file_list()[0]
        self.assertIsNone(stream.download_directory)
        self.assertIsNone(stream.full_path)

        await self._test_range_requests()
        stream = self.daemon.jsonrpc_file_list()[0]
        self.assertIsNone(stream.download_directory)
        self.assertIsNone(stream.full_path)
        current_files_in_download_dir = list(os.scandir(os.path.dirname(self.daemon.conf.data_dir)))
        self.assertEqual(
            len(files_in_download_dir), len(current_files_in_download_dir)
        )

    async def test_stream_and_save_with_blobs(self):
        self.data = get_random_bytes((MAX_BLOB_SIZE - 1) * 4)
        await self._setup_stream(self.data, streaming_only=False)

        await self._test_range_requests()
        streams = self.daemon.jsonrpc_file_list()
        self.assertEqual(1, len(streams))
        stream = streams[0]
        self.assertTrue(os.path.isfile(self.daemon.blob_manager.get_blob(stream.sd_hash).file_path))
        self.assertTrue(os.path.isdir(stream.download_directory))
        self.assertTrue(os.path.isfile(stream.full_path))
        full_path = stream.full_path
        files_in_download_dir = list(os.scandir(os.path.dirname(full_path)))

        for _ in range(3):
            await self._test_range_requests()
            streams = self.daemon.jsonrpc_file_list()
            self.assertEqual(1, len(streams))
            stream = streams[0]
            self.assertTrue(os.path.isfile(self.daemon.blob_manager.get_blob(stream.sd_hash).file_path))
            self.assertTrue(os.path.isdir(stream.download_directory))
            self.assertTrue(os.path.isfile(stream.full_path))
            current_files_in_download_dir = list(os.scandir(os.path.dirname(full_path)))
            self.assertEqual(
                len(files_in_download_dir), len(current_files_in_download_dir)
            )

        await self._restart_stream_manager()

        current_files_in_download_dir = list(os.scandir(os.path.dirname(full_path)))
        self.assertEqual(
            len(files_in_download_dir), len(current_files_in_download_dir)
        )
        streams = self.daemon.jsonrpc_file_list()
        self.assertEqual(1, len(streams))
        stream = streams[0]
        self.assertTrue(os.path.isfile(self.daemon.blob_manager.get_blob(stream.sd_hash).file_path))
        self.assertTrue(os.path.isdir(stream.download_directory))
        self.assertTrue(os.path.isfile(stream.full_path))

        await self._test_range_requests()
        streams = self.daemon.jsonrpc_file_list()
        self.assertEqual(1, len(streams))
        stream = streams[0]
        self.assertTrue(os.path.isfile(self.daemon.blob_manager.get_blob(stream.sd_hash).file_path))
        self.assertTrue(os.path.isdir(stream.download_directory))
        self.assertTrue(os.path.isfile(stream.full_path))
        current_files_in_download_dir = list(os.scandir(os.path.dirname(full_path)))
        self.assertEqual(
            len(files_in_download_dir), len(current_files_in_download_dir)
        )
        with open(stream.full_path, 'rb') as f:
            self.assertEqual(self.data, f.read())

    async def test_stream_and_save_without_blobs(self):
        self.data = get_random_bytes((MAX_BLOB_SIZE - 1) * 4)
        await self._setup_stream(self.data, streaming_only=False)
        self.daemon.conf.save_blobs = False

        await self._test_range_requests()
        stream = self.daemon.jsonrpc_file_list()[0]
        self.assertTrue(os.path.isdir(stream.download_directory))
        self.assertTrue(os.path.isfile(stream.full_path))
        full_path = stream.full_path
        files_in_download_dir = list(os.scandir(os.path.dirname(full_path)))

        for _ in range(3):
            await self._test_range_requests()
            stream = self.daemon.jsonrpc_file_list()[0]
            self.assertTrue(os.path.isdir(stream.download_directory))
            self.assertTrue(os.path.isfile(stream.full_path))
            current_files_in_download_dir = list(os.scandir(os.path.dirname(full_path)))
            self.assertEqual(
                len(files_in_download_dir), len(current_files_in_download_dir)
            )

        await self._restart_stream_manager()
        current_files_in_download_dir = list(os.scandir(os.path.dirname(full_path)))
        self.assertEqual(
            len(files_in_download_dir), len(current_files_in_download_dir)
        )
        streams = self.daemon.jsonrpc_file_list()
        self.assertEqual(1, len(streams))
        stream = streams[0]
        self.assertTrue(os.path.isdir(stream.download_directory))
        self.assertTrue(os.path.isfile(stream.full_path))

        await self._test_range_requests()
        streams = self.daemon.jsonrpc_file_list()
        self.assertEqual(1, len(streams))
        stream = streams[0]
        self.assertTrue(os.path.isdir(stream.download_directory))
        self.assertTrue(os.path.isfile(stream.full_path))
        current_files_in_download_dir = list(os.scandir(os.path.dirname(full_path)))
        self.assertEqual(
            len(files_in_download_dir), len(current_files_in_download_dir)
        )

        with open(stream.full_path, 'rb') as f:
            self.assertEqual(self.data, f.read())

    async def test_switch_save_blobs_while_running(self):
        await self.test_streaming_only_without_blobs()
        self.daemon.conf.save_blobs = True
        blobs_in_stream = self.daemon.jsonrpc_file_list()[0].blobs_in_stream
        sd_hash = self.daemon.jsonrpc_file_list()[0].sd_hash
        start_file_count = len(os.listdir(self.daemon.blob_manager.blob_dir))
        await self._test_range_requests()
        self.assertEqual(start_file_count + blobs_in_stream, len(os.listdir(self.daemon.blob_manager.blob_dir)))
        self.assertEqual(0, self.daemon.jsonrpc_file_list()[0].blobs_remaining)

        # switch back
        self.daemon.conf.save_blobs = False
        await self._test_range_requests()
        self.assertEqual(start_file_count + blobs_in_stream, len(os.listdir(self.daemon.blob_manager.blob_dir)))
        self.assertEqual(0, self.daemon.jsonrpc_file_list()[0].blobs_remaining)
        await self.daemon.jsonrpc_file_delete(delete_from_download_dir=True, sd_hash=sd_hash)
        self.assertEqual(start_file_count, len(os.listdir(self.daemon.blob_manager.blob_dir)))
        await self._test_range_requests()
        self.assertEqual(start_file_count, len(os.listdir(self.daemon.blob_manager.blob_dir)))
        self.assertEqual(blobs_in_stream, self.daemon.jsonrpc_file_list()[0].blobs_remaining)

    async def test_file_save_streaming_only_save_blobs(self):
        await self.test_streaming_only_with_blobs()
        stream = self.daemon.jsonrpc_file_list()[0]
        self.assertIsNone(stream.full_path)
        self.server.stop_server()
        await self.daemon.jsonrpc_file_save('test', self.daemon.conf.data_dir)
        stream = self.daemon.jsonrpc_file_list()[0]
        await stream.finished_writing.wait()
        with open(stream.full_path, 'rb') as f:
            self.assertEqual(self.data, f.read())

    async def test_file_save_streaming_only_dont_save_blobs(self):
        await self.test_streaming_only_without_blobs()
        stream = self.daemon.jsonrpc_file_list()[0]
        self.assertIsNone(stream.full_path)
        await self.daemon.jsonrpc_file_save('test', self.daemon.conf.data_dir)
        stream = self.daemon.jsonrpc_file_list()[0]
        await stream.finished_writing.wait()
        with open(stream.full_path, 'rb') as f:
            self.assertEqual(self.data, f.read())
