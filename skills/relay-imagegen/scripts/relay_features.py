"""Optional protocol, capability and local revision support for relay_imagegen."""
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import urllib.error
import urllib.request


def read_json(path, error):
    try:
        value = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise error("无法读取 JSON 文件。") from None
    if not isinstance(value, dict):
        raise error("JSON 必须是对象。")
    return value


def pillow(error):
    try:
        from PIL import Image
        return Image
    except ImportError:
        raise error("此本地图片功能需要 Pillow：python3 -m pip install Pillow") from None


def load_mask(path, references, api):
    if not references:
        raise api.SkillError("蒙版编辑需要第一张参考图。")
    ref = api.load_reference_images([path])[0]
    if ref["extension"] != "png":
        raise api.SkillError("蒙版必须是含透明通道的 PNG。")
    if 'width' in references[0] and (ref['width'], ref['height']) != (references[0]['width'], references[0]['height']):
        raise api.SkillError("蒙版必须与第一张参考图像素尺寸一致。")
    Image = pillow(api.SkillError)
    try:
        with Image.open(ref['path']) as im:
            im.load()
            if 'A' not in im.getbands() and 'transparency' not in im.info:
                raise api.SkillError("蒙版缺少透明通道；透明区域代表允许编辑的区域。")
            if im.convert('RGBA').getchannel('A').getextrema()[0] == 255:
                raise api.SkillError("蒙版完全不透明，没有可编辑区域。")
    except (OSError, ValueError, Image.DecompressionBombError):
        raise api.SkillError("蒙版无法完整解码。") from None
    return ref


CAP_ARRAYS = {'backends', 'sizes', 'quality', 'background', 'output_format', 'input_fidelity', 'edit_transports', 'moderation'}
CAP_BOOLS = {'edit', 'mask', 'stream', 'output_compression'}
CAP_INTS = {'max_n', 'max_reference_images'}


def validate_profile(profile, error):
    if not isinstance(profile, dict) or set(profile) - CAP_ARRAYS - CAP_BOOLS - CAP_INTS:
        raise error("能力配置含未知字段或不是对象；请参阅 advanced.md。")
    for name, value in profile.items():
        if name in CAP_ARRAYS and (not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value)):
            raise error(f"能力字段 {name} 必须是字符串数组。")
        if name in CAP_BOOLS and type(value) is not bool:
            raise error(f"能力字段 {name} 必须是布尔值。")
        if name in CAP_INTS and (type(value) is not int or value < 1):
            raise error(f"能力字段 {name} 必须是正整数。")
    return profile


def capability_check(args, config, references, api):
    profiles = config.get('capabilities', {})
    if not isinstance(profiles, dict):
        raise api.SkillError("capabilities 必须是以模型 ID 为键的对象。")
    profile = read_json(args.capabilities, api.SkillError) if args.capabilities else profiles.get(config['model'], {})
    validate_profile(profile, api.SkillError)
    conflicts = []
    values = {'backends': args.backend, 'sizes': args.size, 'quality': args.quality,
              'background': args.background, 'output_format': args.output_format,
              'input_fidelity': args.input_fidelity, 'moderation': args.moderation,
              'edit_transports': args.edit_transport if references and args.backend == 'images' else None}
    for key, value in values.items():
        if value is not None and key in profile and value not in profile[key]:
            conflicts.append(f"能力表未列出 {key}={value}")
    for key, requested in [('edit', args.command == 'edit'), ('mask', bool(args.mask or args.mask_url or args.mask_file_id)),
                           ('stream', args.stream), ('output_compression', args.output_compression is not None)]:
        if requested and profile.get(key) is False:
            conflicts.append(f"能力表标记不支持 {key}")
    if 'max_n' in profile and min(args.n, 10) > profile['max_n'] and args.request_mode != 'separate':
        conflicts.append("n 超过能力表的单次上限")
    if 'max_reference_images' in profile and len(references) > profile['max_reference_images']:
        conflicts.append("参考图数量超过能力表上限")
    if conflicts and args.strict_capabilities:
        raise api.SkillError("；".join(conflicts) + "；已按 --strict-capabilities 停止提交请求。")
    args.capability_warnings = [{'warning': text + '；能力表仅供参考，按用户参数提交，由服务端判断支持情况。'} for text in conflicts]
    return profile


def run_capabilities(args, api):
    path = api.config_path(args.config)
    config = api.read_config(path, use_env=False)
    model = args.model or api.read_config(path)['model']
    if args.action == 'set':
        if not args.file:
            raise api.SkillError("capabilities set 需要 --file 能力配置.json。")
        profile = validate_profile(read_json(args.file, api.SkillError), api.SkillError)
        profiles = config.setdefault('capabilities', {})
        if not isinstance(profiles, dict):
            raise api.SkillError("capabilities 配置必须是对象。")
        profiles[model] = profile
        api.write_config(path, config, replace=True)
    else:
        profiles = config.get('capabilities', {})
        if not isinstance(profiles, dict):
            raise api.SkillError('capabilities 配置必须是对象。')
        profile = validate_profile(profiles.get(model, {}), api.SkillError)
    return {'ok': True, 'model': model, 'capabilities': profile,
            'note': '用户维护的参考信息，不默认拦截请求；仅 --strict-capabilities 启用拦截。缺省字段表示未知。'}


def data_url(ref):
    return 'data:' + ref['mime'] + ';base64,' + base64.b64encode(ref['data']).decode('ascii')


def clean_metadata(value, secret):
    """Bound server metadata and redact the configured credential recursively."""
    if isinstance(value, str):
        return value.replace(secret, '[REDACTED]')[:32000] if secret else value[:32000]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {clean_metadata(str(k), secret)[:100]: clean_metadata(v, secret) for k, v in list(value.items())[:100]}
    if isinstance(value, list):
        return [clean_metadata(v, secret) for v in value[:100]]
    return None


def stream_request(request, timeout, partial, final, api, expected=1):
    """Consume SSE incrementally; save previews/finals before the next network read."""
    response_limit = api.byte_limit('RESPONSE', api.MAX_JSON_BYTES)
    payload = {'data': [], 'events': []}
    event_type = ''
    terminal = False
    total = 0
    try:
        with urllib.request.build_opener(api.NoRedirect()).open(request, timeout=timeout) as response:
            if 'text/event-stream' not in response.headers.get('Content-Type', ''):
                raise api.SkillError("请求了流式响应，但接口未返回 text/event-stream；未重试。")
            lines = []
            event_size = 0
            while True:
                raw = response.readline(response_limit + 1)
                total += len(raw)
                event_size += len(raw)
                if event_size > response_limit or total > response_limit * 4:
                    raise api.SkillError("流式响应超过本地大小限制。")
                if raw and raw.strip():
                    if raw.startswith(b'event:'):
                        event_type = raw[6:].strip().decode('utf-8')
                    if raw.startswith(b'data:'):
                        lines.append(raw[5:].lstrip().rstrip(b'\r\n'))
                    continue
                if lines:
                    data = b'\n'.join(lines)
                    lines = []
                    event_size = 0
                    if data == b'[DONE]':
                        break
                    try:
                        event = json.loads(data)
                    except (ValueError, UnicodeError):
                        raise api.SkillError("流式事件不是有效 JSON。") from None
                    if not isinstance(event, dict):
                        raise api.SkillError("流式事件必须是对象。")
                    kind = event.get('type') or event_type
                    event_type = ''
                    payload['events'].append({key: value for key, value in event.items() if key not in ('b64_json','partial_image_b64')})
                    if not isinstance(kind, str):
                        raise api.SkillError('流式事件 type 必须是字符串。')
                    if kind in ('error', 'response.failed', 'response.incomplete') or kind.endswith('.error'):
                        raise api.SkillError("流式接口报告失败；已保存的预览和结果保留，未重试。")
                    if kind.endswith('.partial_image'):
                        encoded = event.get('partial_image_b64') or event.get('b64_json')
                        if encoded:
                            partial(dict(event, b64_json=encoded))
                    elif kind in ('image_generation.completed', 'image_edit.completed'):
                        item = dict(event)
                        final(item)
                        payload['data'].append(item)
                        if 'usage' in event:
                            payload.setdefault('event_usage', []).append(event['usage'])
                        terminal = True
                        if len(payload['data']) >= expected:
                            break
                if not raw:
                    break
            if not terminal:
                raise api.SkillError("流式连接结束但未收到完成事件；预览不是最终图片，未重试。")
    except urllib.error.HTTPError as exc:
        code = exc.code
        try:
            details = json.loads(exc.read(64 * 1024))
        except (ValueError, UnicodeError, OSError):
            details = None
        finally:
            exc.close()
        detail = api.safe_error_details(details, request)
        suffix = f" 参数详情：{detail}" if detail else ""
        raise api.SkillError(f"流式接口返回 HTTP {code}。{suffix} 未自动重试。") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise api.SkillError("流式连接失败或中断；已保存结果保留，未自动重试。") from None
    return payload


def load_run_image(path, index, api):
    run = read_json(path, api.SkillError)
    files = run.get('files', [])
    if not isinstance(files, list) or not 1 <= index <= len(files):
        raise api.SkillError("选中图片编号超出运行记录中的已保存图片范围。")
    if not isinstance(files[index - 1], str) or not files[index - 1]:
        raise api.SkillError('运行记录中的图片路径无效。')
    image = Path(files[index - 1])
    if not image.is_file():
        raise api.SkillError("选中版本的原图文件已不存在。")
    return str(image.resolve()), run


@contextmanager
def session_lock(path, api):
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + '.lock')
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise api.SkillError("会话正被使用，或上次中断留下锁文件；确认进程停止后再移除 .lock。") from None
    os.close(fd)
    try:
        yield path
    finally:
        lock.unlink(missing_ok=True)


def session_read(path, api):
    session = read_json(path, api.SkillError) if path.exists() else {'version': 1, 'revisions': [], 'selected': None}
    if session.get('version') != 1 or not isinstance(session.get('revisions'), list):
        raise api.SkillError("会话文件格式无效。")
    selected = session.get('selected')
    if selected is not None and (type(selected) is not int or not 0 <= selected < len(session['revisions'])):
        raise api.SkillError('会话选中版本无效。')
    for revision in session['revisions']:
        if not isinstance(revision, dict) or not isinstance(revision.get('manifest'), str) or type(revision.get('image_index')) is not int or revision['image_index'] < 1:
            raise api.SkillError('会话版本记录无效。')
        parent = revision.get('parent')
        if parent is not None and (not isinstance(parent, dict) or not isinstance(parent.get('manifest'), str) or type(parent.get('image_index')) is not int):
            raise api.SkillError('会话父版本记录无效。')
    return session


def atomic_json(path, value):
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix='.relay-state-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def resolve_parent(args, session, api):
    parent = None
    if args.from_run:
        parent = {'manifest': str(Path(args.from_run).expanduser().resolve()), 'image_index': args.image_index}
    elif args.command == 'edit' and not (args.images or args.image_urls or args.image_file_ids or args.images_file) and session and session.get('selected') is not None:
        parent = dict(session['revisions'][session['selected']])
    if parent:
        image, run = load_run_image(parent['manifest'], parent['image_index'], api)
        if args.command == 'edit':
            args.images = [image] + (args.images or [])
        args.parent = {k: parent[k] for k in ('manifest', 'image_index')}


def run_with_session(args, api, generate):
    if not args.session:
        resolve_parent(args, None, api)
        return generate(args)
    with session_lock(args.session, api) as path:
        session = session_read(path, api)
        resolve_parent(args, session, api)
        result = generate(args)
        start = len(session['revisions'])
        for index, image in enumerate(result.get('files', []), 1):
            session['revisions'].append({'manifest': result['manifest'], 'image_index': index,
                                         'image': image, 'parent': getattr(args, 'parent', None)})
        if len(session['revisions']) > start:
            session['selected'] = start
        try:
            atomic_json(path, session)
        except OSError:
            result['ok'] = False
            result['errors'].append({'error': '图片已保存，但会话更新失败；可用 --from-run 继续。'})
        result['session'] = str(path)
        result['selected_revision'] = session['selected'] + 1 if session['selected'] is not None else None
        api.save_run_manifest(result)
        return result


def run_history(args, api):
    with session_lock(args.session, api) as path:
        session = session_read(path, api)
        if args.action == 'import':
            if not args.from_run:
                raise api.SkillError('history import 需要 --from-run。')
            manifest = str(Path(args.from_run).expanduser().resolve())
            run = read_json(manifest, api.SkillError)
            files = run.get('files', [])
            if not isinstance(files, list) or not files:
                raise api.SkillError('运行记录没有已保存图片。')
            imported = []
            for i in range(1, len(files) + 1):
                image, _ = load_run_image(manifest, i, api)
                imported.append({'manifest': manifest, 'image_index': i, 'image': image, 'parent': run.get('parent')})
            first = None
            for revision in imported:
                existing = next((i for i, r in enumerate(session['revisions']) if r['manifest'] == manifest and r['image_index'] == revision['image_index']), None)
                if existing is None:
                    existing = len(session['revisions'])
                    session['revisions'].append(revision)
                if first is None:
                    first = existing
            session['selected'] = first
        elif args.action == 'select':
            if args.revision is None or not 1 <= args.revision <= len(session['revisions']):
                raise api.SkillError("请提供有效的 --revision 编号（从 1 开始）。")
            session['selected'] = args.revision - 1
        elif args.action == 'undo':
            selected = session.get('selected')
            parent = session['revisions'][selected].get('parent') if selected is not None else None
            if not parent:
                raise api.SkillError("当前版本没有父图；可用 history select 选择其他版本。")
            index = next((i for i, rev in enumerate(session['revisions']) if
                          rev['manifest'] == parent['manifest'] and rev['image_index'] == parent['image_index']), None)
            if index is None:
                raise api.SkillError("父图不在该会话内，可用 --from-run 指向父运行记录。")
            session['selected'] = index
        if args.action != 'list':
            atomic_json(path, session)
        return {'ok': True, 'session': str(path), 'selected_revision': session['selected'] + 1 if session['selected'] is not None else None,
                'revisions': [dict(r, revision=i) for i, r in enumerate(session['revisions'], 1)]}
