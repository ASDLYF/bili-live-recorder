# -*- coding: utf-8 -*-
"""把待发布队列里的条目真正投出去：逐P上传 → 一次提交 → 记录结果。"""
import os
import re
import shutil
import time

from . import upload as up
from .util import fmt_size

_P_SUFFIX = re.compile(r'\s*P\d+\s*$')


def _strip_part_suffix(title):
    return _P_SUFFIX.sub('', title or '')


def publish_items(store, settings, cookie, item_ids, log, delete_local=None):
    """按会话分组投稿。item_ids 顺序即分P 顺序。"""
    data = store.queue()
    by_id = {it['id']: it for it in data.get('items') or []}
    wanted = [by_id[i] for i in item_ids if i in by_id]
    if not wanted:
        return {'ok': False, 'error': '队列里没有这些条目', 'done': [], 'failed': []}
    missing = [it['id'] for it in wanted
               if it.get('status') not in ('pending', 'failed')]
    if missing:
        return {'ok': False, 'error': '这些条目已经投过、正在投，或不可投：%s' % ', '.join(missing)}

    groups = {}
    order = []
    for it in wanted:
        key = it.get('sessionId') or it['id']
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(it)

    totals = {'done': [], 'failed': []}
    for key in order:
        items = sorted(groups[key], key=lambda x: int(x.get('partIndex') or 1))
        totals.update(_publish_group(store, settings, cookie, items, log,
                                     delete_local if delete_local is not None
                                     else settings.get('deleteLocalAfterPublish', True)))
    return {'ok': True, **totals,
            'done': totals['done'], 'failed': totals['failed']}


def _publish_group(store, settings, cookie, items, log, delete_local):
    done, failed = [], []
    uploaded = []
    first = items[0]
    try:
        for it in items:
            path = it.get('file') or ''
            if not os.path.exists(path):
                raise RuntimeError('文件不见了：%s' % path)
            store.queue_update(it['id'], {'status': 'uploading', 'phase': 'upload'})
            total = os.path.getsize(path)
            last_report = [0.0]

            def progress(sent, size, item_id=it['id'], name=os.path.basename(path)):
                pct = int(sent * 100 / size) if size else 0
                now = time.time()
                if now - last_report[0] < 0.8 and sent != size:
                    return
                last_report[0] = now
                store.set_upload_status(item_id, {
                    'phase': 'uploading', 'percent': pct, 'sent': sent, 'total': size,
                    'file': name, 'note': '%s / %s' % (fmt_size(sent), fmt_size(size)),
                })

            log('开始上传 %s（%s）', os.path.basename(path), fmt_size(total))
            res = up.upload_file(cookie, path, progress=progress, cli_log=log)
            store.set_upload_status(it['id'], {
                'phase': 'uploaded', 'percent': 100, 'note': '已上传，等待合并提交'})
            uploaded.append({'filename': res['filename'], 'title': it.get('title') or ''})
            # 记下 B 站侧的 filename：以后要改可见范围/编辑稿件还得用它
            store.queue_update(it['id'], {'status': 'uploaded', 'phase': 'submit',
                                          'uploadedName': res['filename']})

        # 封面：优先用本组第一条的封面
        cover_url = ''
        if first.get('cover') and os.path.exists(first['cover']):
            try:
                cover_url = up.upload_cover(cookie, first['cover'])
                log('封面上传成功：%s', cover_url)
            except Exception as e:
                log('封面上传失败（不影响投稿）：%s', e)

        title = _strip_part_suffix(first.get('title') or '')
        if len(items) > 1:
            title = '%s（共%dP）' % (title, len(items))
        meta = {
            'title': title[:80],
            'desc': first.get('desc') or '',
            'tag': first.get('tag') or '',
            'tid': first.get('tid') or settings.get('tid') or 21,
            'cover': cover_url,
            'copyright': first.get('copyright') or 1,
            'source': first.get('source') or '',
            'noReprint': first.get('noReprint') or 0,
            'openElec': first.get('openElec') or 0,
            'closeDanmaku': first.get('closeDanmaku') or 0,
            'closeReply': first.get('closeReply') or 0,
            'dynamic': first.get('dynamic') or '',
            'dtime': int(first.get('dtime') or 0),
            'videos': uploaded,
        }
        # 目标合集（按 UP 主配置）
        season = (settings.get('seasonMap') or {}).get(str(first.get('mid'))) or {}
        season_id = int(season.get('id') or 0)
        season_title = season.get('title') or ''
        if season_id:
            meta['seasonId'] = season_id
            log('目标合集：%s（%s）', season_title or season_id, season_id)
        season_result = {'ok': True, 'id': season_id, 'title': season_title}

        log('提交稿件：%s（%dP）', meta['title'], len(uploaded))
        res = up.submit(cookie, meta)
        aid, bvid = res.get('aid'), res.get('bvid')
        log('投稿成功：aid=%s bvid=%s', aid, bvid)

        vis = first.get('visibility') or settings.get('visibility') or 'private'
        if vis not in ('private', 'public', 'scheduled'):
            vis = 'private'
        # 可见范围/禁止转载/充电这些开关走「提交后编辑」，提交时带会被判 21001
        extras = {
            'title': meta['title'], 'desc': meta['desc'], 'tag': meta['tag'],
            'tid': meta['tid'], 'cover': meta['cover'], 'copyright': meta['copyright'],
            'source': meta['source'], 'videos': uploaded, 'dtime': meta['dtime'],
            'dynamic': meta['dynamic'], 'no_reprint': meta['noReprint'],
            'is_only_self': 1 if vis == 'private' else 0,
        }
        vis_result = {'ok': True, 'applied': vis}
        if aid:
            try:
                r = up.edit_archive(cookie, aid, extras)
                vis_result = {'ok': bool(r.get('ok')),
                              'applied': vis if r.get('ok') else 'unknown',
                              'message': r.get('message') or ''}
                if not r.get('ok'):
                    log('可见范围/开关设置未生效：%s', r.get('message'))
                else:
                    log('稿件设置已更新（可见范围=%s，禁止转载=%s，充电=%s）',
                        vis, meta['noReprint'], meta['openElec'])
            except Exception as e:
                log('稿件设置更新异常：%s', e)
                vis_result = {'ok': False, 'applied': 'unknown', 'message': str(e)}

        # 合集：B 站的合集写入接口不可用（-400），只能「投稿时带 season_id + 事后校验」
        if season_id and aid:
            try:
                eps = up.season_episodes(cookie, season_id)
                if aid in eps:
                    log('已归入合集「%s」', season_title or season_id)
                else:
                    r = up.add_to_season(cookie, season_id, [aid])
                    if r.get('ok'):
                        log('合集写入接口通了：%s', r.get('via'))
                    time.sleep(2)
                    eps2 = up.season_episodes(cookie, season_id)
                    if aid in eps2:
                        log('复查：已归入合集「%s」', season_title or season_id)
                    else:
                        season_result = {'ok': False, 'id': season_id, 'title': season_title,
                                         'error': r.get('error') or '合集中没有出现该稿件'}
                        log('没能自动归入合集：%s（需要到创作中心手动加入）',
                            season_result['error'])
            except Exception as e:
                season_result = {'ok': False, 'id': season_id, 'title': season_title,
                                 'error': str(e)}
                log('合集校验异常：%s', e)

        for it in items:
            store.queue_update(it['id'], {
                'status': 'done', 'aid': aid, 'bvid': bvid,
                'submittedAt': time.time(), 'visibility': vis,
                'visibilityApplied': vis_result.get('applied'),
                'visibilityNote': vis_result.get('message') or vis_result.get('note') or '',
                'seasonId': season_result.get('id') or 0,
                'seasonTitle': season_result.get('title') or '',
                'seasonOk': bool(season_result.get('ok')),
                'seasonNote': season_result.get('error') or '',
            })
            store.set_upload_status(it['id'], {'phase': 'done', 'percent': 100,
                                               'note': '已投稿', 'aid': aid, 'bvid': bvid})
            done.append(it['id'])
        # 删除本地：默认「等审核通过再删」。投稿接口返回 0 只代表**已提交**，
        # 稿件随后进审核队列，可能被打回；打回就保留源文件，方便重投。
        review = {'ok': True, 'state': None, 'reason': '未等待审核'}
        if delete_local and aid:
            if settings.get('deleteWaitReview'):
                review = up.wait_archive_passed(
                    cookie, aid, log=log,
                    timeout_sec=int(settings.get('reviewWaitSeconds') or 900))
                log('审核结论：%s（state=%s）', review.get('reason'), review.get('state'))
            if review.get('ok'):
                for it in items:
                    try:
                        if it.get('file') and os.path.exists(it['file']):
                            os.remove(it['file'])
                            log('已删除本地成品：%s', os.path.basename(it['file']))
                            store.queue_update(it['id'], {'fileDeleted': True})
                    except Exception as e:
                        log('删本地成品失败：%s', e)
                if settings.get('deleteSessionAfterPublish'):
                    sess_ids = {it.get('sessionId') for it in items if it.get('sessionId')}
                    for sid in sess_ids:
                        try:
                            d = store.session_dir(sid)
                            if os.path.isdir(d):
                                shutil.rmtree(d, ignore_errors=True)
                                log('已删除会话目录：%s', sid)
                        except Exception as e:
                            log('删会话目录失败：%s', e)
            else:
                for it in items:
                    store.queue_update(it['id'], {
                        'keepReason': '未通过审核，本地文件已保留：%s' % review.get('reason')})
    except Exception as e:
        msg = str(e)
        log('投稿失败：%s', msg)
        for it in items:
            store.queue_update(it['id'], {'status': 'failed', 'error': msg,
                                          'failedAt': time.time()})
            store.set_upload_status(it['id'], {'phase': 'failed', 'note': msg})
            failed.append(it['id'])
    return {'done': done, 'failed': failed}
