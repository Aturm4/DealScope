"""Offline public handshake values only, no network or account material."""

import asyncio
import importlib.util
import struct
import subprocess
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dealradar.handshake_safety import bounded_factorize, install_bounds, MAX_FRAME_BYTES, MAX_PLAIN_HANDSHAKE_BYTES
from dealradar.telegram_source import SourceFailure

HAVE_TELETHON = importlib.util.find_spec('telethon') is not None


@unittest.skipUnless(HAVE_TELETHON, 'Pinned optional transport not installed')
class HandshakeBoundsTests(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_and_invalid_pq_rejected_before_subprocess(self):
        with patch('dealradar.handshake_safety.subprocess.run') as child:
            for value in (2**127-1, -1, 0, 12, True):
                with self.assertRaises(SourceFailure):
                    bounded_factorize(value)
            child.assert_not_called()

    async def test_real_library_small_factorization_in_isolated_process(self):
        from dealradar.handshake_safety import _FACTOR_SCRIPT
        # Upstream randomized factorization sometimes returns (1, pq) for tiny
        # fixtures. Keep this success-path fixture deterministic in the child;
        # production randomness and fail-closed factor checks stay unchanged.
        seeded_script = 'import random\nrandom.seed(0)\n' + _FACTOR_SCRIPT
        with patch('dealradar.handshake_safety._FACTOR_SCRIPT', seeded_script):
            self.assertEqual(bounded_factorize(17 * 19), (17, 19))

    async def test_real_hard_prime_cannot_stall_the_parent_indefinitely(self):
        started = time.monotonic()
        with self.assertRaises(SourceFailure):
            bounded_factorize(2**61 - 1)
        self.assertLess(time.monotonic() - started, 8)

    async def test_factorization_timeout_is_an_enforced_child_deadline(self):
        with patch('dealradar.handshake_safety.subprocess.run', side_effect=subprocess.TimeoutExpired('TEST', 4)) as child:
            with self.assertRaises(SourceFailure):
                bounded_factorize(2**61 - 1)
        self.assertEqual(child.call_args.kwargs['timeout'], 4)
        self.assertIn('-I', child.call_args.args[0])
        self.assertTrue(child.call_args.kwargs['close_fds'])
        self.assertNotIn('API_HASH', child.call_args.kwargs['env'])

    async def test_bad_factorization_result_is_not_accepted(self):
        for output in ('[1,323]', '[17,17]', '[true,323]', 'INVALID'):
            with patch('dealradar.handshake_safety.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=output)):
                with self.assertRaises(SourceFailure):
                    bounded_factorize(17 * 19)

    async def test_large_frame_is_rejected_before_body_read(self):
        codec = install_bounds().packet_codec(None)
        calls = []
        class Reader:
            async def readexactly(self, size):
                calls.append(size)
                if len(calls) > 1:
                    raise AssertionError('Unbounded body must never be requested')
                return struct.pack('<ii', 2**31 - 1, 0)
        with self.assertRaises(SourceFailure):
            await codec.read_packet(Reader())
        self.assertEqual(calls, [8])

    async def test_normal_frame_retains_upstream_checksum_checks(self):
        codec = install_bounds().packet_codec(None)
        wire = codec.encode_packet(b'SYNTHETIC')
        stream = asyncio.StreamReader()
        stream.feed_data(wire)
        stream.feed_eof()
        self.assertEqual(await codec.read_packet(stream), b'SYNTHETIC')
        broken = wire[:-1] + bytes([wire[-1] ^ 1])
        stream = asyncio.StreamReader()
        stream.feed_data(broken)
        stream.feed_eof()
        from telethon.errors import InvalidChecksumError
        with self.assertRaises(InvalidChecksumError):
            await codec.read_packet(stream)

    async def test_pre_auth_gzip_rejected_before_native_decompression(self):
        from collections import defaultdict
        import logging
        from telethon.tl.core import GzipPacked
        from telethon.tl.functions import ReqPqMultiRequest
        from telethon.network.mtprotoplainsender import MTProtoPlainSender
        packed = bytes(GzipPacked(b'A' * 131072))
        body = struct.pack('<qqi', 0, 1, len(packed)) + packed
        codec = install_bounds().packet_codec(None)
        stream = asyncio.StreamReader()
        stream.feed_data(codec.encode_packet(body))
        stream.feed_eof()
        class Connection:
            async def send(self, request): pass
            async def recv(self): return await codec.read_packet(stream)
        sender = MTProtoPlainSender(Connection(), loggers=defaultdict(lambda: logging.getLogger('synthetic')))
        with patch('telethon.tl.core.gzippacked.gzip.decompress', side_effect=AssertionError('Must not decompress')) as inflate:
            with self.assertRaises(SourceFailure):
                await sender.send(ReqPqMultiRequest(0))
        inflate.assert_not_called()

    async def test_concrete_plain_handshake_replies_keep_native_parsing(self):
        from telethon import types
        from telethon.extensions import BinaryReader
        replies = (types.ResPQ(1, 2, b'\x01C', [0]),
                   types.ServerDHParamsOk(1, 2, b'SYNTHETIC'),
                   types.ServerDHParamsFail(1, 2, 0),
                   types.DhGenOk(1, 2, 0), types.DhGenRetry(1, 2, 0), types.DhGenFail(1, 2, 0))
        codec = install_bounds().packet_codec(None)
        for reply in replies:
            payload = bytes(reply)
            body = struct.pack('<qqi', 0, 1, len(payload)) + payload
            stream = asyncio.StreamReader()
            stream.feed_data(codec.encode_packet(body))
            stream.feed_eof()
            received = await codec.read_packet(stream)
            with BinaryReader(received[20:]) as reader:
                self.assertIsInstance(reader.tgread_object(), type(reply))

    async def test_oversized_plain_handshake_rejected_before_object_construction(self):
        from telethon import types
        codec = install_bounds().packet_codec(None)
        body = struct.pack('<qqiI', 0, 1, MAX_PLAIN_HANDSHAKE_BYTES, types.ResPQ.CONSTRUCTOR_ID)
        body += b'\0' * MAX_PLAIN_HANDSHAKE_BYTES
        stream = asyncio.StreamReader()
        stream.feed_data(codec.encode_packet(body))
        stream.feed_eof()
        with self.assertRaises(SourceFailure):
            await codec.read_packet(stream)

    async def test_short_or_unknown_plain_reply_rejected(self):
        codec = install_bounds().packet_codec(None)
        for body in (b'\0' * 8, struct.pack('<qqiI', 0, 1, 4, 0xDEADBEEF)):
            stream = asyncio.StreamReader()
            stream.feed_data(codec.encode_packet(body))
            stream.feed_eof()
            with self.assertRaises(SourceFailure):
                await codec.read_packet(stream)

    async def test_encrypted_frame_body_is_not_interpreted_as_plain_tl(self):
        codec = install_bounds().packet_codec(None)
        body = struct.pack('<qqiI', 1, 1, 4, 0x3072CFA1)
        stream = asyncio.StreamReader()
        stream.feed_data(codec.encode_packet(body))
        stream.feed_eof()
        self.assertEqual(await codec.read_packet(stream), body)


if __name__ == '__main__':
    unittest.main()
