# -*- coding: utf-8 -*-
"""录播守护进程：轮询开播 → 抓流 + 录弹幕 → 下播收尾 → 进待发布队列。

一个进程管所有 UP 主，状态全部落盘（daemon.json / sessions/*/meta.json / queue.json），
所以插件重启、DSH 重启都不影响已录内容；进程活着就能继续录。
"""
import json
import os
import shutil
import signal
import subprocess
import threading
import time

from . import capture as cap
from . import danmaku as dm
from . import finalize as fin
from . import live, publish
from . import __version__
from .store import Store
from .util import fmt_size, iso, now, read_json, write_json, Log, pid_alive as _pid_alive


def disk_free_gb(path):
    try:
        return shutil.disk_usage(path).free / (1024.0 ** 3)
    except Exception:
        return 999.0


class Daemon:
    def __init__(self, work_root, cookie='', log_path=''):
        self.work_root = os.path.abspath(work_root)
        self.log_path = log_path or os.path.join(self.work_root, 'daemon.log')
        self.log = Log(self.log_path)
        self.store = Store(self.work_root, log=self.log)
        self.cookie = cookie
        self.recorders = {}       # mid -> ctx
        self.finalizers = []      # 正在收尾的线程
        self._offline_hits = {}   # mid -> 连续「未开播」次数（防抖：避免掉线抖动切成两段）
        self._cooldown = {}       # mid -> 冷却到期时间（空转一次后别立刻重试）
        self._stop = threading.Event()
        self.started = now()
        self.events_path = os.path.join(self.work_root, 'events.jsonl')
        self.control_path = os.path.join(self.work_root, 'control.json')

    # ------------------------------------------------------------ 事件

    def event(self, kind, text, extra=None):
        rec = {'at': now(), 'time': iso(), 'kind': kind, 'text': text}
        if extra:
            rec.update(extra)
        try:
            with open(self.events_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')
        except Exception:
            pass
        self.log('[事件] %s：%s', kind, text)

    # ------------------------------------------------------------ 主循环

    def run(self):
        if not self._acquire():
            self.log('已有录播守护进程在跑，退出')
            return 2
        self.log('录播守护进程启动 v%s，工作目录 %s', __version__, self.work_root)
        self.event('daemon-start', '录播守护进程启动')
        try:
            self._recover_orphans()
        except Exception:
            self.log.exc('恢复残留会话失败（继续运行）')
        settings = self.store.reload_settings()
        last_poll = 0.0
        try:
            while not self._stop.is_set():
                try:
                    settings = self.store.reload_settings()
                    self._handle_control()
                    if time.time() - last_poll >= max(10, int(settings.get('pollSeconds') or 30)):
                        last_poll = time.time()
                        self._poll_all(settings)
                    self._reap()
                    self._reconcile()
                    self._heartbeat(settings)
                except Exception:
                    self.log.exc('主循环异常（继续运行）')
                for _ in range(10):
                    if self._stop.is_set():
                        break
                    time.sleep(0.5)
        finally:
            self._shutdown()
        return 0

    def stop(self):
        self._stop.set()

    def _acquire(self):
        """单实例锁：daemon.json 可能被手动杀进程弄脏，所以再用 O_EXCL 锁文件兜一层。"""
        lock = os.path.join(self.work_root, 'daemon.lock')
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            self._lock_path = lock
        except FileExistsError:
            other = 0
            try:
                with open(lock, encoding='utf-8') as f:
                    other = int(f.read().strip() or 0)
            except Exception:
                other = 0
            if other and other != os.getpid() and _pid_alive(other):
                self.log('另一个守护进程（PID %s）已在运行，退出', other)
                return False
            try:
                os.remove(lock)
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self._lock_path = lock
            except Exception as e:
                self.log('拿不到单实例锁：%s', e)
                return False
        state = self.store.daemon_state()
        pid = state.get('pid')
        if pid and pid != os.getpid() and _pid_alive(pid):
            self.log('daemon.json 里记录的 PID %s 仍存活，退出（避免双开）', pid)
            self._release_lock()
            return False
        self.store.set_daemon_state({'pid': os.getpid(), 'startedAt': now(),
                                     'version': __version__, 'stale': False})
        try:
            signal.signal(signal.SIGTERM, lambda *a: self.stop())
            signal.signal(signal.SIGINT, lambda *a: self.stop())
        except Exception:
            pass
        return True

    def _release_lock(self):
        try:
            if getattr(self, '_lock_path', ''):
                os.remove(self._lock_path)
                self._lock_path = ''
        except Exception:
            pass

    # ------------------------------------------------------------ 控制文件

    def _handle_control(self):
        ctrl = read_json(self.control_path, None)
        if not ctrl:
            return
        reqs = ctrl.get('requests') or []
        keep = []
        for r in reqs:
            action = r.get('action')
            mid = str(r.get('mid') or '')
            if action == 'start' and mid:
                up = self.store.find_up(mid) or {'mid': mid}
                self._start_recording(up, self.store.reload_settings(), manual=True)
            elif action == 'stop' and mid:
                self._stop_recording(mid, reason='手动停止')
            elif action == 'reload':
                pass
            elif action == 'quit':
                self.log('收到退出请求')
                self._stop.set()
            else:
                keep.append(r)
        write_json(self.control_path, {'requests': keep})

    # ------------------------------------------------------------ 轮询开播

    def _poll_all(self, settings):
        data = self.store.ups()
        ups = [u for u in (data.get('items') or []) if u.get('enabled', True)]
        free = disk_free_gb(self.work_root)
        low_disk = free < float(settings.get('minFreeGb') or 8)
        for up in ups:
            mid = str(up.get('mid') or '')
            if not mid:
                continue
            ctx = self.recorders.get(mid)
            room_id = up.get('roomId') or 0
            info = None
            try:
                if room_id:
                    info = live.room_info(room_id, cookie=self.cookie)
                else:
                    info = live.room_of_mid(mid, cookie=self.cookie)
                    if info:
                        self.store.upsert_up({**up, 'roomId': info['roomId'],
                                              'uname': up.get('uname') or '',
                                              'title': info.get('title') or up.get('title') or ''})
                        room_id = info['roomId']
            except Exception as e:
                self.log('轮询 %s 失败：%s', mid, e)
                continue
            if not info:
                continue
            up_state = {
                'mid': mid, 'roomId': room_id, 'live': bool(info.get('live')),
                'title': info.get('title') or '', 'cover': info.get('cover') or '',
                'online': info.get('online') or 0,
                'checkedAt': now(),
            }
            if not up.get('uname') or str(up.get('uname')).startswith('UP'):
                mi = live.master_info(mid, cookie=self.cookie)
                if mi.get('uname'):
                    up_state['uname'] = mi['uname']
                    up_state['face'] = mi.get('face') or ''
            self.store.upsert_up({**up, **up_state})
            if info.get('live'):
                self._offline_hits.pop(mid, None)
                until = self._cooldown.get(mid, 0)
                if until and now() < until:
                    if not up.get('_coolLogged') or (now() - until) > -540:
                        self.log('%s 开播了，但上次空转已冷却到 %s，先跳过', mid, iso(until))
                    continue
                if not ctx and not low_disk:
                    self._start_recording({**up, **up_state}, settings)
                elif low_disk:
                    self.log('磁盘剩余 %.1fGB 低于阈值，不开始录制', free)
            else:
                if ctx and not ctx.get('stopping'):
                    # 连续两次都判未开播才停：主播短暂掉线/接口抖动不要切成两段
                    hits = self._offline_hits.get(mid, 0) + 1
                    self._offline_hits[mid] = hits
                    if hits >= 2:
                        self._stop_recording(mid, reason='直播已结束')

        # 被移除 / 被暂停的 UP 主：录制线程不会自己停，这里主动收尾。
        # 否则 remove 之后进程还在往 flv 里写，直到手动 stop 或磁盘写满。
        watched = {str(u.get('mid') or '') for u in ups}
        for mid in list(self.recorders.keys()):
            if str(mid) in watched:
                continue
            ctx = self.recorders.get(mid)
            if ctx and not ctx.get('stopping'):
                self.log('UP %s 已不在监控列表，停止录制', mid)
                self._stop_recording(mid, reason='已从监控列表移除')

    # ------------------------------------------------------------ 录制生命周期

    def _start_recording(self, up, settings, manual=False):
        mid = str(up.get('mid') or '')
        room_id = int(up.get('roomId') or 0)
        if not room_id:
            try:
                info = live.room_of_mid(mid, cookie=self.cookie)
                room_id = int((info or {}).get('roomId') or 0)
            except Exception as e:
                self.log('拿不到直播间：%s', e)
        if not room_id:
            self.event('error', 'UP %s 没有可用的直播间号' % mid)
            return None
        if mid in self.recorders:
            return self.recorders[mid]
        info = {}
        try:
            info = live.room_info(room_id, cookie=self.cookie)
        except Exception:
            info = up or {}
        sess = self.store.new_session(mid, {
            'mid': mid,
            'uname': up.get('uname') or info.get('uname') or ('UP%s' % mid),
            'roomId': room_id,
            'title': info.get('title') or up.get('title') or '',
            'liveStart': info.get('liveTime') or 0,
            'recStart': now(),
            'manual': bool(manual),
            'host': self.work_root,
        })
        s = self.store.with_cookie(self.cookie)
        # 每个 UP 主可以单独设画质：maxQn=0/未设 = 自动取该主播当前最高档
        if up.get('maxQn') not in (None, ''):
            s['maxQn'] = int(up.get('maxQn') or 0)
        if up.get('preferCodec'):
            s['preferCodec'] = up.get('preferCodec')
        dan = None
        if self.store.settings.get('danmaku'):
            dan = dm.DanmakuRecorder(sess, room_id, self.cookie, Log(sess.log_path()), t0=sess.data['recStart'])
        recorder = cap.Capture(sess, s, Log(sess.log_path()), danmaku=dan)
        thread = threading.Thread(target=self._record_worker, args=(mid, sess, recorder),
                                  name='rec-%s' % mid, daemon=True)
        self.recorders[mid] = {'session': sess, 'capture': recorder, 'thread': thread,
                               'danmaku': dan, 'stopping': False, 'startedAt': now()}
        thread.start()
        if s.get('keepAwake', True) and settings.get('keepAwake', True):
            self._keep_awake(True)
        self.event('live-start', '%s 开播，开始录制：%s' % (
            sess.data.get('uname'), sess.data.get('title') or ''), {'mid': mid, 'sessionId': sess.id})
        return sess

    def _record_worker(self, mid, sess, recorder):
        try:
            recorder.run()
        except Exception:
            self.log.exc('录制线程异常 mid=%s' % mid)
            sess.update({'status': 'error', 'error': '录制线程异常'})
        finally:
            self.log('录制线程结束 mid=%s', mid)

    def _stop_recording(self, mid, reason=''):
        ctx = self.recorders.get(mid)
        if not ctx or ctx.get('stopping'):
            return
        ctx['stopping'] = True
        ctx['stopReason'] = reason
        ctx['capture'].request_stop()
        self.event('live-stop', '停止录制（%s）：%s' % (
            reason, ctx['session'].data.get('uname')), {'mid': mid,
                                                         'sessionId': ctx['session'].id})

    # ------------------------------------------------------------ 防休眠（macOS）

    def _keep_awake(self, on):
        """录制期间用 caffeinate 顶住休眠 —— 这台 mac 一睡，直播流就断了。"""
        if not hasattr(self, '_caff'):
            self._caff = None
        p = self._caff
        if on:
            if p is not None and p.poll() is None:
                return
            if not shutil.which('caffeinate'):
                return
            try:
                self._caff = subprocess.Popen(
                    ['caffeinate', '-i', '-m', '-s'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL)
                self.log('录制中，已开启防休眠（caffeinate pid %s）', self._caff.pid)
            except Exception:
                self._caff = None
                self.log.exc('开启防休眠失败（不影响录制）')
        else:
            if p is not None and p.poll() is None:
                try:
                    p.terminate()
                    self.log('已关闭防休眠')
                except Exception:
                    pass
            self._caff = None

    def _reap(self):
        """收割已结束的录制线程 → 收尾（单独线程，转码不阻塞轮询）。"""
        for mid, ctx in list(self.recorders.items()):
            th = ctx.get('thread')
            if th and th.is_alive():
                continue
            if not ctx.get('reaped'):
                ctx['reaped'] = True
                sess = ctx['session']
                got = sum((p.get('bytes') or 0) for p in sess.parts())
                if got <= 0:
                    # 一分数据都没拿到：删掉空目录，并给这个 UP 主上一段冷却，
                    # 否则「看着在播但拉不到流」的直播间会无限起空会话。
                    sess.update({'status': 'empty', 'recEnd': now()})
                    cooldown = 15 * 60
                    self._cooldown[mid] = now() + cooldown
                    self.event('error', '%s 这次没拿到任何数据（可能是付费/加密/无可用流），%d 分钟内不再自动重试' % (
                        sess.data.get('uname'), cooldown // 60), {'mid': mid})
                    self.log('会话 %s 空转，删除目录并冷却 %d 秒', sess.id, cooldown)
                    try:
                        import shutil
                        shutil.rmtree(sess.dir, ignore_errors=True)
                    except Exception:
                        pass
                else:
                    sess.update({'status': 'finalizing', 'recEnd': now()})
                    self._cooldown.pop(mid, None)
                    t = threading.Thread(target=self._finalize_worker, args=(mid, sess),
                                         name='fin-%s' % mid, daemon=True)
                    t.start()
                    self.finalizers = [x for x in self.finalizers if x.is_alive()]
                    self.finalizers.append(t)
            if ctx.get('reaped'):
                del self.recorders[mid]
        if not self.recorders:
            self._keep_awake(False)

    def _finalize_worker(self, mid, sess):
        settings = self.store.reload_settings()
        log = Log(sess.log_path())
        try:
            res = fin.finalize_session(self.store, sess, settings, log)
            items = res.get('items') or []
            if items:
                total = sum(i.get('bytes') or 0 for i in items)
                self.event('finalized', '%s 录制完成，%d 个分P（%s）已进待发布队列' % (
                    sess.data.get('uname'), len(items), fmt_size(total)),
                    {'mid': mid, 'sessionId': sess.id, 'items': [i['id'] for i in items]})
                if settings.get('autoPublish'):
                    self.log('已开启自动投稿，开始上传 %d 条', len(items))
                    publish.publish_items(self.store, settings, self.cookie,
                                          [i['id'] for i in items], log)
            else:
                self.event('finalized', '%s 没有录到可用内容' % sess.data.get('uname'),
                           {'mid': mid, 'sessionId': sess.id})
        except Exception as e:
            log.exc('收尾失败')
            self.event('error', '收尾失败：%s' % e, {'mid': mid, 'sessionId': sess.id})
            sess.update({'status': 'error', 'error': str(e)})
        finally:
            self.log('收尾线程结束 mid=%s', mid)

    # ------------------------------------------------------------ 心跳

    def _recover_orphans(self):
        """启动时捞回上次残留的「录制中」会话。

        进程被强杀/断电后，meta.json 会停在 recording、raw/*.flv 留在盘上没人管。
        这里把它们按「已结束」处理：能转出成品就进队列，一点数据都没有就删空目录。
        """
        recovered = 0
        for meta in self.store.list_sessions(limit=500):
            if str(meta.get('status')) not in ('recording', 'finalizing'):
                continue
            sid = meta.get('id')
            if not sid or sid in self.recorders:
                continue
            sess = self.store.load_session(sid)
            if not sess:
                continue
            parts = sess.parts()
            total = sum((p.get('bytes') or 0) for p in parts)
            # 最后一段可能还挂着 recording，收尾前先关掉，否则 finalize 会跳过它
            for p in parts:
                if p.get('status') == 'recording':
                    sess.update_part(p.get('index'), {'status': 'closed', 'endedAt': now()})
            if total <= 0:
                sess.update({'status': 'empty', 'recEnd': now()})
                try:
                    shutil.rmtree(sess.dir, ignore_errors=True)
                except Exception:
                    pass
                self.log('清理空残留会话 %s', sid)
                continue
            sess.update({'status': 'finalizing', 'recEnd': now(), 'recovered': True})
            mid = str(meta.get('mid') or '')
            t = threading.Thread(target=self._finalize_worker, args=(mid, sess),
                                 name='rec-%s' % sid, daemon=True)
            t.start()
            self.finalizers = [x for x in self.finalizers if x.is_alive()]
            self.finalizers.append(t)
            recovered += 1
            self.event('finalized', '捞回上次未收尾的录制：%s（%d 个分P，%s）' % (
                meta.get('uname') or sid, len(parts), fmt_size(total)),
                {'mid': mid, 'sessionId': sid})
        if recovered:
            self.log('启动时捞回 %d 个未收尾的会话', recovered)

    def _reconcile(self):
        """修掉「卡住」的队列条目。

        投稿是独立进程跑的；如果它被强杀（重启 DSH、任务管理器、我手动清进程），
        条目会永远停在 uploading/uploaded，面板上一直显示上传中，还没法重试。
        这里按「上传进度文件多久没更新」判定僵死，标成 failed 让面板能重试。
        """
        stale_after = 15 * 60
        nowt = now()
        for it in self.store.queue().get('items') or []:
            if it.get('status') not in ('uploading', 'uploaded'):
                continue
            up = self.store.upload_status(it['id']) or {}
            touched = up.get('updatedAt') or it.get('createdAt') or 0
            if nowt - touched < stale_after:
                continue
            self.store.queue_update(it['id'], {
                'status': 'failed',
                'error': '上传进程已退出（可能重启过），点「重试失败」重新上传',
                'failedAt': nowt,
            })
            self.log('队列条目 %s 僵死已置为失败（%d 分钟没进度）', it['id'],
                     stale_after // 60)
            self.event('error', '上传卡住已自动标红：%s（%s）' % (
                it.get('title') or it['id'], it.get('uname') or ''),
                {'itemId': it['id']})

    def _heartbeat(self, settings):
        rec = {}
        for mid, ctx in self.recorders.items():
            # 把最新设置推给正在跑的抓流器（分P 时长改完立即生效，不用重启）
            try:
                ctx['capture'].settings.update({
                    'partSeconds': settings.get('partSeconds'),
                    'maxQn': settings.get('maxQn'),
                    'preferCodec': settings.get('preferCodec'),
                })
            except Exception:
                pass
            snap = ctx['capture'].snapshot()
            dan = ctx['danmaku'].snapshot() if ctx.get('danmaku') else {'count': 0}
            rec[mid] = {'sessionId': ctx['session'].id, **snap,
                        'danmaku': dan.get('count'), 'stopping': ctx.get('stopping')}
        self.store.set_daemon_state({
            'pid': os.getpid(), 'version': __version__, 'heartbeatAt': now(),
            'recording': rec, 'uptimeSec': int(now() - self.started),
            'sessions': len(self.store.list_sessions(limit=500)),
        })

    # ------------------------------------------------------------ 退出

    def _shutdown(self):
        self.log('守护进程收尾中…')
        for mid, ctx in list(self.recorders.items()):
            try:
                ctx['capture'].request_stop()
            except Exception:
                pass
        deadline = time.time() + 40
        for mid, ctx in list(self.recorders.items()):
            th = ctx.get('thread')
            if th:
                th.join(timeout=max(1.0, deadline - time.time()))
            sess = ctx['session']
            try:
                sess.update({'status': 'finalizing', 'recEnd': now()})
                fin.finalize_session(self.store, sess, self.store.reload_settings(),
                                     Log(sess.log_path()))
            except Exception:
                self.log.exc('退出时收尾失败')
        self._keep_awake(False)
        self.store.set_daemon_state({'pid': 0, 'stoppedAt': now(), 'recording': {}})
        self._release_lock()
        self.event('daemon-stop', '录播守护进程已退出')
        self.log('守护进程已退出')


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description='B站录播姬守护进程')
    ap.add_argument('--workdir', required=True)
    ap.add_argument('--cookie-file', default='')
    ap.add_argument('--log', default='')
    args = ap.parse_args(argv)
    cookie = ''
    if args.cookie_file:
        from .httpc import load_cookie
        cookie = load_cookie(args.cookie_file)
    d = Daemon(args.workdir, cookie=cookie, log_path=args.log)
    return d.run()


if __name__ == '__main__':
    raise SystemExit(main())
