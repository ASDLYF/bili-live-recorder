# -*- coding: utf-8 -*-
"""录制后处理：FLV → MP4（无损 remux）、抽帧做封面、导出弹幕、生成投稿队列。

ffmpeg 只处理本地文件，不联网（本机命令行建不了 HTTPS）。
"""
import json
import os
import shutil
import subprocess
import time

from . import danmaku as dm
from . import tsfix
from .util import ensure_dir, fmt_size, read_json, write_json

_FFMPEG_ENV = 'BILI_FFMPEG'
_FFPROBE_ENV = 'BILI_FFPROBE'

_HINTS = [
    # 新版 ffmpeg（支持 HEVC-in-FLV，4K 录制必需；7.1+）
    '/opt/homebrew/bin/ffmpeg',        # macOS Apple Silicon (Homebrew)
    '/opt/local/bin/ffmpeg',           # macOS MacPorts
    '/usr/local/bin/ffmpeg',           # macOS Intel (Homebrew) / 手动安装
    '/usr/bin/ffmpeg',                 # Linux 发行版
    r'C:\ffmpeg\bin\ffmpeg.exe',
    r'C:\Program Files\ffmpeg\bin\ffmpeg.exe',
]


def _which(name):
    return shutil.which(name)


def _resolve_exe(cand):
    """显式路径 / 环境变量传进来的可能是绝对路径，也可能只是个命令名。"""
    if not cand:
        return ''
    cand = str(cand).strip()
    if not cand:
        return ''
    if os.path.exists(cand):
        return cand
    return shutil.which(cand) or ''


def find_ffmpeg(explicit=''):
    for cand in (explicit, os.environ.get(_FFMPEG_ENV)):
        hit = _resolve_exe(cand)
        if hit:
            return hit
    hit = _which('ffmpeg')
    if hit:
        return hit
    for cand in _HINTS:
        if os.path.exists(cand):
            return cand
    return ''


def _sibling_probe(exe):
    """从 ffmpeg 路径推导同目录的 ffprobe（跨平台：ffmpeg.exe → ffprobe.exe）。"""
    if not exe:
        return ''
    d = os.path.dirname(os.path.abspath(exe))
    for name in ('ffprobe.exe', 'ffprobe'):
        cand = os.path.join(d, name)
        if os.path.exists(cand):
            return cand
    return ''


def find_ffprobe(explicit='', ffmpeg=''):
    for cand in (explicit, os.environ.get(_FFPROBE_ENV)):
        hit = _resolve_exe(cand)
        if hit:
            return hit
    hit = _which('ffprobe')
    if hit:
        return hit
    guess = _sibling_probe(ffmpeg)
    if guess:
        return guess
    for cand in _HINTS:
        p = _sibling_probe(cand)
        if p:
            return p
    return ''


def run(cmd, timeout=3600, log=None):
    if log:
        log('执行：%s', ' '.join('"%s"' % c if ' ' in str(c) else str(c) for c in cmd[:8]))
    p = subprocess.run(cmd, capture_output=True, timeout=timeout, text=True,
                       encoding='utf-8', errors='replace')
    if p.returncode != 0 and log:
        log('命令失败(%s)：%s', p.returncode, (p.stderr or '')[-1500:])
    return p


def probe(ffprobe, path):
    if not ffprobe:
        return {}
    p = run([ffprobe, '-v', 'error', '-show_entries',
             'stream=codec_type,codec_name,width,height,avg_frame_rate,bit_rate',
             '-show_entries', 'format=duration,size,bit_rate', '-of', 'json', path], timeout=120)
    try:
        data = json.loads(p.stdout or '{}')
    except Exception:
        return {}
    streams = data.get('streams') or []
    out = {'duration': float((data.get('format') or {}).get('duration') or 0),
           'size': int((data.get('format') or {}).get('size') or 0)}
    for s in streams:
        if s.get('codec_type') == 'video' and not out.get('width'):
            out['width'] = int(s.get('width') or 0)
            out['height'] = int(s.get('height') or 0)
            out['vcodec'] = s.get('codec_name') or ''
            fr = s.get('avg_frame_rate') or '0/1'
            try:
                num, den = fr.split('/')
                out['fps'] = round(float(num) / float(den or 1), 2)
            except Exception:
                out['fps'] = 0
        elif s.get('codec_type') == 'audio' and not out.get('acodec'):
            out['acodec'] = s.get('codec_name') or ''
    return out


_QUALITY_PRESET = {
    # quality: (长边像素, 峰值码率)
    '1080p': ('1080', '4000k'),
    '720p': ('720', '2200k'),
    '480p': ('480', '1000k'),
}


def remux(ffmpeg, src, dst, log=None, quality=''):
    """转封装成 mp4。

    默认 -c copy 无损搬运。quality 给 '1080p'/'720p'/'480p' 时走重编码降档 ——
    虎牙的 FLV 只有源画质一条路（rateArray 是播放器标签，URL 带 ratio 无效），
    想省盘只能在录完之后压。
    """
    tmp = dst + '.part.mp4'
    q = _QUALITY_PRESET.get(str(quality or ''))
    if q:
        target, br = q
        # 横屏压长边=宽，竖屏压长边=高，短边按 -2 自动对齐偶数
        vf = ("scale='if(gte(iw,ih),{0},-2)':'if(gte(iw,ih),-2,{0})'".format(target))
        if log:
            log('降档转码到 %s（峰值 %s）', quality, br)
        p = run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                 '-i', src, '-map', '0:v:0', '-map', '0:a:0?',
                 '-vf', vf, '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
                 '-maxrate', br, '-bufsize', str(int(br[:-1]) * 2) + 'k',
                 '-c:a', 'aac', '-b:a', '128k',
                 '-movflags', '+faststart', '-f', 'mp4', tmp],
                timeout=12 * 3600, log=log)
        if p.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 1024:
            os.replace(tmp, dst)
            return dst
        if log:
            log('降档失败，回落到无损转封装')
        try:
            os.path.exists(tmp) and os.unlink(tmp)
        except Exception:
            pass
    p = run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
             '-i', src, '-map', '0:v:0', '-map', '0:a:0?', '-c', 'copy',
             '-movflags', '+faststart', '-f', 'mp4', tmp], timeout=6 * 3600, log=log)
    if p.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 1024:
        # 有些流的首帧没有关键帧，重试时补一个空包解码时间基准
        p = run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                 '-fflags', '+genpts', '-i', src, '-map', '0:v:0', '-map', '0:a:0?',
                 '-c', 'copy', '-movflags', '+faststart', '-f', 'mp4', tmp],
                timeout=6 * 3600, log=log)
    if p.returncode != 0 or not os.path.exists(tmp):
        raise RuntimeError('转 mp4 失败：%s' % ((p.stderr or '')[-600:] or '未知错误'))
    os.replace(tmp, dst)
    return dst


def extract_cover(ffmpeg, video, dst, at_sec=20, log=None):
    """抽一帧做封面：优先取第 20 秒（避开开场黑屏），太短就取 1 秒。"""
    for at in (at_sec, 1.0, 0.0):
        p = run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error', '-ss', '%.2f' % at,
                 '-i', video, '-frames:v', '1', '-vf', 'scale=1146:-2',
                 '-q:v', '3', dst], timeout=180, log=None)
        if p.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 2048:
            return dst
    if log:
        log('抽封面失败，投稿时将由 B 站自动生成封面')
    return ''


def render_template(tpl, ctx):
    out = str(tpl or '')
    for k, v in (ctx or {}).items():
        out = out.replace('{%s}' % k, str(v))
    return out


def finalize_session(store, session, settings, log, ffmpeg='', ffprobe='',
                     only_indexes=None, keep_session_open=False):
    """把一次直播会话的处理完：分P 转 mp4、导出弹幕、做封面、进投稿队列。

    only_indexes：只处理这些分P（长直播滚动投稿用，不给就是全部）。
    keep_session_open：处理完不把会话标成 done（因为还在继续录）。
    """
    ffmpeg = find_ffmpeg(ffmpeg)
    ffprobe = find_ffprobe(ffprobe, ffmpeg)
    data = session.data
    done = list(data.get('finalized') or [])
    parts = [p for p in session.parts() if p.get('status') == 'closed']
    if only_indexes is not None:
        want = set(int(i) for i in only_indexes)
        parts = [p for p in parts if int(p['index']) in want]
    if not parts:
        session.update({'status': 'empty', 'finalizedAt': time.time()})
        return {'ok': True, 'items': [], 'note': '没有录到内容'}

    # 弹幕导出
    jsonl = os.path.join(session.dir, 'danmaku.jsonl')
    xml = os.path.join(session.dir, 'danmaku.xml')
    ass = os.path.join(session.dir, 'danmaku.ass')
    dan_count = 0
    if os.path.exists(jsonl):
        try:
            dan_count = dm.export_xml(jsonl, xml)
            first = probe(ffprobe, parts[0].get('file') or '')
            w = int(first.get('width') or 1920) or 1920
            h = int(first.get('height') or 1080) or 1080
            dm.export_ass(jsonl, ass, width=w, height=h)
            log('弹幕导出：%d 条 → xml/ass', dan_count)
        except Exception as e:
            log('弹幕导出失败：%s', e)

    date = time.strftime('%Y-%m-%d', time.localtime(data.get('recStart') or time.time()))
    uname = data.get('uname') or ('UP%s' % data.get('mid'))
    live_title = (data.get('title') or '').strip() or '直播'
    _plat = data.get('platform') or 'bili'
    room_url = ('https://www.huya.com/%s' % data.get('roomId')) if _plat == 'huya' \
        else ('https://live.bilibili.com/%s' % data.get('roomId'))
    multi = len(parts) > 1
    items = []

    for part in parts:
        src = part.get('file') or ''
        if not src or not os.path.exists(src):
            continue
        dst = os.path.join(session.dir, 'part-%03d.mp4' % int(part['index']))
        info = {}
        if os.path.exists(dst) and os.path.getsize(dst) > 1024:
            info = probe(ffprobe, dst)
        else:
            log('转封装 分P%d：%s', part['index'], fmt_size(os.path.getsize(src)))
            try:
                _q = ''
                if _plat == 'huya':
                    _q = str((settings or {}).get('huyaQuality') or '')
                remux(ffmpeg, src, dst, log=log, quality=_q)
                # 兜底：断流留下的时间戳跳变会让 B 站转码失败（state=-16），
                # 录到一半就断了的历史文件尤其常见。
                try:
                    fixed = tsfix.flatten_if_needed(ffmpeg, ffprobe, dst, log=log)
                    if fixed:
                        log('分P%d 修掉 %d 处时间戳跳变', part['index'], fixed)
                except Exception as e:
                    log('分P%d 时间戳修复跳过：%s', part['index'], e)
                info = probe(ffprobe, dst)
            except Exception as e:
                log('分P%d 转封装失败：%s', part['index'], e)
                continue
        duration = float(info.get('duration') or (part.get('durationMs') or 0) / 1000.0)
        if duration < 5:
            # 太短的分段不投稿，但也不能把 mp4 留在盘上变成「隐形占用」：
            # 这种文件既不在队列里、用户也没法从面板删，只能悄悄吃磁盘。
            log('分P%d 只有 %.1f 秒，跳过投稿并删除这个文件', part['index'], duration)
            for junk in (dst, src):
                try:
                    if junk and os.path.exists(junk):
                        os.remove(junk)
                        log('  已删除 %s', os.path.basename(junk))
                except Exception:
                    pass
            session.update_part(part['index'], {'status': 'skipped',
                                                'skipReason': 'too_short'})
            continue

        ctx = {'date': date, 'uname': uname, 'title': live_title, 'roomUrl': room_url,
               'duration': time.strftime('%H:%M:%S', time.gmtime(duration)),
               'part': 'P%d' % part['index'] if multi else '',
               'time': time.strftime('%H:%M', time.localtime(data.get('recStart') or time.time()))}
        title = render_template(settings.get('titleTemplate'), ctx).strip()
        if multi:
            title = '%s P%d' % (title, part['index'])
        desc = render_template(settings.get('descTemplate'), ctx).strip()
        tags = [render_template(t, ctx) for t in (settings.get('tags') or []) if t]
        cover = os.path.join(session.dir, 'cover.jpg')
        if settings.get('makeCover') and not (os.path.exists(cover) and os.path.getsize(cover) > 2048):
            extract_cover(ffmpeg, dst, cover, log=log)
        if not (os.path.exists(cover) and os.path.getsize(cover) > 2048):
            cover = ''

        item = {
            'sessionId': session.id,
            'mid': data.get('mid'),
            'uname': uname,
            'partIndex': int(part['index']),
            'file': dst,
            'bytes': os.path.getsize(dst),
            'durationSec': round(duration, 1),
            'width': info.get('width'), 'height': info.get('height'),
            'title': title[:80],
            'desc': desc[:1900],
            'tag': ','.join([t[:20] for t in tags])[:200],
            'tid': int(settings.get('tid') or 21),
            'cover': cover,
            'visibility': settings.get('visibility') or 'private',
            'dtime': int(settings.get('dtime') or 0),
            'copyright': int(settings.get('copyright') or 1),
            'source': settings.get('source') or '',
            'noReprint': int(settings.get('noReprint') or 0),
            'openElec': int(settings.get('openElec') or 0),
            'closeDanmaku': int(settings.get('closeDanmaku') or 0),
            'closeReply': int(settings.get('closeReply') or 0),
            'dynamic': settings.get('dynamic') or '',
            'sessionTitle': live_title,
            'recordedAt': data.get('recStart') or time.time(),
        }
        items.append(item)
        done.append(part['index'])

        if not settings.get('keepRawFlv'):
            try:
                os.remove(src)
                log('已删除原始 FLV：%s', os.path.basename(src))
            except Exception:
                pass

    if items:
        added = store.queue_add(items)
        log('已加入待发布队列：%d 条', len(added))
    else:
        added = []
    if keep_session_open:
        # 滚动投稿：会话还在录，只把进度记下来，状态和磁盘清理留给最终收尾
        session.update({'finalized': sorted(set(done)), 'danmaku': dan_count,
                        'cover': items[0]['cover'] if items else (data.get('cover') or '')})
    else:
        session.update({'status': 'done', 'finalizedAt': time.time(),
                        'finalized': sorted(set(done)), 'danmaku': dan_count,
                        'cover': items[0]['cover'] if items else ''})
    if settings.get('keepRawFlv') is False:
        raw = os.path.join(session.dir, 'raw')
        try:
            if os.path.isdir(raw) and not os.listdir(raw):
                os.rmdir(raw)
        except Exception:
            pass
    return {'ok': True, 'items': added, 'danmaku': dan_count}
