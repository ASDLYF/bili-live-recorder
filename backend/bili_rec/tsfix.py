"""时间戳跳变修复。

直播断流时 B 站下发的 tag 时间戳会突然往前跳一大截（实测 185s / 333s / 507s），
而 FLV 里对应的那一帧 duration 就成了「好几分钟」。ffmpeg -c copy 原样搬进 mp4 后，
时长统计照样对，但内部 PTS 有三处空档 —— B 站转码直接报
「该视频内部存在时间戳跳变」并失败（草稿 state=-16）。

capture.py 已经在录制时重锚（TS_JUMP_MS），这里是兜底：处理历史文件、
以及 mkv/flv 拼接等异常场景。

做法：
  * 视频轨：bsf setts 把超长帧 duration 归位 + PTS 整体前移（无损 copy）
  * 音频轨：filter asetpts=N 按样本序号重排（重编码，一小时的音频十几秒）
两条轨道各自从 0 起连续，音画误差稳定在 1 帧以内。
"""

import json
import os
import subprocess
from collections import Counter


def _packets(ffprobe, path, sel):
    """取某一路的 (pts, duration) 序列。"""
    p = subprocess.run(
        [ffprobe, '-v', 'error', '-select_streams', sel,
         '-show_entries', 'packet=pts,duration', '-of', 'csv=p=0', path],
        capture_output=True, text=True, encoding='utf-8', errors='replace')
    out = []
    for line in (p.stdout or '').splitlines():
        cols = line.strip().split(',')
        if len(cols) < 2:
            continue
        try:
            out.append((int(cols[0]), int(cols[1])))
        except ValueError:
            continue
    return out


def find_jumps(pkts, factor=3, floor=1000):
    """挑出 duration 明显超长（断流）的帧，返回 (跳变列表, 正常帧长)。

    正常帧长取众数：视频 1080p 竖屏是 640（@16000 时基 / 25fps），
    音频是 1024（@48000 时基 / 1024 采样一帧）。
    """
    durs = [d for _, d in pkts if d > 0]
    if not durs:
        return [], 1
    normal = Counter(durs).most_common(1)[0][0]
    jumps = [(p, d) for p, d in pkts
             if d > normal * factor and (d - normal) > floor]
    return jumps, normal


def _has_jumps(ffprobe, path):
    vj, _ = find_jumps(_packets(ffprobe, path, 'v:0'))
    return len(vj)


def flatten(ffmpeg, ffprobe, src, dst, log=None, audio_bitrate='160k'):
    """把 src 的时间轴压平后写到 dst（不覆盖 src）。返回跳变处数。"""
    vp = _packets(ffprobe, src, 'v:0')
    vj, vn = find_jumps(vp)
    if not vj:
        return 0

    # 累积偏移：每跨过一个超长帧，后面的 PTS 就多减 (duration - 正常帧长)
    cums, acc = [], 0
    for p, d in vj:
        acc += d - vn
        cums.append((p + d, acc))

    # 正序生成条件（大阈值在外层）：小阈值放外层会先命中，只减第一段 —— 踩过这个坑
    expr = 'TS'
    for thr, off in cums:
        expr = 'if(gt(TS,%d),TS-%d,%s)' % (thr - 1, off, expr)

    de = 'DURATION'
    for d in sorted({d for _, d in vj}, reverse=True):
        de = 'if(eq(DURATION,%d),%d,%s)' % (d, vn, de)

    vbsf = "setts=ts='%s':duration='%s'" % (expr, de)

    tmp = dst + '.tsfix.mp4'
    cmd = [ffmpeg, '-y', '-hide_banner', '-loglevel', 'error', '-i', src,
           '-map', '0:v:0', '-map', '0:a:0?', '-c:v', 'copy', '-bsf:v', vbsf,
           '-af', 'asetpts=N', '-c:a', 'aac', '-b:a', audio_bitrate,
           '-movflags', '+faststart', '-f', 'mp4', tmp]
    if log:
        log('时间戳跳变 %d 处，压平时间轴（视频 copy + 音频重排）', len(vj))
    p = subprocess.run(cmd, capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    if p.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 1024:
        # 兜底：视频也重编码，慢但一定能出
        if log:
            log('bsf 方案失败，回退到视频重编码：%s', (p.stderr or '')[-300:])
        cmd2 = [ffmpeg, '-y', '-hide_banner', '-loglevel', 'error', '-i', src,
                '-map', '0:v:0', '-map', '0:a:0?',
                '-vf', 'setpts=N/25/TB', '-af', 'asetpts=N',
                '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
                '-c:a', 'aac', '-b:a', audio_bitrate,
                '-movflags', '+faststart', '-f', 'mp4', tmp]
        p = subprocess.run(cmd2, capture_output=True, text=True,
                           encoding='utf-8', errors='replace')
        if p.returncode != 0:
            raise RuntimeError('时间戳修复失败：%s' % ((p.stderr or '')[-500:] or '未知错误'))
    os.replace(tmp, dst)
    return len(vj)


def flatten_if_needed(ffmpeg, ffprobe, path, log=None):
    """原地修复：有跳变才动手，返回修掉的处数（0 表示干净）。"""
    n = _has_jumps(ffprobe, path)
    if not n:
        return 0
    tmp = path + '.flat.mp4'
    got = flatten(ffmpeg, ffprobe, path, tmp, log=log)
    if not got:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return 0
    os.replace(tmp, path)
    return got


def summary(ffprobe, path):
    """给面板/日志用的体检报告。"""
    vp = _packets(ffprobe, path, 'v:0')
    vj, vn = find_jumps(vp)
    return {'videoJumps': len(vj),
            'jumpPoints': [{'at': round(p / 16000.0, 2),
                            'stallSec': round((d - vn) / 16000.0, 2)} for p, d in vj],
            'normalFrameTs': vn}


if __name__ == '__main__':
    import sys
    src = sys.argv[1]
    ff = sys.argv[2] if len(sys.argv) > 2 else '/opt/homebrew/bin/ffmpeg'
    fp = sys.argv[3] if len(sys.argv) > 3 else '/opt/homebrew/bin/ffprobe'
    print(json.dumps(summary(fp, src), ensure_ascii=False, indent=1))
