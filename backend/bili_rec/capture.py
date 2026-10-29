# -*- coding: utf-8 -*-
"""FLV 抓流：自己解析 tag 头，改写时间戳，按关键帧切分P。

为什么不用 ffmpeg 直接拉流：
  1. 本机命令行程序建不了 HTTPS（schannel 无凭据），ffmpeg 拉 https 直接失败；
  2. 直播流地址带 expires，会过期，ffmpeg 不会自己换地址；
  3. 自己读写才能精确统计字节/时长，并在断流处对齐关键帧。

每个分P文件都是完整 FLV（独立时间戳，从 0 开始）→ 之后用 ffmpeg -c copy 转 mp4 无损失。
"""
import os
import struct
import threading
import time

from . import httpc, live, platforms
from .util import ensure_dir, fmt_size

FLV_HEADER = b'FLV\x01\x05\x00\x00\x00\x09\x00\x00\x00\x00'
TAG_AUDIO, TAG_VIDEO, TAG_SCRIPT = 8, 9, 18
# 时间戳跳变阈值（毫秒）：直播正常帧间隔 <200ms，断流重连后绝对时间戳一次跳几十秒到几分钟。
# 超过这个值就判定为断流，把时间轴重锚到上一帧之后（见 FlvSink.write_tag）。
TS_JUMP_MS = 8000


class NotLive(RuntimeError):
    """接口说在播、取流时又报 live_status=0。这种重试也没用，直接收工。"""


class FlvSink:
    """往一个 FLV 文件里写 tag：新文件补 FLV 头，续写则接着原文件。"""

    def __init__(self, path, header=None, resume_ts=None):
        ensure_dir(os.path.dirname(path))
        self.path = path
        if resume_ts is None:
            self.f = open(path, 'wb')
            self.f.write(header or FLV_HEADER)
            self.base_ts = None       # 本文件首个 tag 的原始时间戳
            self.offset = 0
            self.last_ts = -1
        else:
            self.f = open(path, 'ab')
            self.base_ts = None
            self.offset = resume_ts + 1
            self.last_ts = resume_ts
        self.first_ts = 0 if resume_ts is None else 0
        # 断流重锚统计：录制结束时可以把「救回来的空档」写进日志
        self.jump_count = 0
        self.jump_saved_ms = 0

    def close(self):
        try:
            self.f.flush()
            os.fsync(self.f.fileno())
        except Exception:
            pass
        try:
            self.f.close()
        except Exception:
            pass

    @property
    def bytes(self):
        return self.f.tell()

    @property
    def duration_ms(self):
        return max(0, self.last_ts)

    def write_tag(self, tag_type, data_size, raw_ts, ext_ts, stream_id, payload, set_base=True):
        raw = (int(ext_ts) << 24) | int(raw_ts)
        ts = raw
        if self.base_ts is None and set_base and tag_type in (TAG_AUDIO, TAG_VIDEO):
            # 注意：直播流第一条往往是 onMetaData 脚本 tag（时间戳 0），
            # 真正的音视频 tag 时间戳是「开播至今」。基准必须取首条音视频 tag，
            # 否则整段视频会被画到几小时长。
            self.base_ts = ts
        if self.base_ts is None:
            ts = self.offset                     # 脚本 tag：直接落在段落起点之后
        else:
            ts = ts - self.base_ts + self.offset
            # 断流重连：B 站下发的绝对时间戳会突然往前跳一大截（实测 185s/333s/507s），
            # 直接平移会让 mp4 里出现「一帧持续好几分钟」的超长 duration，
            # B 站转码直接报「视频内部存在时间戳跳变」并失败（-16）。
            # 把跳变点重锚到上一帧之后，整条时间轴保持连续，空档自然消失。
            if self.last_ts >= 0 and ts - self.last_ts > TS_JUMP_MS:
                _jump = ts - self.last_ts
                self.base_ts = raw
                self.offset = self.last_ts + 1
                ts = self.offset
                self.jump_count = getattr(self, 'jump_count', 0) + 1
                self.jump_saved_ms = getattr(self, 'jump_saved_ms', 0) + _jump
        if ts < 0:
            ts = 0
        if ts <= self.last_ts:
            ts = self.last_ts + 1          # 保证单调，ffmpeg 才不会抱怨时间戳倒退
        self.last_ts = ts
        head = (struct.pack('>B', tag_type) + int(data_size).to_bytes(3, 'big') +
                (ts & 0xFFFFFF).to_bytes(3, 'big') +
                struct.pack('>B', (ts >> 24) & 0xFF) +
                int(stream_id).to_bytes(3, 'big'))
        self.f.write(head)
        self.f.write(payload)
        self.f.write(struct.pack('>I', 11 + data_size))


def _read_exact(stream, n):
    buf = b''
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return buf
        buf += chunk
    return buf


class Capture:
    """一次直播的抓流器（跨断流重连）。对外只读 snapshot()。"""

    def __init__(self, session, settings, log, stop_event=None, danmaku=None, platform='bili'):
        self.session = session
        self.settings = settings
        self.log = log
        self.stop_event = stop_event or threading.Event()
        self.danmaku = danmaku
        self.platform = platform or 'bili'
        self.room_id = session.room_id
        self.cookie = settings.get('_cookie') or ''
        # maxQn = 0 表示「自动取该主播当前最高画质」（注意 0 是合法值，不能写 or 10000）
        qn = settings.get('maxQn')
        self.qn = int(qn) if qn not in (None, '') else 0
        self.raw_dir = ensure_dir(os.path.join(session.dir, 'raw'))
        self.status = {
            'state': 'starting',
            'bytes': 0,
            'parts': 0,
            'curPart': 0,
            'curPartBytes': 0,
            'curPartMs': 0,
            'startedAt': time.time(),
            'lastDataAt': 0,
            'stream': '',
            'reconnects': 0,
            'error': '',
            'bitrateKbps': 0,
        }
        self._lock = threading.Lock()
        self._sink = None
        self._part_index = 0
        self._part_last_ts = -1
        self._rotate = False
        self._speed_t0 = time.time()
        self._speed_bytes = 0
        self._total_bytes = 0
        self._part_started = time.time()
        self._part_closed = False
        self._seq = {}          # 关键的编解码头：video AVC/HEVC sequence header + AAC header
        # 虎牙的 FLV 是分块响应（单连接只给 1~2MB），必须高频重连；
        # 但每个 wsSecret 链接有效期有好几分钟，缓存住避免每次重连都打一次取流接口。
        self._stream_cache = None
        self._stream_cache_at = 0.0
        self._raw_last = {}     # 虎牙去重用：每个轨最近写过的绝对时间戳（ms）

    # ------------------------------------------------------------ 取流缓存

    # 缓存多久：B站 playurl 的地址分钟级就可能失效，不缓存；虎牙的 wsSecret 几十分钟有效
    # 虎牙的 FLV 每次连接只回 1~2MB 的快照，必须重新取流（新 wsSecret）才能拿到更新的位置，
    # 所以这里不缓存；B站 playurl 地址本身就是长连接，也不需要缓存。
    _CACHE_TTL = {}

    def _cached_stream(self):
        ttl = self._CACHE_TTL.get(self.platform, 0)
        if not ttl or not self._stream_cache:
            return None
        if time.time() - self._stream_cache_at > ttl:
            return None
        return self._stream_cache

    def _cache_stream(self, best):
        if self.platform in self._CACHE_TTL:
            self._stream_cache = best
            self._stream_cache_at = time.time()

    # ------------------------------------------------------------ 对外

    def snapshot(self):
        s = dict(self.status)
        s['parts'] = self._part_index
        s['bytes'] = self._total_bytes
        if self._sink is not None:
            s['curPartBytes'] = self._sink.bytes
            s['curPartMs'] = self._elapsed_ms()
        return s

    @property
    def part_seconds(self):
        """分P 时长每次实时读设置：面板里改完，当前这场也立刻按新值切。"""
        try:
            return max(60, int(self.settings.get('partSeconds') or 7200))
        except (TypeError, ValueError):
            return 7200

    def _elapsed_ms(self):
        """分P 时长用墙钟算：直播流的时间戳是「开播至今」，不能当录制时长。"""
        if not self._part_started:
            return 0
        return int(max(0.0, time.time() - self._part_started) * 1000)

    def request_stop(self):
        self.stop_event.set()

    @property
    def stopping(self):
        return self.stop_event.is_set()

    # ------------------------------------------------------------ 主循环

    def run(self):
        backoff = 2.0
        fails = 0
        if self.danmaku:
            self.danmaku.start()
        try:
            while not self.stopping:
                try:
                    self._capture_once()
                except NotLive as e:
                    self.log('取流说没在播（%s），结束本次录制', e)
                    self.status['state'] = 'done'
                    break
                except Exception as e:
                    fails += 1
                    msg = '%s: %s' % (type(e).__name__, e)
                    self.log('抓流异常（连续第 %d 次）：%s', fails, msg)
                    self.log.exc('抓流异常详情')
                    self.status['state'] = 'reconnecting'
                    self.status['error'] = msg
                    self.status['reconnects'] = int(self.status.get('reconnects') or 0) + 1
                else:
                    fails = 0
                    backoff = 0.15 if self.platform == platforms.HUYA else 2.0
                finally:
                    self._close_part_handle()

                if self.stopping:
                    break
                if fails >= 8:
                    self.log('连续失败 %d 次，结束抓流', fails)
                    self.status['state'] = 'error'
                    break
                wait = min(20.0, backoff)
                slept = 0.0
                while slept < wait and not self.stopping:
                    step = min(0.5, wait - slept)
                    time.sleep(step)
                    slept += step
                backoff *= 1.6
        finally:
            self._close_part(closed=True)
            if self.danmaku:
                self.danmaku.stop()
            if self.status['state'] != 'error':
                self.status['state'] = 'done'
            self.status['endedAt'] = time.time()
        return self.snapshot()

    # ------------------------------------------------------------ 单次连接

    def _capture_once(self):
        prefer_codec = self.settings.get('preferCodec') or 'avc'
        best = self._cached_stream()
        try:
            if best is None:
                best = platforms.stream(self.platform, self.room_id, qn=self.qn,
                                        codec=prefer_codec, cookie=self.cookie)
                self._cache_stream(best)
        except live.LiveError as e:
            if 'live_status=0' in str(e):
                raise NotLive(str(e))
            raise
        if self.qn <= 0 and best.get('qn'):
            self.log('自动画质：qn=%s（%s %s）', best['qn'], best['codec'], best.get('qnDesc') or '')
            self.session.update({'maxQn': best['qn'], 'vcodec': best['codec']})
        self.status['stream'] = '%s/%s/%s qn=%s' % (
            best['protocol'], best['format'], best['codec'], best['qn'])
        self.status['state'] = 'recording'
        self.status['error'] = ''
        self.log('取流：%s %s（qn=%s %s）', best['protocol'], best['format'],
                 best['qn'], best.get('qnDesc') or '')
        if best['format'] != 'flv':
            raise RuntimeError('当前只有 %s 流（本机命令行拉不了 https），暂不支持' % best['format'])

        try:
            stream = httpc.Stream(best['url'],
                                  headers=best.get('headers') or {'Referer': 'https://live.bilibili.com/'},
                                  cookie=self.cookie, timeout=25)
        except Exception:
            # 链接过期/被 CDN 拒了：丢掉缓存，下一轮重新取流
            self._stream_cache = None
            self._stream_cache_at = 0.0
            raise
        try:
            header = _read_exact(stream, 13)
            if not header or header[:3] != b'FLV':
                raise RuntimeError('不是 FLV 流（前 13 字节 %r）' % header[:13])

            # 同一次直播里重连：接着当前分P文件续写；分P 已收尾则另起一段
            if self._sink is None:
                if self._part_index == 0:
                    self._open_part(header, resume=False)
                else:
                    self._open_part(None, resume=not self._part_closed)
            first = True
            while not self.stopping:
                head = _read_exact(stream, 11)
                if len(head) < 11:
                    self.log('流结束（连接被关闭），准备重连')
                    return
                tag_type = head[0]
                data_size = int.from_bytes(head[1:4], 'big')
                raw_ts = int.from_bytes(head[4:7], 'big')
                ext_ts = head[7]
                stream_id = int.from_bytes(head[8:11], 'big')
                payload = _read_exact(stream, data_size)
                if len(payload) < data_size:
                    self.log('流中断（payload 不完整），准备重连')
                    return
                if len(_read_exact(stream, 4)) < 4:
                    self.log('流中断（缺 PreviousTagSize），准备重连')
                    return

                if first:
                    self.log('开始写入分P %d', self._part_index)
                    first = False

                # 记住编解码头：切新段时要补在最前面，否则 ffmpeg 拿不到 SPS/PPS
                if tag_type == TAG_VIDEO and len(payload) >= 2 and payload[1] == 0:
                    self._seq['video'] = payload
                elif tag_type == TAG_AUDIO and len(payload) >= 2 and payload[0] >> 4 == 10 and payload[1] == 0:
                    self._seq['audio'] = payload

                # 该切段了：等到下一个关键帧再切，保证每段都从关键帧开始
                if self._rotate and tag_type == TAG_VIDEO and payload and (payload[0] >> 4) == 1:
                    self._rotate = False
                    self._close_part(closed=True)
                    self._open_part(None, resume=False)

                if self.platform == platforms.HUYA and tag_type in (8, 9):
                    # 虎牙每次重连给的是「当前 GOP 起点的快照」，边缘节点还会重放同一段。
                    # 用绝对时间戳做闸门：不比上一段尾帧新的 tag 一律丢弃，否则画面会重复卡顿。
                    raw_ms = (int(ext_ts) << 24) | int(raw_ts)
                    prev = self._raw_last.get(tag_type)
                    if prev is not None and raw_ms <= prev:
                        continue
                    self._raw_last[tag_type] = raw_ms
                self._sink.write_tag(tag_type, data_size, raw_ts, ext_ts, stream_id, payload)
                self._total_bytes += 11 + data_size + 4
                self._part_last_ts = self._sink.last_ts
                self.status['lastDataAt'] = time.time()
                if self._elapsed_ms() >= self.part_seconds * 1000:
                    self._rotate = True
                self._speed_tick()
        finally:
            stream.close()

    def _speed_tick(self):
        now = time.time()
        dt = now - self._speed_t0
        if dt >= 3.0:
            db = self._total_bytes - self._speed_bytes
            self.status['bitrateKbps'] = int(db * 8 / 1024 / dt) if dt else 0
            cur = self._sink.bytes if self._sink else 0
            ms = self._elapsed_ms()
            self.status['curPartBytes'] = cur
            self.status['curPartMs'] = ms
            self.session.update_part(self._part_index, {'bytes': cur, 'durationMs': ms})
            self._speed_t0 = now
            self._speed_bytes = self._total_bytes

    # ------------------------------------------------------------ 分P 生命周期

    def _open_part(self, header, resume=False):
        if not resume:
            self._part_index += 1
            self._part_last_ts = -1
        self._part_closed = False
        path = os.path.join(self.raw_dir, 'part-%03d.flv' % self._part_index)
        resume_ts = self._part_last_ts if (resume and self._part_last_ts >= 0) else None
        self._sink = FlvSink(path, header=header, resume_ts=resume_ts)
        if not resume:
            self._part_started = time.time()
            # 新分P：先把编解码头补上（part2 起没有 stream 开头的 sequence header）
            for kind in ('video', 'audio'):
                blob = self._seq.get(kind)
                if blob:
                    self._sink.write_tag(
                        TAG_VIDEO if kind == 'video' else TAG_AUDIO,
                        len(blob), 0, 0, 0, blob, set_base=False)
        self._speed_t0 = time.time()
        self._speed_bytes = self._total_bytes
        self.status['state'] = 'recording'
        self.status['curPart'] = self._part_index
        self.status['parts'] = self._part_index
        self.session.add_part({
            'index': self._part_index,
            'file': path,
            'bytes': 0,
            'durationMs': 0,
            'status': 'recording',
            'startedAt': time.time(),
        })
        if resume_ts is not None:
            self.log('分P %d 续写（上一段已有 %.1f 秒）', self._part_index, resume_ts / 1000.0)
        else:
            self.log('新分P %d 开始', self._part_index)

    def _close_part_handle(self):
        """连接结束：只关文件句柄，分P 仍算「录制中」（等重连续写或最终收尾）。"""
        sink, self._sink = self._sink, None
        if sink is not None:
            nbytes = sink.bytes          # 必须先取字节数：close() 之后 tell() 会抛
            elapsed = self._elapsed_ms()
            self._part_last_ts = sink.last_ts
            sink.close()
            self.session.update_part(self._part_index, {
                'bytes': nbytes, 'durationMs': elapsed})

    def _close_part(self, closed=False):
        self._close_part_handle()
        if self._part_index <= 0:
            return
        if closed:
            self._part_closed = True
        self.session.update_part(self._part_index, {
            'status': 'closed' if closed else 'recording',
            'endedAt': time.time(),
        })
        if closed:
            self.log('分P %d 收尾：%.1f 秒', self._part_index, self._elapsed_ms() / 1000.0)
