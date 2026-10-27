"""Per-database OS lock shared by monitoring and destructive maintenance."""

from contextlib import contextmanager
import os
from pathlib import Path


class MonitorBusy(OSError):
    """A monitor or another maintenance operation still owns this database."""


@contextmanager
def monitor_lock(path):
    lock_path = Path(str(Path(path).resolve()) + '.monitor.lock')
    with lock_path.open('a+b') as stream:
        if stream.seek(0, 2) == 0:
            stream.write(b'\0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise MonitorBusy('Сначала останови мониторинг этой базы и дождись выхода. Повтори действие после остановки.') from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
