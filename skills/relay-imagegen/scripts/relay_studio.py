"""Loopback-only, token-protected local version viewer and mask painter."""
import base64
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4


def make_server(session_path, port, api):
    extra = api.features()
    Image = extra.pillow(api.SkillError)
    session_path = Path(session_path).expanduser().resolve()
    if not session_path.is_file():
        raise api.SkillError("studio 需要已有的 --session 文件。")
    extra.session_read(session_path, api)
    token = secrets.token_urlsafe(32)
    page = (Path(__file__).resolve().parent.parent / 'assets' / 'studio.html').read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log access URLs containing the local token.

        def send(self, status, data, kind='application/json'):
            if isinstance(data, dict):
                data = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            host = self.headers.get('Host', '')
            origin = self.headers.get('Origin')
            expected = f'127.0.0.1:{self.server.server_port}'
            if host != expected or (origin is not None and origin != 'http://' + expected) or not secrets.compare_digest(query.get('token', [''])[0], token):
                self.send(403, {'ok': False, 'error': '本地访问凭据无效。'})
                return None
            return parsed.path, query

        def revision(self, index):
            session = extra.session_read(session_path, api)
            if not 1 <= index <= len(session['revisions']):
                raise api.SkillError('版本编号无效。')
            revision = session['revisions'][index-1]
            image, _ = extra.load_run_image(revision['manifest'], revision['image_index'], api)
            return revision, Path(image)

        def do_GET(self):
            auth = self.authorized()
            if auth is None:
                return
            path, query = auth
            try:
                if path == '/':
                    self.send(200, page, 'text/html; charset=utf-8')
                elif path == '/api/state':
                    session = extra.session_read(session_path, api)
                    self.send(200, {'ok': True, 'session': str(session_path), **session})
                elif path == '/api/image':
                    _, image = self.revision(int(query.get('revision', ['0'])[0]))
                    data = image.read_bytes()
                    ext = api.image_format(data)
                    self.send(200, data, 'image/jpeg' if ext == 'jpg' else 'image/' + ext)
                else:
                    self.send(404, {'ok': False})
            except (api.SkillError, ValueError, OSError):
                self.send(400, {'ok': False, 'error': '无法读取该版本。'})

        def do_POST(self):
            auth = self.authorized()
            if auth is None:
                return
            path, _ = auth
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= api.byte_limit('IMAGE', api.MAX_IMAGE_BYTES) * 2:
                    raise api.SkillError('请求大小无效。')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise api.SkillError('请求必须为对象。')
                if path == '/api/select':
                    result = extra.run_history(SimpleNamespace(session=str(session_path), action='select', revision=int(body['revision'])), api)
                    self.send(200, result)
                elif path == '/api/mask':
                    revision, source = self.revision(int(body['revision']))
                    data = base64.b64decode(body['png'], validate=True)
                    if len(data) > api.byte_limit('IMAGE', api.MAX_IMAGE_BYTES):
                        raise api.SkillError('蒙版超过大小限制。')
                    with Image.open(io.BytesIO(data)) as mask, Image.open(source) as original:
                        mask.load()
                        if mask.format != 'PNG' or mask.size != original.size or 'A' not in mask.getbands():
                            raise api.SkillError('蒙版格式、尺寸或透明通道无效。')
                        if mask.getchannel('A').getextrema()[0] == 255:
                            raise api.SkillError('请先涂抹需要修改的区域。')
                        directory = session_path.parent / 'masks'
                        directory.mkdir(exist_ok=True)
                        output = directory / f'mask-{uuid4().hex[:12]}.png'
                        mask.save(output)
                    self.send(200, {'ok': True, 'mask': str(output), 'from_run': revision['manifest'],
                                    'image_index': revision['image_index'], 'image': str(source)})
                else:
                    self.send(404, {'ok': False})
            except (api.SkillError, ValueError, KeyError, TypeError, OSError, Image.DecompressionBombError) as exc:
                self.send(400, {'ok': False, 'error': str(exc) if isinstance(exc, api.SkillError) else '无法处理请求。'})

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server, f'http://127.0.0.1:{server.server_port}/?token={token}'


def run(args, api):
    if not 0 <= args.port <= 65535:
        raise api.SkillError('port 必须为 0–65535。')
    server, url = make_server(args.session, args.port, api)
    print(json.dumps({'ok': True, 'studio_url': url, 'note': '仅本机可访问。Ctrl+C 停止；界面不提交生图请求。'}, ensure_ascii=False), file=sys.stderr, flush=True)
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return {'ok': True, 'message': '本地工作台已关闭。'}
