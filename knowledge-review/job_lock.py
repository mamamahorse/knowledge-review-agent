"""Nonblocking per-job OS lock; does not open a business database."""
import os
from pathlib import Path
from contextlib import contextmanager


class JobBusy(ValueError):
    pass


@contextmanager
def job_lock(database, identity):
    if len(identity) != 32 or any(c not in '0123456789abcdef' for c in identity):
        raise KeyError("任务记录不存在。")
    path = Path(database).with_name(f"{Path(database).stem}-job-{identity}.lock")
    with path.open('a+b') as file:
        if file.tell() == 0:
            file.write(b'0')
            file.flush()
        file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise JobBusy("这项任务正在处理，请等待完成。") from exc
        try:
            yield
        finally:
            file.seek(0)
            if os.name == 'nt':
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file, fcntl.LOCK_UN)
