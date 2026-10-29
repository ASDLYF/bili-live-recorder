# -*- coding: utf-8 -*-
"""状态存储：settings / ups / sessions / queue。

目录约定（workRoot 由插件配置决定，默认与后端同级的 _bili_live_work）：

  workRoot/
    settings.json              全局设置
    ups.json                   监控的 UP 主
    daemon.json                守护进程心跳
    queue.json                 待发布队列
    uploads/<id>.json          每条投稿任务的上传进度
    sessions/<sessionId>/
        meta.json              录制元信息 + 分P 列表
        raw/part-001.flv       原始 FLV（转 mp4 后可删）
        part-001.mp4           投稿用成品
        cover.jpg              自动封面
        danmaku.jsonl          弹幕原始记录
        danmaku.xml / danmaku.ass
        run.log
"""
import os
import threading
import time

from .util import ensure_dir, now, read_json, write_json

DEFAULT_SETTINGS = {
    # 监控
    'pollSeconds': 30,             # 开播轮询间隔
    'maxQn': 10000,                # 取流清晰度上限（10000=原画，20000=4K）
    'preferCodec': 'avc',          # avc=兼容性最好（推荐）/ hevc=仅在需要 4K 时用
    'partSeconds': 7200,           # 每 2 小时一个分P
    # 录制后处理
    'recordRoot': '',              # 录制目录；留空 = workRoot/sessions
    'danmaku': True,               # 录弹幕
    'makeCover': True,             # 抽帧做封面
    'keepRawFlv': False,           # 转 mp4 后是否保留 FLV
    'minFreeGb': 8,                # 磁盘剩余低于此值停止录制
    'keepAwake': True,             # 录制期间用 caffeinate 阻止 macOS 休眠
    # 发布默认值（参考 B 站投稿选项）
    'titleTemplate': '【录播】{date} {uname} {title}',
    'descTemplate': ('{uname} 的直播录播（{date}）。\n'
                     '直播间：{roomUrl}\n'
                     '本录播由 DSH「直播小助手」自动录制。'),
    'tid': 21,                     # 分区：21=日常
    'tidName': '日常',
    'tags': ['录播', '{uname}'],
    'copyright': 1,                # 1 原创 / 2 转载
    'source': '',
    'visibility': 'private',       # private=仅自己可见 / public=公开 / scheduled=定时
    'dynamic': '',                 # 发布同时带一条动态，空=不带动
    'noReprint': 1,                # 禁止转载
    'openElec': 1,                 # 允许充电
    'closeDanmaku': 0,             # 关闭弹幕
    'closeReply': 0,               # 关闭评论
    'dtime': 0,                    # 定时发布时间戳（visibility=scheduled 时）
    'autoPublish': False,          # True = 录完直接投（默认走人工确认）
    'deleteLocalAfterPublish': True,
    'deleteSessionAfterPublish': True,   # 连会话目录（封面/弹幕/meta）一起删
    'deleteWaitReview': True,            # 等审核通过再删；被驳回就不删
    'reviewWaitSeconds': 900,            # 等审核最多多久（超时先不删）
    'publishConcurrency': 1,
    # 每个 UP 主的目标 B 站合集：{"<mid>": {"id": 9135411, "title": "路演"}}
    'seasonMap': {},
}


def settings_path(work_root):
    return os.path.join(work_root, 'settings.json')


def load_settings(work_root):
    s = dict(DEFAULT_SETTINGS)
    s.update(read_json(settings_path(work_root), {}) or {})
    return s


def save_settings(work_root, patch):
    cur = load_settings(work_root)
    for k, v in (patch or {}).items():
        if k in DEFAULT_SETTINGS:
            cur[k] = v
    write_json(settings_path(work_root), cur)
    return cur


class Store:
    """一个 workRoot 下的所有状态。线程安全（写操作加锁 + 原子替换）。"""

    def __init__(self, work_root, log=None):
        self.root = os.path.abspath(work_root)
        ensure_dir(self.root)
        self.log = log
        self._lock = threading.RLock()
        self.settings = load_settings(self.root)

    # ------------------------------------------------------------ settings

    def reload_settings(self):
        self.settings = load_settings(self.root)
        return self.settings

    def update_settings(self, patch):
        with self._lock:
            self.settings = save_settings(self.root, patch)
        return self.settings

    def with_cookie(self, cookie):
        s = dict(self.settings)
        s['_cookie'] = cookie or ''
        return s

    # ------------------------------------------------------------ ups

    def ups(self):
        return read_json(os.path.join(self.root, 'ups.json'), {'items': []}) or {'items': []}

    def save_ups(self, data):
        with self._lock:
            write_json(os.path.join(self.root, 'ups.json'), data)

    def find_up(self, mid):
        for up in self.ups().get('items') or []:
            if str(up.get('mid')) == str(mid):
                return up
        return None

    def upsert_up(self, up):
        with self._lock:
            data = self.ups()
            items = data.setdefault('items', [])
            for i, cur in enumerate(items):
                if str(cur.get('mid')) == str(up.get('mid')):
                    items[i] = {**cur, **up}
                    break
            else:
                items.append(up)
            write_json(os.path.join(self.root, 'ups.json'), data)
            return up

    def remove_up(self, mid):
        with self._lock:
            data = self.ups()
            data['items'] = [u for u in data.get('items') or [] if str(u.get('mid')) != str(mid)]
            write_json(os.path.join(self.root, 'ups.json'), data)

    # ------------------------------------------------------------ sessions

    def sessions_root(self):
        base = self.settings.get('recordRoot') or ''
        return base if base else os.path.join(self.root, 'sessions')

    def session_dir(self, session_id):
        return os.path.join(self.sessions_root(), session_id)

    def new_session(self, mid, meta):
        sid = '%s_%s' % (mid, time.strftime('%Y%m%d_%H%M%S'))
        d = self.session_dir(sid)
        ensure_dir(d)
        sess = Session(self, sid, d)
        sess.data = {
            'id': sid, 'mid': str(mid), 'createdAt': now(),
            'status': 'recording', **meta,
            'parts': [], 'danmaku': 0, 'cover': '', 'finalized': [],
        }
        sess.save()
        return sess

    def load_session(self, session_id):
        d = self.session_dir(session_id)
        if not os.path.isdir(d):
            return None
        sess = Session(self, session_id, d)
        sess.load()
        return sess

    def list_sessions(self, limit=100):
        root = self.sessions_root()
        out = []
        if not os.path.isdir(root):
            return out
        names = sorted(os.listdir(root), reverse=True)
        for name in names[:limit]:
            d = os.path.join(root, name)
            if not os.path.isdir(d):
                continue
            meta = read_json(os.path.join(d, 'meta.json'), None)
            if meta:
                out.append(meta)
        return out

    # ------------------------------------------------------------ queue

    def queue(self):
        return read_json(os.path.join(self.root, 'queue.json'), {'items': []}) or {'items': []}

    def save_queue(self, data):
        with self._lock:
            write_json(os.path.join(self.root, 'queue.json'), data)

    def queue_add(self, items):
        with self._lock:
            data = self.queue()
            seq = data.get('seq') or 0
            added = []
            for it in items:
                seq += 1
                it = dict(it)
                it['id'] = 'p%05d' % seq
                it.setdefault('status', 'pending')
                it.setdefault('createdAt', now())
                data['items'].append(it)
                added.append(it)
            data['seq'] = seq
            write_json(os.path.join(self.root, 'queue.json'), data)
            return added

    def queue_update(self, item_id, patch):
        with self._lock:
            data = self.queue()
            hit = None
            for it in data.get('items') or []:
                if it.get('id') == item_id:
                    it.update(patch)
                    hit = it
                    break
            write_json(os.path.join(self.root, 'queue.json'), data)
            return hit

    def queue_items(self, statuses=None):
        items = self.queue().get('items') or []
        if statuses:
            items = [i for i in items if i.get('status') in statuses]
        return items

    # ------------------------------------------------------------ uploads

    def upload_path(self, item_id):
        d = ensure_dir(os.path.join(self.root, 'uploads'))
        return os.path.join(d, '%s.json' % item_id)

    def upload_status(self, item_id):
        return read_json(self.upload_path(item_id), {}) or {}

    def set_upload_status(self, item_id, payload):
        with self._lock:
            cur = self.upload_status(item_id)
            cur.update(payload)
            cur['updatedAt'] = now()
            write_json(self.upload_path(item_id), cur)
            return cur

    # ------------------------------------------------------------ daemon

    def daemon_path(self):
        return os.path.join(self.root, 'daemon.json')

    def daemon_state(self):
        return read_json(self.daemon_path(), {}) or {}

    def set_daemon_state(self, payload):
        with self._lock:
            cur = self.daemon_state()
            cur.update(payload)
            cur['updatedAt'] = now()
            write_json(self.daemon_path(), cur)
            return cur


class Session:
    """一次直播（一个目录）。分P 列表写进 meta.json，随时可读。"""

    def __init__(self, store, sid, directory):
        self.store = store
        self.id = sid
        self.dir = directory
        self.data = {}
        self._lock = threading.RLock()

    def load(self):
        self.data = read_json(os.path.join(self.dir, 'meta.json'), {}) or {}

    def save(self):
        with self._lock:
            write_json(os.path.join(self.dir, 'meta.json'), self.data)

    def update(self, patch):
        with self._lock:
            self.data.update(patch)
            write_json(os.path.join(self.dir, 'meta.json'), self.data)
            return self.data

    def parts(self):
        return self.data.get('parts') or []

    def add_part(self, part):
        with self._lock:
            parts = self.data.setdefault('parts', [])
            for i, p in enumerate(parts):
                if p.get('index') == part.get('index'):
                    parts[i] = {**p, **part}
                    break
            else:
                parts.append(part)
            write_json(os.path.join(self.dir, 'meta.json'), self.data)
        return part

    def update_part(self, index, patch):
        with self._lock:
            for p in self.data.setdefault('parts', []):
                if p.get('index') == index:
                    p.update(patch)
                    write_json(os.path.join(self.dir, 'meta.json'), self.data)
                    return p
        return None

    @property
    def room_id(self):
        return int(self.data.get('roomId') or 0)

    def log_path(self):
        return os.path.join(self.dir, 'run.log')
