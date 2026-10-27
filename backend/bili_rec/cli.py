# -*- coding: utf-8 -*-
"""命令行入口：插件（Host 侧）只通过它读写后端，所有结果都是 JSON。

  python -m bili_rec.cli status --workdir W --json-out R.json
  python -m bili_rec.cli ups add --mid 123 --workdir W
  python -m bili_rec.cli queue publish --ids p00001,p00002 --workdir W
"""
import argparse
import json
import os
import sys
import time

from . import finalize as fin
from . import live, publish as pub, upload as up
from . import __version__
from .store import Store, DEFAULT_SETTINGS
from .util import Log, fmt_size, iso, now, read_json, write_json, tail, pid_alive
from .daemon import disk_free_gb

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------- 输出

class Out:
    def __init__(self, path=''):
        self.path = path
        self.data = {'ok': True, 'version': __version__}

    def set(self, **kw):
        self.data.update(kw)

    def fail(self, msg):
        self.data['ok'] = False
        self.data['error'] = str(msg)

    def emit(self):
        text = json.dumps(self.data, ensure_ascii=False, indent=1)
        if self.path:
            write_json(self.path, self.data)
        sys.stdout.write(text + '\n')
        return text


# ---------------------------------------------------------------- 公共

def cookie_candidates(explicit=''):
    """cookie 文件位置：显式参数 → 本后端目录 → 隔壁 bili-quark 后端目录（复用已有凭据）。"""
    out = []
    if explicit:
        out.append(explicit)
    if os.environ.get('BILI_REC_COOKIE'):
        out.append(os.environ['BILI_REC_COOKIE'])
    out.append(os.path.join(BACKEND_DIR, 'bili_cookie.txt'))
    out.append(os.path.join(os.path.dirname(BACKEND_DIR), 'deepseek_bilibili', 'bili_cookie.txt'))
    return [p for p in out if p]


def make_ctx(args):
    work = os.path.abspath(args.workdir)
    cookie_file = ''
    cookie = ''
    from .httpc import load_cookie
    for cand in cookie_candidates(getattr(args, 'cookie_file', '') or ''):
        if os.path.exists(cand):
            cookie_file = cand
            cookie = load_cookie(cand)
            if cookie:
                break
    store = Store(work)
    return store, cookie, cookie_file


def daemon_alive(store):
    st = store.daemon_state()
    pid = st.get('pid')
    if pid and pid_alive(pid):
        hb = st.get('heartbeatAt') or st.get('updatedAt') or 0
        return True, pid, hb, st
    return False, pid, 0, st


def push_control(store, req):
    path = os.path.join(store.root, 'control.json')
    cur = read_json(path, {'requests': []}) or {'requests': []}
    cur.setdefault('requests', []).append({**req, 'at': now()})
    write_json(path, cur)
    return cur


# ---------------------------------------------------------------- 各命令

def cmd_status(args, out):
    store, cookie, cookie_file = make_ctx(args)
    alive, pid, hb, dst = daemon_alive(store)
    sessions = store.list_sessions(limit=args.limit)
    queue = store.queue().get('items') or []
    ups = (store.ups().get('items') or [])
    rec = (dst.get('recording') or {}) if alive else {}
    # 叠加每个 UP 的最新状态 + 当前录制进度
    items = []
    for u in ups:
        mid = str(u.get('mid'))
        cur = rec.get(mid) or {}
        items.append({**u, 'recording': cur, 'isRecording': bool(cur)})
    counts = {}
    for it in queue:
        counts[it.get('status')] = counts.get(it.get('status'), 0) + 1
    # 每个录制会话在队列里还有几条（不含已删除的）
    by_sid = {}
    for it in queue:
        if it.get('status') in ('deleted',):
            continue
        sid = it.get('sessionId') or ''
        if sid:
            by_sid[sid] = by_sid.get(sid, 0) + 1
    out.set(
        workdir=store.root,
        daemon={
            'alive': alive, 'pid': pid if alive else 0,
            'heartbeat': iso(hb) if hb else '',
            'version': dst.get('version') or '',
            'uptimeSec': dst.get('uptimeSec') or 0,
        },
        settings=store.settings,
        ups=items,
        sessions=[_slim_session(s, by_sid.get(s.get('id'), 0),
                                _local_of(store, s.get('id')))
                  for s in sessions],
        queue=_slim_items(queue, limit=args.queueLimit),
        queueCounts=counts,
        disk={'freeGb': round(disk_free_gb(store.root), 1)},
        cookie={'file': cookie_file, 'present': bool(cookie),
                'bytes': len(cookie)},
    )
    if not alive:
        out.set(note='守护进程未运行，不会自动录制')


def _slim_session(s, queue_count=0, local=None):
    parts = s.get('parts') or []
    local = local or {'files': 0, 'bytes': 0}
    return {
        'id': s.get('id'), 'mid': s.get('mid'), 'uname': s.get('uname'),
        'title': s.get('title'), 'roomId': s.get('roomId'),
        'status': s.get('status'),
        'recStart': s.get('recStart'), 'recEnd': s.get('recEnd'),
        'startText': iso(s.get('recStart')) if s.get('recStart') else '',
        'parts': len(parts),
        'durationSec': round(sum((p.get('durationMs') or 0) for p in parts) / 1000.0, 1),
        'bytes': sum((p.get('bytes') or 0) for p in parts),
        'danmaku': s.get('danmaku') or 0,
        'cover': s.get('cover') or '',
        # 下面三个是「真实存在性」：队列条目被删、或分段太短被丢弃后，
        # 会话记录还在，但本地可能已经没有任何成品文件了。
        'localFiles': local['files'],
        'localBytes': local['bytes'],
        'queueCount': queue_count,
    }


def _local_of(store, session_id):
    d = store.session_dir(session_id)
    files = 0
    size = 0
    try:
        for name in os.listdir(d):
            if name.lower().endswith('.mp4') and not name.endswith('.part.mp4'):
                files += 1
                try:
                    size += os.path.getsize(os.path.join(d, name))
                except Exception:
                    pass
    except Exception:
        pass
    return {'files': files, 'bytes': size}


def _slim_items(items, limit=100):
    out = []
    for it in items[-limit:][::-1]:
        out.append({
            'id': it.get('id'), 'sessionId': it.get('sessionId'),
            'mid': it.get('mid'), 'uname': it.get('uname'),
            'partIndex': it.get('partIndex'), 'title': it.get('title'),
            'bytes': it.get('bytes'), 'durationSec': it.get('durationSec'),
            'width': it.get('width'), 'height': it.get('height'),
            'cover': it.get('cover'), 'status': it.get('status'),
            'error': it.get('error') or '', 'tid': it.get('tid'),
            'tag': it.get('tag'), 'desc': it.get('desc'),
            'visibility': it.get('visibility'), 'dtime': it.get('dtime'),
            'aid': it.get('aid'), 'bvid': it.get('bvid'),
            'createdAt': it.get('createdAt'), 'file': it.get('file'),
            'fileDeleted': bool(it.get('fileDeleted')),
            'visibilityApplied': it.get('visibilityApplied') or '',
            'visibilityNote': it.get('visibilityNote') or '',
        })
    return out


def cmd_ups(args, out):
    store, cookie, _ = make_ctx(args)
    if args.action == 'list':
        out.set(ups=store.ups().get('items') or [])
        return
    if args.action == 'add':
        mid = str(args.mid).strip()
        if not mid.isdigit():
            out.fail('mid 必须是数字 UID')
            return
        info = live.room_of_mid(mid, cookie=cookie)
        if not info:
            out.fail('拿不到该 UID 的直播间（可能没开通过直播）')
            return
        uname = ''
        try:
            uname = live.master_info(mid, cookie=cookie).get('uname') or ''
        except Exception:
            pass
        up_rec = {'mid': mid, 'uname': uname or ('UP%s' % mid),
                  'roomId': info['roomId'], 'enabled': True,
                  'live': info['live'], 'title': info.get('title') or '',
                  'cover': info.get('cover') or '', 'addedAt': now()}
        store.upsert_up(up_rec)
        out.set(up=up_rec)
        return
    if args.action == 'remove':
        store.remove_up(args.mid)
        out.set(removed=str(args.mid))
        return
    if args.action == 'toggle':
        cur = store.find_up(args.mid)
        if not cur:
            out.fail('没有这个 UP 主')
            return
        store.upsert_up({**cur, 'enabled': bool(args.enabled)})
        out.set(up=store.find_up(args.mid))
        return
    out.fail('未知 ups 动作：%s' % args.action)


def cmd_record(args, out):
    store, cookie, _ = make_ctx(args)
    alive, pid, _hb, dst = daemon_alive(store)
    if not alive:
        out.fail('守护进程没在运行（先在面板里点「启动守护进程」）')
        return
    if args.action == 'start':
        up = store.find_up(args.mid)
        if not up:
            out.fail('没有这个 UP 主，先添加')
            return
        push_control(store, {'action': 'start', 'mid': str(args.mid)})
        out.set(started=str(args.mid), note='已请求开始录制，下个轮询周期生效')
        return
    if args.action == 'stop':
        push_control(store, {'action': 'stop', 'mid': str(args.mid)})
        out.set(stopped=str(args.mid))
        return


def cmd_daemon(args, out):
    store, cookie, _ = make_ctx(args)
    alive, pid, hb, dst = daemon_alive(store)
    if args.action == 'stop':
        if alive:
            push_control(store, {'action': 'quit'})
            out.set(stopped=True, pid=pid, note='已请求退出，正在收尾')
        else:
            out.set(stopped=False, note='本来就没在运行')
        return
    if args.action == 'status':
        out.set(alive=alive, pid=pid if alive else 0,
                heartbeat=iso(hb) if hb else '', state=dst)
        return
    out.fail('未知 daemon 动作：%s（start 由插件直接拉起进程）' % args.action)


def delete_queue_items(store, ids):
    """从队列里彻底删掉条目，并删除本地成品文件（面板的「删除」就是这个）。"""
    ids = [i for i in (ids or []) if i]
    if not ids:
        return {'ok': False, 'error': '没指定要删的条目'}
    data = store.queue()
    items = data.get('items') or []
    drop = [it for it in items if it.get('id') in ids]
    keep = [it for it in items if it.get('id') not in ids]
    if not drop:
        return {'ok': False, 'error': '队列里没有这些条目'}
    freed = 0
    files = 0
    audit = []
    raw_dirs = set()
    for it in drop:
        path = it.get('file') or ''
        size = 0
        if path and os.path.exists(path):
            try:
                size = os.path.getsize(path)
                os.remove(path)
                freed += size
                files += 1
            except Exception as e:
                audit.append({'at': now(), 'id': it['id'], 'title': it.get('title'),
                              'file': path, 'bytes': 0, 'status': it.get('status'),
                              'error': '删文件失败：%s' % e})
                continue
        if path:
            raw_dirs.add(os.path.join(os.path.dirname(path), 'raw'))
        up = store.upload_path(it['id'])
        try:
            if os.path.exists(up):
                os.remove(up)
        except Exception:
            pass
        audit.append({'at': now(), 'id': it['id'], 'title': it.get('title'),
                      'file': path, 'bytes': size, 'status': it.get('status')})
    data['items'] = keep
    store.save_queue(data)
    # 本地为空的分P 目录顺手收掉
    for d in raw_dirs:
        try:
            if os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
        except Exception:
            pass
    try:
        with open(os.path.join(store.root, 'deleted.jsonl'), 'a', encoding='utf-8') as f:
            for a in audit:
                f.write(json.dumps(a, ensure_ascii=False) + '\n')
    except Exception:
        pass
    return {'ok': True, 'deleted': [it['id'] for it in drop], 'count': len(drop),
            'filesDeleted': files, 'freedBytes': freed, 'freedText': fmt_size(freed)}


def cmd_queue(args, out):
    store, cookie, _ = make_ctx(args)
    if args.action == 'list':
        items = store.queue().get('items') or []
        out.set(items=items)
        return
    if args.action == 'update':
        patch = json.loads(args.json or '{}')
        hit = store.queue_update(args.id, patch)
        if not hit:
            out.fail('没有这一条')
            return
        out.set(item=hit)
        return
    if args.action == 'discard':
        ids = [i for i in (args.ids or '').split(',') if i]
        out.set(**delete_queue_items(store, ids))
        return
    if args.action == 'retry':
        ids = [i for i in (args.ids or '').split(',') if i]
        for i in ids:
            store.queue_update(i, {'status': 'pending', 'error': ''})
        out.set(retried=ids)
        return
    if args.action == 'publish':
        ids = [i for i in (args.ids or '').split(',') if i]
        if not ids:
            out.fail('没指定要投稿的条目')
            return
        if not cookie:
            out.fail('没有 B 站 cookie（需要 SESSDATA + bili_jct）')
            return
        log = Log(os.path.join(store.root, 'publish.log'))
        res = pub.publish_items(store, store.settings, cookie, ids, log)
        out.set(**res)
        return
    out.fail('未知 queue 动作：%s' % args.action)


def cmd_settings(args, out):
    store, cookie, _ = make_ctx(args)
    if args.action == 'get':
        out.set(settings=store.settings, defaults=DEFAULT_SETTINGS)
        return
    if args.action == 'set':
        patch = json.loads(args.json or '{}')
        out.set(settings=store.update_settings(patch))
        return
    out.fail('未知 settings 动作：%s' % args.action)


def cmd_auth(args, out):
    store, cookie, cookie_file = make_ctx(args)
    ok, info, _raw = live.nav(cookie)
    out.set(loggedIn=ok, account=info, cookieFile=cookie_file,
            hasCookie=bool(cookie), note='' if ok else 'cookie 失效或缺失，需要重新粘贴')
    # 顺手把账号的合集列表带回去：面板选合集用它（避免再开一个接口）
    if ok:
        try:
            res = up.list_seasons(cookie)
            out.set(seasons=res.get('seasons') or [], seasonsError=res.get('error') or '')
        except Exception as e:
            out.set(seasons=[], seasonsError=str(e))
    else:
        out.set(seasons=[], seasonsError='未登录')
    out.set(seasonMap=store.settings.get('seasonMap') or {})
    if ok and not store.settings.get('publishReady'):
        pass


def cmd_sessions(args, out):
    store, cookie, _ = make_ctx(args)
    if args.action == 'list':
        out.set(sessions=[_slim_session(s) for s in store.list_sessions(limit=args.limit)])
        return
    if args.action == 'finalize':
        sess = store.load_session(args.id)
        if not sess:
            out.fail('没有这个录制会话')
            return
        log = Log(sess.log_path())
        res = fin.finalize_session(store, sess, store.settings, log)
        out.set(result={**res, 'items': [{'id': i['id'], 'title': i['title']} for i in res.get('items') or []]})
        return
    if args.action == 'delete':
        sess = store.load_session(args.id)
        if not sess:
            out.fail('没有这个录制会话')
            return
        import shutil
        shutil.rmtree(sess.dir, ignore_errors=True)
        out.set(deleted=args.id)
        return
    out.fail('未知 sessions 动作：%s' % args.action)


def cmd_log(args, out):
    store, _, _ = make_ctx(args)
    if args.session:
        sess = store.load_session(args.session)
        path = sess.log_path() if sess else ''
    else:
        path = os.path.join(store.root, 'daemon.log')
    out.set(path=path, text=tail(path, args.lines or 80))


def cmd_publish_options(args, out):
    """读 B 站当前的投稿选项（分区表/是否支持定时等），给设置页做下拉。"""
    store, cookie, _ = make_ctx(args)
    tids = up.TID_LIST
    bili_tids = []
    try:
        r = live.httpc.get_json(
            'https://member.bilibili.com/x/vupre/web/archive/pre',
            cookie=cookie,
            headers={'Referer': 'https://member.bilibili.com/york/videoup',
                     'Origin': 'https://member.bilibili.com'})
        for t in ((r.get('data') or {}).get('typelist') or []):
            subs = [{'tid': s.get('id'), 'name': s.get('name')} for s in (t.get('sub') or [])]
            bili_tids.append({'tid': t.get('id'), 'name': t.get('name'), 'sub': subs})
    except Exception as e:
        out.set(warn='拉 B 站分区表失败：%s' % e)
    out.set(tids=[{'tid': t, 'name': n} for t, n in tids], biliTids=bili_tids)


def cmd_probe(args, out):
    """探测一次取流（不录制），用于验证 cookie/清晰度。"""
    store, cookie, _ = make_ctx(args)
    try:
        info = live.room_info(args.room, cookie=cookie) if args.room else live.room_of_mid(args.mid, cookie=cookie)
    except Exception as e:
        out.fail(str(e))
        return
    if not info:
        out.fail('没有这个直播间')
        return
    out.set(room=info)
    if not info.get('live'):
        out.set(streams=[], note='当前未开播')
        return
    try:
        streams = live.playurl(info['roomId'], qn=args.qn, cookie=cookie)
    except Exception as e:
        out.fail(str(e))
        return
    out.set(streams=[{k: v for k, v in s.items() if k != 'url'} for s in streams])


def cmd_upload_test(args, out):
    """投稿链路自检：上传一个小文件并提交一条「定时发布」稿件，然后删掉。"""
    store, cookie, _ = make_ctx(args)
    log = Log(os.path.join(store.root, 'publish.log'))
    path = args.file
    if not os.path.exists(path):
        out.fail('文件不存在')
        return
    out.set(file=path, bytes=os.path.getsize(path))
    res = up.upload_file(cookie, path, cli_log=log)
    out.set(upload=res)
    if args.submit:
        meta = {
            'title': args.title or 'DSH 录播姬自检',
            'desc': '这是一条自检投稿，稍后会被删除。',
            'tag': '自检',
            'tid': int(args.tid or 21),
            'cover': '',
            'copyright': 1,
            'videos': [{'filename': res['filename'], 'title': args.title or '自检'}],
            'dtime': int(time.time()) + 3 * 3600,
            'visibility': 'private' if args.private else 'public',
        }
        r = up.submit(cookie, meta)
        out.set(submit=r)
        if r.get('aid') and args.private:
            out.set(private=up.set_private(cookie, r['aid'], {
                'title': meta['title'], 'desc': meta['desc'], 'tag': meta['tag'],
                'tid': meta['tid'], 'videos': meta['videos'], 'dtime': meta['dtime']}))
        if args.delete and r.get('aid'):
            time.sleep(2)
            out.set(deleted=up.delete_archive(cookie, r['aid']))


# ---------------------------------------------------------------- 入口

def build_parser():
    ap = argparse.ArgumentParser(prog='bili_rec', description='B站录播姬后端')
    ap.add_argument('--workdir', default=os.environ.get('BILI_REC_WORK')
                    or os.path.join(BACKEND_DIR, '_bili_live_work'))
    ap.add_argument('--cookie-file', default=os.environ.get('BILI_REC_COOKIE') or '')
    ap.add_argument('--json-out', default='')
    ap.add_argument('--version', action='version', version=__version__)
    sub = ap.add_subparsers(dest='cmd', required=True)

    # 全局参数也允许写在子命令后面（SUPPRESS 保证不会覆盖前面的值）
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--workdir', default=argparse.SUPPRESS)
    common.add_argument('--cookie-file', dest='cookie_file', default=argparse.SUPPRESS)
    common.add_argument('--json-out', dest='json_out', default=argparse.SUPPRESS)

    def new(name):
        return sub.add_parser(name, parents=[common])

    p = new('status'); p.add_argument('--limit', type=int, default=30)
    p.add_argument('--queue-limit', dest='queueLimit', type=int, default=60)

    p = new('ups')
    p.add_argument('action', choices=['list', 'add', 'remove', 'toggle'])
    p.add_argument('--mid', default='')
    p.add_argument('--enabled', type=int, default=1)

    p = new('record')
    p.add_argument('action', choices=['start', 'stop'])
    p.add_argument('--mid', required=True)

    p = new('daemon')
    p.add_argument('action', choices=['status', 'stop'])

    p = new('queue')
    p.add_argument('action', choices=['list', 'update', 'discard', 'retry', 'publish'])
    p.add_argument('--id', default=''); p.add_argument('--ids', default='')
    p.add_argument('--json', default='')

    p = new('settings')
    p.add_argument('action', choices=['get', 'set'])
    p.add_argument('--json', default='')

    new('auth')
    new('publish-options')

    p = new('sessions')
    p.add_argument('action', choices=['list', 'finalize', 'delete'])
    p.add_argument('--id', default='')
    p.add_argument('--limit', type=int, default=50)

    p = new('log')
    p.add_argument('--session', default='')
    p.add_argument('--lines', type=int, default=80)

    p = new('probe')
    p.add_argument('--mid', default=''); p.add_argument('--room', type=int, default=0)
    p.add_argument('--qn', type=int, default=10000)

    p = new('upload-test')
    p.add_argument('--file', required=True)
    p.add_argument('--submit', type=int, default=0)
    p.add_argument('--delete', type=int, default=0)
    p.add_argument('--private', type=int, default=1)
    p.add_argument('--title', default='')
    p.add_argument('--tid', type=int, default=21)

    return ap


COMMANDS = {
    'status': cmd_status, 'ups': cmd_ups, 'record': cmd_record, 'daemon': cmd_daemon,
    'queue': cmd_queue, 'settings': cmd_settings, 'auth': cmd_auth,
    'publish-options': cmd_publish_options, 'sessions': cmd_sessions, 'log': cmd_log,
    'probe': cmd_probe, 'upload-test': cmd_upload_test,
}


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    out = Out(args.json_out)
    fn = COMMANDS.get(args.cmd)
    try:
        fn(args, out)
    except Exception as e:
        import traceback
        out.fail('%s: %s' % (type(e).__name__, e))
        out.set(trace=traceback.format_exc()[-1500:])
    out.emit()
    return 0 if out.data.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
