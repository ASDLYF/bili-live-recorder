# -*- coding: utf-8 -*-
"""B 站投稿：upos 分片上传 + 稿件提交（协议按 biliup 的实测实现对齐）。

  1. GET  /preupload?r=upos&profile=ugcupos/bup         → endpoint / upos_uri / auth / biz_id / chunk_size
  2. POST {endpoint}/{upos_uri}?uploads&output=json      → upload_id（不先建对象，PUT 会 NoSuchUpload）
  3. PUT  {endpoint}/{upos_uri}?uploadId&chunk&partNumber&start&end…  → 逐片
  4. POST {endpoint}/{upos_uri}?name&uploadId&biz_id&output=json  + body {"parts":[…]}  → 合并
  5. POST /x/vu/web/cover/up                              → 封面（16:10）
  6. POST /x/vu/web/add?csrf=<bili_jct>                   → 提交（is_only_self=1 即「仅自己可见」）
"""
import base64
import json
import math
import os
import time
import urllib.parse

from . import httpc
from .util import fmt_size

MEMBER = 'https://member.bilibili.com'
REFER = 'https://member.bilibili.com/york/videoup'
PROFILE = 'ugcupos/bup'

# 常用分区（B站投稿必选一个 tid）
TID_LIST = [
    (21, '日常'), (160, '生活'), (138, '搞笑'), (250, '出行'), (251, '三农'),
    (4, '游戏'), (17, '单机游戏'), (171, '电子竞技'), (172, '手机游戏'), (65, '网络游戏'),
    (3, '音乐'), (28, '原创音乐'), (31, '翻唱'), (30, 'VOCALOID·UTAU'), (59, '演奏'),
    (1, '动画'), (24, 'MAD·AMV'), (25, 'MMD·3D'), (47, '短片·手书·配音'), (257, '虚拟UP主'),
    (129, '舞蹈'), (20, '宅舞'), (154, '舞蹈综合'),
    (5, '娱乐'), (71, '综艺'), (137, '明星综合'),
    (36, '知识'), (201, '科学科普'), (124, '社科·法律·心理'), (228, '人文历史'),
    (188, '科技'), (95, '数码'), (230, '软件应用'),
    (181, '影视'), (182, '影视杂谈'), (183, '影视剪辑'),
    (211, '美食'), (76, '美食制作'), (212, '美食侦探'),
    (217, '动物圈'), (218, '喵星人'), (219, '汪星人'),
    (234, '运动'), (235, '篮球'), (164, '健身'),
    (223, '汽车'), (176, '汽车生活'),
    (155, '时尚'), (157, '美妆护肤'), (158, '穿搭'),
    (202, '资讯'), (203, '热点'),
]


class UploadError(RuntimeError):
    pass


def _csrf(cookie):
    return httpc.cookie_field(cookie, 'bili_jct')


def _member_headers(cookie):
    return {'Referer': REFER, 'Origin': MEMBER,
            'Cookie': cookie, 'Accept': 'application/json, text/plain, */*'}


# ---------------------------------------------------------------- 上传

def preupload(cookie, filename, size):
    params = dict(r='upos', profile=PROFILE, ssl=0, version='2.8.12', build='2081200',
                  name=filename, size=int(size))
    url = '%s/preupload?%s' % (MEMBER, urllib.parse.urlencode(params))
    last = ''
    for attempt in range(5):
        status, raw = httpc.request('GET', url, headers=_member_headers(cookie),
                                    cookie=cookie, timeout=40, retries=0)
        if status == 200:
            try:
                data = json.loads(raw.decode('utf-8', 'replace'))
            except Exception:
                raise UploadError('preupload 返回异常（HTTP %s）：%s' % (status, raw[:200]))
            if data.get('chunk_size'):
                return data
            raise UploadError('preupload 被拒：%s' % (data.get('message') or data))
        last = 'HTTP %s' % status
        time.sleep(2.0 * (attempt + 1))   # 403/406 多为短时风控
    raise UploadError('preupload 失败（%s），可能是风控限流，稍后再试' % last)


def _base_url(pre):
    endpoint = pre.get('endpoint') or ''
    if not endpoint:
        eps = pre.get('endpoints') or []
        endpoint = eps[0] if eps else ''
    if not endpoint.startswith('http'):
        endpoint = 'https:' + endpoint
    upos = (pre.get('upos_uri') or '').replace('upos://', '')
    return endpoint.rstrip('/') + '/' + upos


def upload_file(cookie, path, progress=None, cli_log=None):
    """分片上传；返回可提交的 filename（B 站要的是「不含目录与扩展名」的名字）。"""
    size = os.path.getsize(path)
    name = os.path.basename(path)
    pre = preupload(cookie, name, size)
    url = _base_url(pre)
    auth = pre.get('auth') or ''
    biz_id = str(pre.get('biz_id') or '')
    chunk_size = int(pre.get('chunk_size') or 10485760)
    headers = {'X-Upos-Auth': auth, 'Referer': 'https://member.bilibili.com/'}
    if cli_log:
        cli_log('上传 %s（%s，分片 %s）→ %s', name, fmt_size(size), fmt_size(chunk_size), url)

    # 1) 申请上传，拿 upload_id
    status, raw = httpc.request('POST', url + '?uploads&output=json', headers=headers,
                                cookie=cookie, timeout=60, retries=1)
    try:
        init = json.loads(raw.decode('utf-8', 'replace'))
    except Exception:
        raise UploadError('初始化上传失败（HTTP %s）：%s' % (status, raw[:200]))
    upload_id = str(init.get('upload_id') or '')
    if not upload_id:
        raise UploadError('初始化上传没拿到 upload_id：%s' % json.dumps(init, ensure_ascii=False)[:300])
    if cli_log:
        cli_log('upload_id=%s', upload_id)

    chunks = max(1, int(math.ceil(float(size) / chunk_size)))
    sent = 0
    with open(path, 'rb') as f:
        for index in range(chunks):
            blob = f.read(chunk_size)
            if not blob:
                break
            params = {
                'uploadId': upload_id, 'chunks': chunks, 'total': size,
                'chunk': index, 'size': chunk_size, 'partNumber': index + 1,
                'start': index * chunk_size, 'end': index * chunk_size + chunk_size,
            }
            one = url + '?' + urllib.parse.urlencode(params)
            ok = False
            last = ''
            for attempt in range(4):
                try:
                    st, rw = httpc.request('PUT', one, headers=headers, data=blob,
                                           cookie=cookie, timeout=600, retries=0)
                    if st == 200:
                        ok = True
                        break
                    last = 'HTTP %s %s' % (st, rw[:120])
                except Exception as e:
                    last = str(e)
                time.sleep(2.0 * (attempt + 1))
            if not ok:
                raise UploadError('第 %d/%d 片上传失败：%s' % (index + 1, chunks, last))
            sent += len(blob)
            if progress:
                progress(sent, size)

    # 2) 合并分片
    parts = [{'partNumber': i + 1, 'eTag': 'etag'} for i in range(chunks)]
    fin_params = {'name': name, 'uploadId': upload_id, 'biz_id': biz_id,
                  'output': 'json', 'profile': PROFILE}
    fin_url = url + '?' + urllib.parse.urlencode(fin_params)
    body = json.dumps({'parts': parts}).encode('utf-8')
    last = ''
    for attempt in range(4):
        try:
            st, rw = httpc.request('POST', fin_url,
                                   headers={**headers, 'Content-Type': 'application/json'},
                                   data=body, cookie=cookie, timeout=300, retries=0)
            try:
                fin = json.loads(rw.decode('utf-8', 'replace'))
            except Exception:
                fin = {}
            if fin.get('OK') == 1:
                if cli_log:
                    cli_log('上传完成 %s（%d 片）', name, chunks)
                base = os.path.splitext(os.path.basename(url))[0]
                return {'filename': base, 'size': size, 'uploadId': upload_id,
                        'raw': fin}
            last = 'HTTP %s %s' % (st, rw[:160])
        except Exception as e:
            last = str(e)
        time.sleep(3.0 * (attempt + 1))
    raise UploadError('合并分片失败：%s' % last)


def upload_cover(cookie, path):
    """封面：B 站要求 16:10 左右，这里直接把已经裁好的 jpg 传上去。"""
    if not path or not os.path.exists(path):
        return ''
    with open(path, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode()
    body = urllib.parse.urlencode({
        'cover': 'data:image/jpeg;base64,' + b64,
        'csrf': _csrf(cookie),
    })
    url = '%s/x/vu/web/cover/up' % MEMBER
    status, raw = httpc.request('POST', url,
                                headers={**_member_headers(cookie),
                                         'Content-Type': 'application/x-www-form-urlencoded'},
                                data=body, cookie=cookie, timeout=180, retries=1)
    try:
        data = json.loads(raw.decode('utf-8', 'replace'))
    except Exception:
        raise UploadError('封面上传返回异常（HTTP %s）：%s' % (status, raw[:200]))
    if data.get('data') is None:
        raise UploadError('封面上传失败：%s' % (data.get('message') or data.get('code')))
    return (data.get('data') or {}).get('url') or ''


# ---------------------------------------------------------------- 合集(season)

CREATIVE = '%s/x2/creative/web' % MEMBER


def list_seasons(cookie):
    """当前账号的合集列表（只读接口，可用）。"""
    url = '%s/seasons?pn=1&ps=50&order=mtime' % CREATIVE
    status, raw = httpc.request('GET', url, headers=_member_headers(cookie),
                                cookie=cookie, timeout=40, retries=1)
    try:
        data = json.loads(raw.decode('utf-8', 'replace'))
    except Exception:
        return {'ok': False, 'error': '合集列表返回异常（HTTP %s）' % status, 'seasons': []}
    if data.get('code') != 0:
        return {'ok': False, 'error': data.get('message') or data.get('msg'),
                'seasons': []}
    out = []
    for it in ((data.get('data') or {}).get('seasons') or []):
        s = it.get('season') or {}
        out.append({'id': s.get('id'), 'title': s.get('title') or '',
                    'epCount': s.get('epCount') or s.get('ep_num') or 0,
                    'state': s.get('state')})
    return {'ok': True, 'seasons': out}


def season_episodes(cookie, season_id):
    """合集里现有的 aid 列表（用来校验投稿有没有真的进去）。"""
    url = '%s/season/section?season_id=%s' % (CREATIVE, season_id)
    status, raw = httpc.request('GET', url, headers=_member_headers(cookie),
                                cookie=cookie, timeout=40, retries=1)
    try:
        data = json.loads(raw.decode('utf-8', 'replace'))
    except Exception:
        return []
    eps = ((data.get('data') or {}).get('episodes') or [])
    return [e.get('aid') for e in eps if e.get('aid')]


def add_to_season(cookie, season_id, aids):
    """把稿件加进合集。

    说明（实测）：B 站的合集写入接口（season/section/add、section/edit、
    season/edit…）全部返回 -400 请求错误，公开渠道没有可用参数组合；
    只有「投稿时带 season_id」会被接受。所以这里先试一次，失败就如实返回，
    由上层做校验 + 提示手动加入。
    """
    csrf = _csrf(cookie)
    body = json.dumps({'season_id': int(season_id), 'section_id': 0,
                       'aids': [int(a) for a in aids if a]}).encode('utf-8')
    for path in ('/season/section/add', '/season/section/edit'):
        url = '%s%s?csrf=%s' % (CREATIVE, path, csrf)
        try:
            status, raw = httpc.request(
                'POST', url,
                headers={**_member_headers(cookie), 'Content-Type': 'application/json'},
                data=body, cookie=cookie, timeout=60, retries=0)
        except Exception as e:
            return {'ok': False, 'error': str(e)}
        try:
            data = json.loads(raw.decode('utf-8', 'replace'))
        except Exception:
            continue
        if data.get('code') == 0:
            return {'ok': True, 'via': path}
    return {'ok': False, 'error': 'B 站合集写入接口不可用（-400），需要手动加入'}


# ---------------------------------------------------------------- 提交

def submit(cookie, meta):
    """提交稿件。

    meta.visibility: private → is_only_self=1（仅自己可见）
                     scheduled → dtime（必须距现在 > 4 小时，否则 B 站会忽略）
                     public → 都不带
    meta.seasonId: 目标合集 id（B 站投稿接口接受该字段，但实测不一定生效，
                   调用方要用 season_episodes() 校验）
    """
    videos = meta.get('videos') or []
    if not videos:
        raise UploadError('没有可提交的视频')
    vis = meta.get('visibility') or 'private'
    visibility = vis if vis in ('private', 'public', 'scheduled') else 'private'
    dtime = int(meta.get('dtime') or 0)
    if visibility == 'scheduled':
        if dtime - int(time.time()) <= 4 * 3600:
            raise UploadError('定时发布时间必须晚于现在 4 小时以上（B 站限制），当前设置无效')
    else:
        dtime = 0

    payload = {
        'copyright': int(meta.get('copyright') or 1),
        'source': meta.get('source') or '',
        'tid': int(meta.get('tid') or 21),
        'cover': meta.get('cover') or '',
        'title': (meta.get('title') or '')[:80],
        'desc': (meta.get('desc') or '')[:2000],
        'tag': (meta.get('tag') or '').strip() or '录播',
        'videos': [{'filename': v['filename'],
                    'title': (v.get('title') or meta.get('title') or '')[:80],
                    'desc': ''} for v in videos],
        'dtime': dtime,
        'dynamic': meta.get('dynamic') or '',
    }
    if meta.get('seasonId'):
        payload['season_id'] = int(meta['seasonId'])
    # 其余开关（仅自己可见 / 禁止转载 / 充电 / 关弹幕评论）走提交后的编辑接口：
    # 直接塞进提交里会被 B 站判 21001 参数错误（实测），编辑接口则接受。

    # 提交前先探一下风控（B 站网页端也这么做）
    try:
        httpc.request('GET', '%s/x/geetest/pre/add' % MEMBER,
                      headers=_member_headers(cookie), cookie=cookie, timeout=15, retries=0)
    except Exception:
        pass

    url = '%s/x/vu/web/add?csrf=%s' % (MEMBER, _csrf(cookie))
    status, raw = httpc.request('POST', url,
                                headers={**_member_headers(cookie),
                                         'Content-Type': 'application/json'},
                                data=json.dumps(payload).encode('utf-8'),
                                cookie=cookie, timeout=180, retries=0)
    try:
        data = json.loads(raw.decode('utf-8', 'replace'))
    except Exception:
        raise UploadError('投稿返回异常（HTTP %s）：%s' % (status, raw[:300]))
    if data.get('code') != 0:
        raise UploadError('投稿失败（code=%s）：%s' % (
            data.get('code'), data.get('message') or data.get('msg') or ''))
    d = data.get('data') or {}
    return {'aid': d.get('aid'), 'bvid': d.get('bvid'), 'raw': d,
            'visibilityApplied': visibility}

def archive_state(cookie, aid):
    """查稿件的审核状态：state==0 表示已通过并开放浏览；负数是审核中/打回。

    B 站状态码（常用）：0 开放浏览 / -6 审核中 / -30 审核中 / -2 被打回 /
    -100 审核不通过 / -40 被删除 / 100 用户删除
    """
    for status in ('is_pubing,pubed,not_pubed', ''):
        url = '%s/x/web/archives?pn=1&ps=50%s' % (
            MEMBER, ('&status=' + status) if status else '')
        try:
            st, raw = httpc.request('GET', url, headers=_member_headers(cookie),
                                    cookie=cookie, timeout=40, retries=1)
            data = json.loads(raw.decode('utf-8', 'replace'))
        except Exception:
            continue
        for it in ((data.get('data') or {}).get('arc_audits') or []):
            a = it.get('Archive') or {}
            if int(a.get('aid') or 0) == int(aid):
                return {'ok': True, 'state': int(a.get('state') if a.get('state') is not None else 999),
                        'title': a.get('title') or '',
                        'isOnlySelf': int(a.get('is_only_self') or 0)}
    return {'ok': False, 'state': None}


def wait_archive_passed(cookie, aid, log=None, timeout_sec=600, interval=30):
    """等稿件通过审核（state==0）。返回 {ok, state, reason}。

    注意：投稿接口返回 code=0 只代表**已提交**，稿件随后会进入审核队列；
    审核不通过的稿件会被打回（-2 / -100），此时绝不能把本地源文件删掉，
    否则要重投就只能重录。
    """
    deadline = time.time() + max(60, timeout_sec)
    last = None
    while time.time() < deadline:
        r = archive_state(cookie, aid)
        state = r.get('state')
        if state == 0:
            return {'ok': True, 'state': 0, 'reason': '已通过审核'}
        if state in (-2, -100, -40, 100):
            return {'ok': False, 'state': state,
                    'reason': '稿件被驳回/删除（state=%s）' % state}
        last = state
        if log:
            log('等待审核：aid=%s 当前 state=%s，%d 秒后再查', aid, state, interval)
        time.sleep(interval)
    return {'ok': False, 'state': last, 'reason': '等待审核超时（state=%s）' % last}


def archive_info(cookie, aid):
    url = '%s/x/vu/web/edit?aid=%s' % (MEMBER, aid)
    status, raw = httpc.request('GET', url, headers=_member_headers(cookie),
                                cookie=cookie, timeout=40, retries=0)
    try:
        return json.loads(raw.decode('utf-8', 'replace'))
    except Exception:
        return {'code': status, 'message': raw[:200].decode('utf-8', 'replace')}


def edit_archive(cookie, aid, fields):
    """改稿件设置（可见范围 / 禁止转载 / 充电 / 关弹幕评论 / 标题简介等）。

    B 站网页端的编辑接口接受比提交更多的字段，所以「仅自己可见」这类设置
    走「先提交、再编辑」更稳（提交时带某些字段会 21001 参数错误）。
    """
    payload = {
        'aid': int(aid),
        'copyright': int(fields.get('copyright') or 1),
        'source': fields.get('source') or '',
        'tid': int(fields.get('tid') or 21),
        'cover': fields.get('cover') or '',
        'title': (fields.get('title') or '')[:80],
        'desc': (fields.get('desc') or '')[:2000],
        'tag': (fields.get('tag') or '录播'),
        'videos': fields.get('videos') or [],
        'dtime': int(fields.get('dtime') or 0),
        'is_only_self': int(fields.get('is_only_self') or 0),
        'no_reprint': int(fields.get('no_reprint') or 0),
        'dynamic': fields.get('dynamic') or '',
    }
    # 注意：不要带 charging_pay。带「充电专属」会与转载/首映等互斥，
    # B 站直接拒整个编辑请求，连带「仅自己可见」也设不上（实测 22:35 踩过）。
    url = '%s/x/vu/web/edit?csrf=%s' % (MEMBER, _csrf(cookie))
    status, raw = httpc.request('POST', url,
                                headers={**_member_headers(cookie),
                                         'Content-Type': 'application/json'},
                                data=json.dumps(payload).encode('utf-8'),
                                cookie=cookie, timeout=120, retries=0)
    try:
        data = json.loads(raw.decode('utf-8', 'replace'))
    except Exception:
        return {'ok': False, 'code': status, 'message': raw[:200].decode('utf-8', 'replace')}
    return {'ok': data.get('code') == 0, 'code': data.get('code'),
            'message': data.get('message') or data.get('msg') or ''}


def set_private(cookie, aid, payload_extra=None):
    fields = dict(payload_extra or {})
    fields['is_only_self'] = 1
    return edit_archive(cookie, aid, fields)


def delete_archive(cookie, aid):
    csrf = _csrf(cookie)
    headers_form = {**_member_headers(cookie),
                    'Content-Type': 'application/x-www-form-urlencoded'}
    headers_json = {**_member_headers(cookie), 'Content-Type': 'application/json'}
    for path, as_json in (
        ('/x/vu/web/delete', False),
        ('/x/vu/web/archive/delete', False),
        ('/x/vupre/web/archive/delete', False),
        ('/x/vu/web/archive/del', True),
    ):
        url = '%s%s?csrf=%s' % (MEMBER, path, csrf)
        body = json.dumps({'aid': int(aid)}).encode() if as_json else ('aid=%s' % aid).encode()
        status, raw = httpc.request('POST', url,
                                    headers=headers_json if as_json else headers_form,
                                    data=body, cookie=cookie, timeout=60, retries=0)
        text = raw[:200].decode('utf-8', 'replace')
        if status == 200 and text.lstrip().startswith('{'):
            try:
                data = json.loads(text)
            except Exception:
                continue
            if data.get('code') == 0:
                return {'ok': True, 'deleted': True, 'via': path}
    # 兜底：至少让它不可见
    fell = set_private(cookie, aid)
    return {'ok': False, 'deleted': False,
            'message': '没有可用的删除接口；已尝试设为仅自己可见',
            'private': fell}
