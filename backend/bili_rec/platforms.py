"""直播平台适配层：B站 / 虎牙。

上层（daemon / capture / cli / plugin）只认统一字段，平台差异全部收在这里：

    detect(text)                        把用户输入（链接 / 房间号 / UID）解析成 {platform, roomId, uid}
    room_info(platform, room_id)        直播间快照：{roomId, uid, live, title, cover, uname, face, online, liveTime}
    stream(platform, room_id, qn, codec) 可直接拉的 FLV：{url, qn, qnDesc, format, protocol, codec, headers}

B站走 live.py 的 playurl + pick_stream；虎牙走 mp.huya.com 的 profileRoom 缓存接口。
"""

import re

from . import httpc
from . import live

UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36')

BILI = 'bili'
HUYA = 'huya'

LABELS = {BILI: 'B站', HUYA: '虎牙'}

# 虎牙码率档位（rateArray 的 iBitRate → 显示名）
HUYA_QUALITY = [(8000, '蓝光8M'), (4000, '蓝光4M'), (2000, '超清'), (1000, '高清'), (500, '流畅')]


# ---------------------------------------------------------------- 输入解析

def detect(text):
    """把用户输入解析成 {platform, roomId, uid, label}。

    支持：
      919191 / 11342412                 纯数字（默认按 B站 房间号理解，除非给了平台前缀）
      huya:919191 / 虎牙 919191         显式平台
      https://www.huya.com/919191       虎牙房间页
      https://live.bilibili.com/22603245 B站直播间
      https://space.bilibili.com/6930731 B站 UP 主主页
      bili:6930731                      显式 B站 UID
    """
    s = (text or '').strip()
    if not s:
        raise ValueError('输入为空')

    low = s.lower()

    # 显式前缀：huya:919191 / bili:6930731
    m = re.match(r'^(huya|虎牙|bili|bilibili|b站)\s*[:：]?\s*(\d+)$', s)
    if m:
        plat = HUYA if m.group(1) in ('huya', '虎牙') else BILI
        rid = m.group(2)
        return {'platform': plat, 'roomId': int(rid) if plat == HUYA else 0,
                'uid': 0 if plat == HUYA else int(rid), 'label': LABELS[plat]}

    if 'huya.com' in low:
        m = re.search(r'huya\.com/(\d{4,12})', low)
        if not m:
            m = re.search(r'huya\.com/(?:u/)?(\d{4,12})', low)
        if not m:
            raise ValueError('从链接里找不到虎牙房间号：%s' % s)
        return {'platform': HUYA, 'roomId': int(m.group(1)), 'uid': 0, 'label': LABELS[HUYA]}

    if 'bilibili.com' in low:
        m = re.search(r'live\.bilibili\.com/(?:h5/)?(\d+)', low)
        if m:
            return {'platform': BILI, 'roomId': int(m.group(1)), 'uid': 0, 'label': LABELS[BILI]}
        m = re.search(r'space\.bilibili\.com/(\d+)', low)
        if m:
            return {'platform': BILI, 'roomId': 0, 'uid': int(m.group(1)), 'label': LABELS[BILI]}
        raise ValueError('B站链接里找不到房间号或 UID：%s' % s)

    m = re.match(r'^(\d{4,12})$', s)
    if m:
        # 纯数字默认当 B站 房间号；用户想看虎牙就写 huya:919191 或贴链接
        return {'platform': BILI, 'roomId': int(m.group(1)), 'uid': 0, 'label': LABELS[BILI]}

    raise ValueError('认不出这是哪个平台的输入：%s' % s)


# ---------------------------------------------------------------- B站

def _bili_room_info(room_id, cookie=''):
    d = live.room_info(room_id, cookie=cookie)
    uname, face = '', ''
    try:
        mi = live.master_info(int(d.get('uid') or 0), cookie=cookie)
        uname = mi.get('uname') or ''
        face = mi.get('face') or ''
    except Exception:
        pass
    return {
        'roomId': int(d.get('roomId') or room_id),
        'uid': int(d.get('uid') or 0),
        'live': bool(d.get('live')),
        'title': d.get('title') or '',
        'cover': d.get('cover') or '',
        'uname': uname,
        'face': face,
        'online': int(d.get('online') or 0),
        'liveTime': int(d.get('liveTime') or 0),
    }


def _bili_stream(room_id, qn=0, codec='', cookie=''):
    prefer = codec or 'avc'
    if qn <= 0:
        streams = live.playurl(room_id, qn=20000, cookie=cookie, codec='0,1')
        best = live.pick_stream(streams, 'flv', '')
    else:
        try:
            streams = live.playurl(
                room_id, qn=qn, cookie=cookie,
                codec='0' if prefer == 'avc' else ('1' if prefer == 'hevc' else '0,1'))
        except live.LiveError:
            streams = live.playurl(room_id, qn=qn, cookie=cookie, codec='0,1')
        best = live.pick_stream(streams, 'flv', prefer)
    return {
        'url': best['url'], 'qn': best['qn'],
        'qnDesc': live.QN_DESC.get(best['qn'], ''),
        'format': best['format'], 'protocol': best['protocol'], 'codec': best['codec'],
        'headers': {'Referer': 'https://live.bilibili.com/'},
    }


# ---------------------------------------------------------------- 虎牙

_HUYA_API = 'https://mp.huya.com/cache.php?m=Live&do=profileRoom&roomid=%s'


def _huya_profile(room_id):
    url = _HUYA_API % room_id
    try:
        r = httpc.get_json(url, headers={
            'Referer': 'https://www.huya.com/%s' % room_id,
            'User-Agent': UA,
            'Accept': 'application/json, text/plain, */*',
        })
    except Exception as e:
        raise live.LiveError('虎牙房间 %s 信息获取失败：%s' % (room_id, e))
    if int(r.get('status') or 0) != 200 or not r.get('data'):
        raise live.LiveError('虎牙房间 %s 不存在或已关闭' % room_id)
    return r['data']


def _huya_room_info(room_id, cookie=''):
    d = _huya_profile(room_id)
    ld = d.get('liveData') or {}
    pf = d.get('profileInfo') or {}
    status = str(d.get('liveStatus') or '').upper()
    return {
        'roomId': int(ld.get('profileRoom') or room_id),
        'uid': int(pf.get('uid') or ld.get('yyid') or 0),
        'live': status == 'ON',
        'title': (ld.get('introduction') or d.get('welcomeText') or '').strip(),
        'cover': ld.get('screenshot') or '',
        'uname': (pf.get('nick') or ld.get('nick') or ('虎牙%s' % room_id)).strip(),
        'face': (pf.get('avatar180') or ld.get('avatar180') or ''),
        'online': int(d.get('liveData', {}).get('totalCount') or 0),
        'liveTime': 0,
    }


def _huya_stream(room_id, qn=0, codec='', cookie=''):
    d = _huya_profile(room_id)
    if str(d.get('liveStatus') or '').upper() != 'ON':
        raise live.LiveError('live_status=0（虎牙未开播）')
    stream = d.get('stream') or {}
    flv = stream.get('flv') or {}
    lines = flv.get('multiLine') or []
    if not lines:
        raise live.LiveError('虎牙没给出可用的 FLV 线路（可能是付费/加密直播）')
    # 线路优先级：webPriorityRate 越大越靠前；同档优先 AL
    lines = sorted(lines, key=lambda x: (int(x.get('webPriorityRate') or 0), x.get('cdnType') != 'AL'),
                   reverse=True)
    url = lines[0].get('url') or ''
    if not url:
        raise live.LiveError('虎牙线路地址为空')
    if url.startswith('//'):
        url = 'https:' + url

    rate = _huya_pick_rate(flv.get('rateArray') or [], qn)
    if rate:
        url += ('&' if '?' in url else '?') + 'ratio=%d' % rate

    return {
        'url': url, 'qn': qn or 0,
        'qnDesc': _huya_hint(stream, qn),
        'format': 'flv', 'protocol': 'http_stream', 'codec': 'avc',
        'headers': {'Referer': 'https://www.huya.com/%s' % room_id, 'User-Agent': UA},
    }


def _huya_pick_rate(rate_array, qn):
    """qn 是统一的码率上限（kbps），0/空 = 原画（不加 ratio）。"""
    if not rate_array or not qn:
        return 0
    avail = sorted({int(x.get('iBitRate') or 0) for x in rate_array if x.get('iBitRate')}, reverse=True)
    for b in avail:
        if b <= int(qn):
            return b
    return avail[-1] if avail else 0


def _huya_hint(stream, qn):
    ra = (stream.get('flv') or {}).get('rateArray') or []
    if not ra:
        return '原画'
    top = max(int(x.get('iBitRate') or 0) for x in ra)
    if not qn or qn >= top:
        return '原画(%d)' % top
    return '≤%d' % qn


# ---------------------------------------------------------------- 统一入口

def room_info(platform, room_id, cookie=''):
    platform = platform or BILI
    if platform == HUYA:
        return _huya_room_info(room_id, cookie)
    return _bili_room_info(room_id, cookie)


def stream(platform, room_id, qn=0, codec='', cookie=''):
    platform = platform or BILI
    if platform == HUYA:
        return _huya_stream(room_id, qn, codec, cookie)
    return _bili_stream(room_id, qn, codec, cookie)


def resolve(text, cookie=''):
    """输入 → 完整的主播条目（ups.json 里存的那一坨）。"""
    info = detect(text)
    plat, rid, uid = info['platform'], info['roomId'], info['uid']
    if plat == HUYA:
        if not rid:
            raise ValueError('虎牙需要房间号')
        snap = _huya_room_info(rid)
        return {'mid': str(snap['roomId']), 'platform': HUYA, 'roomId': snap['roomId'],
                'uid': snap['uid'], 'uname': snap['uname'], 'face': snap['face'],
                'title': snap['title'], 'live': snap['live'], 'enabled': True}
    # B站：给了 UID 就先查房间
    if not rid and uid:
        r = live.room_of_mid(uid, cookie=cookie)
        if r and r.get('roomId'):
            rid = int(r['roomId'])
    if not rid:
        raise ValueError('B站需要直播间号或 UP 主 UID')
    snap = _bili_room_info(rid, cookie)
    return {'mid': str(snap['uid'] or rid), 'platform': BILI, 'roomId': snap['roomId'],
            'uid': snap['uid'], 'uname': snap['uname'], 'face': snap['face'],
            'title': snap['title'], 'live': snap['live'], 'enabled': True}


def label(platform):
    return LABELS.get(platform or BILI, platform or LABELS[BILI])
