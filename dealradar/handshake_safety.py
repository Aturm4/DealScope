"""Availability bounds around pinned dependency code, without changing crypto.

Only public handshake pq enters the isolated child. No key/phone/code is passed.
"""

import json
import os
from pathlib import Path
import subprocess
import sys

MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_PLAIN_HANDSHAKE_BYTES = 64 * 1024
FACTOR_SECONDS = 4
_FACTOR_SCRIPT = """
import json, sys
from telethon.crypto.factorization import Factorization
print(json.dumps(Factorization.factorize(int(sys.argv[1]))))
"""


def bounded_factorize(pq):
    from .telegram_source import SourceFailure
    # MTProto normally uses a positive product of odd primes below2**63.
    # The pinned upstream parser already treats the byte string as signed.
    if type(pq) is not int or not 15 <= pq < 2**63 or pq % 2 == 0:
        raise SourceFailure('protocol_error')
    child_env = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR', 'TEMP', 'TMP') if key in os.environ}
    child_env['PATH'] = os.pathsep.join((sys.base_prefix, str(Path(sys.base_prefix) / 'DLLs'), str(Path(sys.executable).parent)))
    try:
        result = subprocess.run([sys.executable, '-I', '-B', '-c', _FACTOR_SCRIPT, str(pq)],
                                cwd=Path(__file__).resolve().parent.parent, env=child_env,
                                capture_output=True, text=True, encoding='ascii',
                                timeout=FACTOR_SECONDS, check=False, close_fds=True)
        if result.returncode != 0 or len(result.stdout) > 100:
            raise ValueError
        factors = json.loads(result.stdout)
        if (not isinstance(factors, list) or len(factors) != 2 or
                any(type(value) is not int for value in factors)):
            raise ValueError
        p, q = factors
        if not 2 < p < q < pq or p % 2 == 0 or q % 2 == 0 or p * q != pq:
            raise ValueError
        return p, q
    except (OSError, subprocess.TimeoutExpired, ValueError, UnicodeError):
        raise SourceFailure('protocol_error') from None


def install_bounds():
    from telethon.network import authenticator
    from telethon.network.connection.tcpfull import ConnectionTcpFull, FullPacketCodec
    from telethon import types
    from .telegram_source import SourceFailure

    # Plain key-exchange replies have closed, concrete schemas. Reject gzip,
    # containers and unrelated objects before native TL object construction.
    plain_replies = frozenset(reply.CONSTRUCTOR_ID for reply in (
        types.ResPQ, types.ServerDHParamsFail, types.ServerDHParamsOk,
        types.DhGenOk, types.DhGenRetry, types.DhGenFail,
    ))

    class Factorization:
        factorize = staticmethod(bounded_factorize)

    # Replace this module's dependency alias, not package files or crypto math.
    authenticator.Factorization = Factorization

    class BoundedCodec(FullPacketCodec):
        async def read_packet(self, reader):
            class LimitedReader:
                async def readexactly(self, size):
                    if type(size) is not int or not 0 <= size <= MAX_FRAME_BYTES:
                        raise SourceFailure('protocol_error')
                    return await reader.readexactly(size)
            body = await super().read_packet(LimitedReader())
            if body[:8] == b'\0' * 8:
                if (not 24 <= len(body) <= MAX_PLAIN_HANDSHAKE_BYTES or
                        int.from_bytes(body[20:24], 'little') not in plain_replies):
                    raise SourceFailure('protocol_error')
            return body

    class BoundedConnection(ConnectionTcpFull):
        packet_codec = BoundedCodec

    return BoundedConnection
