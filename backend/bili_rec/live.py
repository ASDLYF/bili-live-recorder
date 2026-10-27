# -*- coding: utf-8 -*-
"""B 站直播接口：房间信息 / 开播状态 / 取流地址 / 弹幕服务器。"""
import json
import time
import urllib.parse

from . import httpc

LIVE_API = 'https://api.live.bilibili.com'
REFERER = 'https://live.bilibili.com/'

# B 站清晰度语义（qn）：150 高清 / 250 超清 / 400 蓝光 / 10000 原画 / 20000 4K
QN_DESC = {30000: '杜比', 25000: '原画真彩', 20000: '4K', 15000: '2K',
           10000: '原画', 400: '蓝光', 250: '超清', 150: '高清', 80: '流畅'}


class LiveError(RuntimeError):
    pass


def _headers(room_id=None):
    ref = (REFERER + str(room_id)) if room_id else REFERER
    return {'Referer': ref, 'Origin': 'https://live.bilibili.com',
            'Accept': 'application/json, text/plain, */*'}


def room_of_mid(mid, cookie=''):
    """mid → 直播间信息（含 liveStatus）。UP 主没开播/没有直播间时返回 None。"""
    url = '%s/room/v1/Room/getRoomInfoOld?mid=%s' % (LIVE_API, mid)
    try:
        r = httpc.get_json(url, headers=_headers(), cookie=cookie)
    except Exception as e:
        raise LiveError('查房间失败：%s' % e)
    if r.get('code') != 0:
        return None
    d = r.get('data') or {}
    if not d.get('roomid'):
        return None
    return {
        'roomId': int(d.get('roomid')),
        'shortId': int(d.get('short_id') or 0),
        'live': int(d.get('liveStatus') or 0) == 1,
        'title': d.get('title') or '',
        'cover': d.get('cover') or '',
        'online': int(d.get('online') or 0),
        'liveTime': _to_ts(d.get('live_time')),
        'url': d.get('url') or '',
        'uid': mid,
    }


def room_info(room_id, cookie=''):
    """直播间实时信息（比 getRoomInfoOld 更全，用于轮询开播）。"""
    url = '%s/room/v1/Room/get_info?room_id=%s' % (LIVE_API, room_id)
    r = httpc.get_json(url, headers=_headers(room_id), cookie=cookie)
    if r.get('code') != 0:
        raise LiveError('直播间 %s 信息获取失败：%s' % (room_id, r.get('message') or r.get('msg')))
    d = r.get('data') or {}
    return {
        'roomId': int(d.get('room_id') or room_id),
        'uid': int(d.get('uid') or 0),
        'live': int(d.get('live_status') or 0) == 1,
        'title': _strip_tags(d.get('title') or ''),
        'cover': d.get('user_cover') or d.get('keyframe') or '',
        'online': int(d.get('online') or 0),
        'liveTime': _to_ts(d.get('live_time')),
        'areaName': d.get('area_name') or '',
        'parentAreaName': d.get('parent_area_name') or '',
    }


def _to_ts(value):
    """live_time 可能是秒级时间戳，也可能是 'YYYY-mm-dd HH:MM:SS' 字符串。"""
    if value is None or value == '':
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
        try:
            return int(time.mktime(time.strptime(str(value), fmt)))
        except Exception:
            continue
    return 0


def _strip_tags(text):
    out = []
    depth = 0
    for ch in text or '':
        if ch == '<':
            depth += 1
        elif ch == '>':
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return ''.join(out).strip()


def playurl(room_id, qn=10000, cookie='', codec='0'):
    """返回可用的流列表。优先 http-flv（能自己控时间戳与断流重连）。

    codec: '0' 只要 AVC / '1' 只要 HEVC / '0,1' 都行。
    默认只要 AVC —— B 站的 HEVC 走的是 Enhanced-FLV（codecID 12），
    ffmpeg 7.0 的 flv 解封装读不出尺寸，转 mp4 会失败。
    """
    params = dict(room_id=room_id, protocol='0,1', format='0,1,2', codec=codec,
                  qn=qn, platform='web', ptype=8, dolby=5, panorama=1)
    url = '%s/xlive/web-room/v2/index/getRoomPlayInfo?%s' % (LIVE_API, urllib.parse.urlencode(params))
    r = httpc.get_json(url, headers=_headers(room_id), cookie=cookie)
    data = r.get('data') or {}
    if r.get('code') != 0:
        raise LiveError('取流出错：%s' % (r.get('message') or r.get('msg')))
    info = data.get('playurl_info')
    if not info:
        # live_status=0 或未开播时没有 playurl_info
        raise LiveError('直播间未开播或取流失败（live_status=%s）' % data.get('live_status'))
    p = info['playurl']
    streams = []
    for st in p.get('stream') or []:
        proto = st.get('protocol_name')
        for fmt in st.get('format') or []:
            for codec in fmt.get('codec') or []:
                urls = codec.get('url_info') or []
                if not urls:
                    continue
                u = urls[0]
                full = httpc.abs_url(u.get('host', '') + codec.get('base_url', '') +
                                     '?' + (u.get('extra') or ''))
                streams.append({
                    'protocol': proto,
                    'format': fmt.get('format_name'),
                    'codec': codec.get('codec_name'),
                    'qn': int(codec.get('current_qn') or 0),
                    'acceptQn': codec.get('accept_qn') or [],
                    'url': full,
                    'host': u.get('host') or '',
                    'ttl': u.get('stream_ttl') or 0,
                })
    if not streams:
        raise LiveError('没有可用的流地址')
    return streams


def pick_stream(streams, prefer='flv', prefer_codec='avc'):
    """挑一条流：优先 flv（时间戳可控），其次 ts；同格式内按画质/编码偏好排。"""
    def score(s):
        want_flv = 1 if (s['format'] == 'flv' and s['protocol'] == 'http_stream') else 0
        want_ts = 1 if (s['format'] == 'ts' and s['protocol'] == 'http_hls') else 0
        pref = (3 * want_flv + want_ts) if prefer == 'flv' else (3 * want_ts + want_flv)
        codec_ok = 1 if str(s['codec']).lower() in (prefer_codec, '') else 0
        return (pref, codec_ok, s['qn'])

    return max(streams, key=score)


def danmu_info(room_id, cookie=''):
    """弹幕服务器 + token。这个接口要 WBI 签名，不签名会返回 -352（风控）。"""
    params = {'id': room_id, 'type': 0}
    url = '%s/xlive/web-room/v1/index/getDanmuInfo?%s' % (
        LIVE_API, wbi_signed(params, cookie))
    r = httpc.get_json(url, headers=_headers(room_id), cookie=cookie)
    if r.get('code') != 0:
        # 退一步：不带签名再试（有的账号/灰度能过）
        url2 = '%s/xlive/web-room/v1/index/getDanmuInfo?id=%s&type=0' % (LIVE_API, room_id)
        r2 = httpc.get_json(url2, headers=_headers(room_id), cookie=cookie)
        if r2.get('code') == 0:
            r = r2
        else:
            raise LiveError('取弹幕服务器失败：%s（%s）' % (
                r.get('message') or r.get('msg'), r.get('code')))
    d = r.get('data') or {}
    hosts = d.get('host_list') or []
    out = []
    for h in hosts:
        out.append({'host': h.get('host'), 'wssPort': int(h.get('wss_port') or 443),
                    'wsPort': int(h.get('ws_port') or 2244)})
    return {'token': d.get('token') or '', 'hosts': out}


# ---------------------------------------------------------------- WBI 签名

MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

_WBI_CACHE = {'keys': None, 'at': 0.0}


def _wbi_keys(cookie='', force=False):
    """从 nav 拿 img_key/sub_key，缓存一小时。"""
    if not force and _WBI_CACHE['keys'] and time.time() - _WBI_CACHE['at'] < 3600:
        return _WBI_CACHE['keys']
    r = httpc.get_json('https://api.bilibili.com/x/web-interface/nav', cookie=cookie,
                       headers={'Referer': 'https://www.bilibili.com/'})
    wbi = ((r.get('data') or {}).get('wbi_img')) or {}
    img = (wbi.get('img_url') or '').rsplit('/', 1)[-1].split('.')[0]
    sub = (wbi.get('sub_url') or '').rsplit('/', 1)[-1].split('.')[0]
    if not img or not sub:
        raise LiveError('拿不到 WBI key')
    _WBI_CACHE['keys'] = (img, sub)
    _WBI_CACHE['at'] = time.time()
    return img, sub


def wbi_signed(params, cookie=''):
    """按 B 站规则给参数加 wts + w_rid。"""
    import hashlib
    img, sub = _wbi_keys(cookie)
    mixin = ''.join((img + sub)[i] for i in MIXIN_TAB)[:32]
    p = dict(params)
    p['wts'] = int(time.time())
    p = dict(sorted(p.items()))
    p = {k: ''.join(c for c in str(v) if c not in "!'()*") for k, v in p.items()}
    query = urllib.parse.urlencode(p)
    p['w_rid'] = hashlib.md5((query + mixin).encode()).hexdigest()
    return urllib.parse.urlencode(p)


def master_info(mid, cookie=''):
    """UP 主昵称/头像（直播接口，不用 wbi）。"""
    try:
        r = httpc.get_json('%s/live_user/v1/Master/info?uid=%s' % (LIVE_API, mid),
                           headers=_headers(), cookie=cookie)
        info = (r.get('data') or {}).get('info') or {}
        return {'uname': info.get('uname') or '', 'face': info.get('face') or ''}
    except Exception:
        return {'uname': '', 'face': ''}


def nav(cookie=''):
    """检查 cookie 是否有效：返回 (ok, 账号信息, 原始返回)。"""
    try:
        r = httpc.get_json('https://api.bilibili.com/x/web-interface/nav', cookie=cookie)
    except Exception as e:
        return False, {'error': str(e)}, {}
    d = r.get('data') or {}
    # 注意：不能写 `r.get('code') or -1` —— code 成功时是 0，会被当成假值变成 -1
    ok = int(r.get('code', -1)) == 0 and bool(d.get('isLogin'))
    return ok, {'mid': d.get('mid'), 'uname': d.get('uname'),
                'vip': d.get('vipStatus'), 'level': d.get('level_info', {}).get('current_level'),
                'code': r.get('code'), 'message': r.get('message')}, r
