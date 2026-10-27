# -*- coding: utf-8 -*-
"""直播弹幕录制（标准库手写 WebSocket）。

B 站弹幕协议：16 字节包头 + body，protover=2 时 body 是 zlib 压缩的一串子包。
这里只要「能收、能落盘、能导出 xml/ass」，所以不引第三方库。
"""
import base64
import json
import os
import random
import socket
import ssl
import struct
import threading
import time
import zlib

from . import live
from .httpc import cookie_field
from .util import ensure_dir

OP_HEARTBEAT = 2
OP_HEARTBEAT_REPLY = 3
OP_MESSAGE = 5
OP_AUTH = 7
OP_AUTH_REPLY = 8

HEADER = struct.Struct('>IHHII')  # 总长/头长/协议版本/操作码/序号


def _pack(op, body=b'', protover=1):
    return HEADER.pack(16 + len(body), 16, protover, op, 1) + body


class WsClient:
    """最小可用 WebSocket 客户端（只做客户端→服务器的 masked 帧）。"""

    def __init__(self, host, port, path='/sub', cookie='', origin='https://live.bilibili.com',
                 timeout=8.0):
        self.host, self.port, self.path = host, port, path
        self.cookie = cookie
        self.origin = origin
        self.timeout = timeout
        self.sock = None
        self._buf = b''
        self._lock = threading.Lock()

    def connect(self):
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        ctx = ssl.create_default_context()
        self.sock = ctx.wrap_socket(raw, server_hostname=self.host)
        self.sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            'GET %s HTTP/1.1' % self.path,
            'Host: %s:%d' % (self.host, self.port),
            'Upgrade: websocket',
            'Connection: Upgrade',
            'Sec-WebSocket-Key: %s' % key,
            'Sec-WebSocket-Version: 13',
            'Origin: %s' % self.origin,
            'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/139.0.0.0 Safari/537.36',
        ]
        if self.cookie:
            lines.append('Cookie: %s' % self.cookie)
        self.sock.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode())
        head = b''
        while b'\r\n\r\n' not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError('WebSocket 握手被关闭')
            head += chunk
        first = head.split(b'\r\n', 1)[0].decode('latin-1')
        if ' 101 ' not in first + ' ':
            raise RuntimeError('WebSocket 握手失败：%s' % first)
        self._buf = head.split(b'\r\n\r\n', 1)[1]
        return self

    def send(self, payload):
        data = bytes(payload)
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            head = struct.pack('>BB', 0x82, 0x80 | n)
        elif n < 65536:
            head = struct.pack('>BBH', 0x82, 0x80 | 126, n)
        else:
            head = struct.pack('>BBQ', 0x82, 0x80 | 127, n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        with self._lock:
            self.sock.sendall(head + mask + masked)

    def _fill(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError('WebSocket 连接关闭')
            self._buf += chunk

    def recv(self):
        """返回一条文本/二进制消息（已处理分片）。"""
        payload = b''
        while True:
            self._fill(2)
            b0, b1 = self._buf[0], self._buf[1]
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            length = b1 & 0x7F
            off = 2
            if length == 126:
                self._fill(4)
                length = struct.unpack('>H', self._buf[2:4])[0]
                off = 4
            elif length == 127:
                self._fill(10)
                length = struct.unpack('>Q', self._buf[2:10])[0]
                off = 10
            mask = b''
            if masked:
                self._fill(off + 4)
                mask = self._buf[off:off + 4]
                off += 4
            self._fill(off + length)
            data = self._buf[off:off + length]
            self._buf = self._buf[off + length:]
            if masked and mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x8:
                raise ConnectionError('服务器关闭 WebSocket')
            if opcode == 0x9:
                with self._lock:
                    self.sock.sendall(struct.pack('>BB', 0x8A, 0x80) + os.urandom(4))
                continue
            payload += data
            if fin:
                return payload

    def close(self):
        try:
            self.sock and self.sock.close()
        except Exception:
            pass


# 统计：protover=3（brotli 压缩）的包收到了多少个但解不开。
# 本机 Python 没有 brotli 模块时会走这条路，表现就是「连上了但一条弹幕都没有」。
PV3_SEEN = [0]

try:                                    # 装了 brotli 就顺带支持 protover 3
    import brotli as _brotli
except Exception:
    _brotli = None


def _parse_packets(raw):
    out = []
    i = 0
    while i + 16 <= len(raw):
        total, head_len, protover, op, _seq = HEADER.unpack_from(raw, i)
        if total < head_len or i + total > len(raw):
            break
        body = raw[i + head_len:i + total]
        i += total
        if op == OP_MESSAGE and protover in (2, 3):
            try:
                if protover == 2:
                    body = zlib.decompress(body)
                elif _brotli is not None:
                    body = _brotli.decompress(body)
                else:
                    PV3_SEEN[0] += 1
                    continue
            except Exception:
                if protover == 3:
                    PV3_SEEN[0] += 1
                continue
            out.extend(_parse_packets(body))
        else:
            out.append((op, protover, body))
    return out


class DanmakuRecorder:
    """一个直播会话的弹幕记录器，写 danmaku.jsonl（每行一条）。"""

    def __init__(self, session, room_id, cookie, log, t0=None):
        self.session = session
        self.room_id = int(room_id)
        self.cookie = cookie or ''
        self.log = log
        self.t0 = t0 or time.time()
        # 关键：鉴权包里必须带真实 uid。uid=0（游客）会被服务器立刻断开。
        self.uid = int(cookie_field(self.cookie, 'DedeUserID') or 0)
        self.path = os.path.join(session.dir, 'danmaku.jsonl')
        ensure_dir(session.dir)
        self._stop = threading.Event()
        self._thread = None
        self._f = None
        self.count = 0
        self.state = 'idle'

    # ------------------------------------------------------------ 控制

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._f = open(self.path, 'a', encoding='utf-8')
        self._thread = threading.Thread(target=self._run, name='danmaku-%s' % self.room_id,
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=8)
        try:
            self._f and self._f.close()
        except Exception:
            pass
        self._f = None
        self.state = 'stopped'

    def snapshot(self):
        return {'count': self.count, 'state': self.state}

    # ------------------------------------------------------------ 主循环

    def _run(self):
        backoff = 3.0
        while not self._stop.is_set():
            try:
                self._session_loop()
                backoff = 3.0
            except Exception as e:
                self.state = 'reconnecting'
                self.log('弹幕连接断开：%s: %s', type(e).__name__, e)
            if self._stop.is_set():
                break
            slept = 0.0
            while slept < min(30.0, backoff) and not self._stop.is_set():
                time.sleep(0.5)
                slept += 0.5
            backoff *= 1.5
        self.state = 'stopped'

    def _session_loop(self):
        info = live.danmu_info(self.room_id, cookie=self.cookie)
        hosts = info.get('hosts') or []
        if not hosts:
            raise RuntimeError('没有可用弹幕服务器')
        random.shuffle(hosts)
        last_err = None
        for h in hosts:
            try:
                client = WsClient(h['host'], h['wssPort'], cookie=self.cookie).connect()
            except Exception as e:
                last_err = e
                continue
            try:
                token = info.get('token') or ''
                auth = {'uid': self.uid, 'roomid': self.room_id, 'protover': 2,
                        'platform': 'web', 'type': 2, 'key': token}
                client.send(_pack(OP_AUTH, json.dumps(auth).encode('utf-8')))
                self.state = 'connected'
                self.log('弹幕已连接 %s', h['host'])
                pv3_seen = PV3_SEEN[0]
                next_hb = time.time() + 25
                while not self._stop.is_set():
                    if time.time() >= next_hb:
                        client.send(_pack(OP_HEARTBEAT))
                        next_hb = time.time() + 25
                        if PV3_SEEN[0] > pv3_seen:
                            pv3_seen = PV3_SEEN[0]
                            self.log('⚠ 收到 %d 个 protover=3（brotli）弹幕包但解不开：%s',
                                     pv3_seen,
                                     '已用 brotli 解压' if _brotli is not None
                                     else '本机 Python 没有 brotli 模块，这个房间的弹幕会丢')
                    try:
                        msg = client.recv()
                    except socket.timeout:
                        continue
                    for op, _pv, body in _parse_packets(msg):
                        if op == OP_HEARTBEAT_REPLY:
                            continue
                        if op in (OP_AUTH_REPLY, OP_MESSAGE):
                            self._handle_body(body)
                return
            finally:
                client.close()
        if last_err:
            raise last_err

    def _handle_body(self, body):
        text = body.decode('utf-8', 'replace').strip()
        if not text.startswith('{'):
            return
        try:
            cmd = json.loads(text)
        except Exception:
            # 一个包体可能是多个 JSON 拼在一起（很少见）
            return
        self._on_cmd(cmd)

    def _on_cmd(self, cmd):
        name = cmd.get('cmd') or ''
        if name.startswith('DANMU_MSG'):
            self._on_danmu(cmd)
        elif name in ('SEND_GIFT', 'SUPER_CHAT_MESSAGE', 'GUARD_BUY'):
            self._on_gift(name, cmd.get('data') or {})

    def _write(self, obj):
        if not self._f:
            return
        try:
            self._f.write(json.dumps(obj, ensure_ascii=False) + '\n')
            self.count += 1
            self._f.flush()                 # 边录边落盘：进程被杀也不丢
            if self.count % 10 == 0:
                self.session.update({'danmaku': self.count})
        except Exception:
            pass

    def _on_danmu(self, cmd):
        info = cmd.get('info') or []
        if len(info) < 3:
            return
        try:
            meta = info[0]
            user = info[2]
            ts = float(meta[4])
            if ts > 1e11:            # 有的包给的是毫秒
                ts /= 1000.0
            self._write({
                't': round(max(0.0, ts - self.t0), 3),
                'ts': ts,
                'mode': int(meta[1]),
                'size': int(meta[2]),
                'color': int(meta[3]),
                'uid': user[0],
                'uname': user[1],
                'text': info[1],
            })
        except Exception:
            return

    def _on_gift(self, name, data):
        try:
            self._write({
                'kind': 'gift',
                'cmd': name,
                't': round(time.time() - self.t0, 3),
                'uname': data.get('uname') or '',
                'gift': (data.get('giftName') or data.get('gift_name') or ''),
                'num': data.get('num') or 1,
                'price': data.get('price') or 0,
                'message': data.get('message') or '',
            })
        except Exception:
            return


# ---------------------------------------------------------------- 导出

def read_jsonl(path):
    out = []
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return out


def _xml_escape(text):
    return (str(text).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


def export_xml(jsonl_path, xml_path):
    rows = [r for r in read_jsonl(jsonl_path) if 'text' in r]
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<i>', '<chatserver>live.bilibili.com</chatserver>']
    for r in rows:
        p = '%.3f,%d,%d,%d,%d,0,%s,%d' % (
            max(0.0, float(r.get('t') or 0)), int(r.get('mode') or 1),
            int(r.get('size') or 25), int(r.get('color') or 16777215),
            int(r.get('ts') or 0), r.get('uid') or 0, int(abs(hash(str(r.get('uid')))) % 10 ** 8))
        lines.append('<d p="%s">%s</d>' % (p, _xml_escape(r.get('text') or '')))
    lines.append('</i>')
    with open(xml_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    return len(rows)


ASS_HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Danmaku,Microsoft YaHei UI,{fs},&H{col}&,&H{col}&,&H66000000,&H00000000,0,0,0,0,100,100,0,0,1,1.2,0.6,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ass_time(sec):
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return '%d:%02d:%05.2f' % (h, m, s)


def _bgr(color):
    color = int(color or 0xFFFFFF)
    b = (color >> 16) & 0xFF
    g = (color >> 8) & 0xFF
    r = color & 0xFF
    return '%02X%02X%02X' % (b, g, r)


def export_ass(jsonl_path, ass_path, width=1920, height=1080, duration=4.0):
    """导出滚动弹幕 ASS。顶部/底部弹幕各自静态停留 4 秒。"""
    rows = [r for r in read_jsonl(jsonl_path) if 'text' in r]
    fs = max(20, int(height * 0.038))
    body = [ASS_HEAD.format(w=width, h=height, fs=fs, col=_bgr(0xFFFFFF))]
    for r in rows:
        text = (r.get('text') or '').replace('\n', ' ').replace('{', '(').replace('}', ')')
        if not text:
            continue
        t = max(0.0, float(r.get('t') or 0))
        mode = int(r.get('mode') or 1)
        color = _bgr(r.get('color') or 0xFFFFFF)
        start = _ass_time(t)
        tag = '&H%s&' % color
        if mode in (4, 5):
            align = 2 if mode == 4 else 8
            end = _ass_time(t + duration)
            body.append('Dialogue: 0,%s,%s,Danmaku,,0,0,0,,{\\an%d\\c%s}%s'
                        % (start, end, align, tag, text))
        else:
            end = _ass_time(t + duration + len(text) * 0.03)
            body.append('Dialogue: 0,%s,%s,Danmaku,,0,0,0,,{\\move(%d,0,%d,0)\\c%s}%s'
                        % (start, end, width, -width, tag, text))
    with open(ass_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(body))
    return len(rows)
