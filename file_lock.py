"""An interprocess exclusive lock that is released if the owner exits."""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import time

if os.name == 'nt':
    import msvcrt
else:
    import fcntl


@contextmanager
def file_lock(path, timeout=None):
    """Wait for the lock; timeout=0 raises BlockingIOError and a timed wait raises TimeoutError."""
    if timeout is not None and timeout < 0:
        raise ValueError('锁的等待时间不能为负数')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        fd = handle.fileno()
        if os.name != 'nt' and timeout is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            deadline = None if timeout is None else time.monotonic() + timeout
            # Windows byte-range locks can extend past EOF, including on a new empty file.
            handle.seek(0)
            while True:
                try:
                    if os.name == 'nt':
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if timeout == 0:
                        raise BlockingIOError(errno.EAGAIN, '锁正在使用', str(path)) from error
                    delay = .05 if deadline is None else min(.05, deadline - time.monotonic())
                    if delay <= 0:
                        raise TimeoutError(f'等待锁超时：{path}') from error
                    time.sleep(delay)
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
