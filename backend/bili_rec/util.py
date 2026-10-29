# -*- coding: utf-8 -*-
"""通用工具：路径、JSON 落盘、日志、时间格式化。"""
import json
import os
import signal
import tempfile
import time
import traceback


def now():
    return time.time()


def iso(ts=None):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts if ts else time.time()))


def compact(ts=None):
    return time.strftime('%Y%m%d_%H%M%S', time.localtime(ts if ts else time.time()))


def fmt_size(n):
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return ('%.1f%s' % (n, unit)) if unit != 'B' else ('%d%s' % (n, unit))
        n /= 1024.0
    return '%.1fTB' % n


def fmt_dur(sec):
    sec = int(max(0, sec or 0))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return '%d:%02d:%02d' % (h, m, s)
    return '%d:%02d' % (m, s)


def ensure_dir(path):
    if path:
        os.makedirs(path, exist_ok=True)
    return path


def read_json(path, default=None):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, value, backup=True):
    """原子写：先写临时文件再替换，避免读到半个文件。

    backup=True 时先把旧文件留一份 .bak —— 主播列表这类「一次写坏就再也找不回来」
    的小文件，靠它兜底（曾经因为一次写空把 4 个主播全丢了）。
    """
    d = os.path.dirname(os.path.abspath(path))
    if d:
        ensure_dir(d)
    if backup and os.path.exists(path):
        try:
            import shutil
            shutil.copy2(path, path + '.bak')
        except Exception:
            pass
    fd, tmp = tempfile.mkstemp(dir=d or '.', prefix='.tmp-', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def tail(path, lines=60, max_bytes=256000):
    """读日志尾部（按行）。"""
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            raw = f.read()
        text = raw.decode('utf-8', 'replace')
        arr = [l for l in text.replace('\r\n', '\n').split('\n')]
        return '\n'.join(arr[-lines:])
    except Exception:
        return ''


class Log:
    """往文件追加日志；控制台也打一份（守护进程里 stdout 被重定向到日志文件）。"""

    def __init__(self, path=None, echo=True):
        self.path = path
        self.echo = echo
        self._lock_needed = False

    def __call__(self, msg, *args):
        try:
            text = msg % args if args else msg
        except Exception:
            text = str(msg)
        line = '[%s] %s' % (iso(), text)
        if self.echo:
            try:
                print(line, flush=True)
            except Exception:
                pass
        if self.path:
            try:
                ensure_dir(os.path.dirname(self.path))
                with open(self.path, 'a', encoding='utf-8') as f:
                    f.write(line + '\n')
            except Exception:
                pass
        return line

    def exc(self, prefix='异常'):
        self('%s:\n%s', prefix, traceback.format_exc())


def pid_alive(pid):
    """进程是否存活。

    Windows 上 **不能** 用 os.kill(pid, 0)：CPython 在 Windows 下除
    CTRL_C/CTRL_BREAK 以外的信号都走 TerminateProcess，sig=0 也会被当成
    「结束进程」——查询即杀掉。所以这里走 OpenProcess + GetExitCodeProcess。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_pid(pid):
    """结束进程（Windows 用 taskkill /T 连子进程一起）。"""
    import subprocess
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                           capture_output=True, timeout=20)
        else:
            os.kill(pid, signal.SIGTERM)
        return True
    except Exception:
        return False
