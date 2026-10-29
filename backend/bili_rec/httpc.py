# -*- coding: utf-8 -*-
"""极简 HTTP 客户端（标准库）。不走系统代理 —— 本机 Clash 对国内站点反而更慢。"""
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36')

# macOS 上 python.org 装的 Python 不会自动装 CA 包（要手动跑 Install
# Certificates.command），于是 ssl.create_default_context() 里一个根证书都没有，
# 所有 HTTPS 都报 CERTIFICATE_VERIFY_FAILED。这里按「显式环境变量 → certifi →
# 系统 CA 文件」的顺序兜底，保证换台 mac 也能直接跑。
_CA_FILES = (
    '/etc/ssl/cert.pem',                        # macOS 系统 CA（LibreSSL 格式，CPython 认）
    '/opt/homebrew/etc/openssl@3/cert.pem',     # Homebrew (Apple Silicon)
    '/usr/local/etc/openssl@3/cert.pem',        # Homebrew (Intel)
    '/opt/homebrew/etc/ca-certificates/cert.pem',
    '/etc/ssl/certs/ca-certificates.crt',       # Debian/Ubuntu
    '/etc/pki/tls/certs/ca-bundle.crt',         # Fedora/RHEL
)


def _build_context():
    paths = ssl.get_default_verify_paths()
    for cand in (paths.cafile, os.environ.get('SSL_CERT_FILE')):
        if cand and os.path.exists(cand):
            try:
                return ssl.create_default_context(cafile=cand)
            except Exception:
                pass
    try:
        import certifi  # 有就用（python.org 安装器通常带）
        caf = certifi.where()
        if caf and os.path.exists(caf):
            return ssl.create_default_context(cafile=caf)
    except Exception:
        pass
    ctx = ssl.create_default_context()
    try:
        if ctx.cert_store_stats().get('x509_ca'):
            return ctx
    except Exception:
        pass
    for cand in _CA_FILES:
        if os.path.exists(cand):
            try:
                return ssl.create_default_context(cafile=cand)
            except Exception:
                continue
    if os.environ.get('BILI_REC_INSECURE') == '1':
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


_CTX = _build_context()


def _opener():
    """模块级复用；BILI_REC_PROXY=1 时才走系统代理。"""
    global _OPENER
    try:
        return _OPENER
    except NameError:
        pass
    handler = (urllib.request.ProxyHandler()
               if os.environ.get('BILI_REC_PROXY') == '1'
               else urllib.request.ProxyHandler({}))
    _OPENER = urllib.request.build_opener(handler,
                                          urllib.request.HTTPSHandler(context=_CTX))
    return _OPENER


def load_cookie(path):
    """读整行 cookie 文件：多行/注释会被规整成一行 `a=b; c=d`。"""
    if not path or not os.path.exists(path):
        return ''
    try:
        with open(path, encoding='utf-8') as f:
            raw = f.read()
    except Exception:
        return ''
    parts = []
    for line in raw.replace('\r', '\n').split('\n'):
        line = line.split('#')[0].strip()
        # 很多人是从 DevTools 里连标头名一起复制的：Cookie: a=b; c=d
        low = line.lower()
        for prefix in ('set-cookie:', 'cookie:'):
            if low.startswith(prefix):
                line = line[len(prefix):].strip()
                break
        line = line.strip().strip('"').strip("'").strip()
        if line and '=' in line:
            parts.append(line)
    return '; '.join(parts)


def cookie_field(cookie, key):
    for item in (cookie or '').split(';'):
        item = item.strip()
        if item.startswith(key + '='):
            return item[len(key) + 1:]
    return ''


def abs_url(url):
    if url.startswith('//'):
        return 'https:' + url
    if url.startswith('http://') or url.startswith('https://'):
        return url
    return 'https://' + url


def request(method, url, headers=None, data=None, timeout=30, retries=2,
            backoff=1.5, cookie=None, json_body=None):
    """返回 (status, body_bytes)。HTTP 4xx/5xx 不抛，交给调用方看 body。"""
    hdrs = {'User-Agent': UA}
    if cookie:
        hdrs['Cookie'] = cookie
    if headers:
        hdrs.update(headers)
    body = data
    if json_body is not None:
        body = json.dumps(json_body).encode('utf-8')
        hdrs.setdefault('Content-Type', 'application/json')
    elif isinstance(body, str):
        body = body.encode('utf-8')

    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with _opener().open(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            raw = e.read()
            if e.code in (412, 429) and attempt < retries:
                last = 'HTTP %d' % e.code
                time.sleep(backoff * (attempt + 1))
                continue
            return e.code, raw
        except Exception as e:
            last = '%s: %s' % (type(e).__name__, e)
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
                continue
    raise RuntimeError('请求失败 %s %s (%s)' % (method, url.split('?')[0], last))


def get_json(url, headers=None, cookie=None, timeout=25, retries=2):
    status, raw = request('GET', url, headers=headers, cookie=cookie,
                          timeout=timeout, retries=retries)
    try:
        return json.loads(raw.decode('utf-8', 'replace'))
    except Exception:
        raise RuntimeError('返回不是 JSON（HTTP %s）：%s' % (status, raw[:200]))


def post_json(url, payload, headers=None, cookie=None, timeout=40, retries=2):
    status, raw = request('POST', url, headers=headers, cookie=cookie,
                          json_body=payload, timeout=timeout, retries=retries)
    try:
        return json.loads(raw.decode('utf-8', 'replace'))
    except Exception:
        raise RuntimeError('返回不是 JSON（HTTP %s）：%s' % (status, raw[:300]))


class Stream:
    """流式 GET：`read(n)` 拿字节，`close()` 关连接。"""

    def __init__(self, url, headers=None, cookie=None, timeout=25):
        hdrs = {'User-Agent': UA, 'Accept': '*/*',
                'Accept-Encoding': 'identity', 'Connection': 'keep-alive'}
        if cookie:
            hdrs['Cookie'] = cookie
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, headers=hdrs, method='GET')
        self._resp = _opener().open(req, timeout=timeout)
        self.status = self._resp.status
        self.headers = dict(self._resp.headers)

    def read(self, n=65536):
        return self._resp.read(n)

    def close(self):
        try:
            self._resp.close()
        except Exception:
            pass


def qs(params):
    return urllib.parse.urlencode(params)
