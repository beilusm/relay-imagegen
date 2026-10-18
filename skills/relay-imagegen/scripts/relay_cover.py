"""Deterministic local cover composition. Pillow is imported only when used."""
import hashlib
import math
from pathlib import Path
from uuid import uuid4


def number(value, name, api, minimum=0, maximum=1):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise api.SkillError(f"{name} 必须在 {minimum}–{maximum} 范围内。")
    return value


def resolve_file(value, root, api):
    if not isinstance(value, str) or not value:
        raise api.SkillError("素材和字体路径必须是非空字符串。")
    path = Path(value).expanduser()
    path = (root / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_file():
        raise api.SkillError("素材或字体文件不存在。")
    return path


def font_path(spec, root, api):
    if spec:
        return resolve_file(spec, root, api)
    candidates = ['/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc',
                  '/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc',
                  '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc',
                  '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
                  '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                  '/usr/share/fonts/TTF/DejaVuSans.ttf']
    for value in candidates:
        if Path(value).is_file():
            return Path(value)
    raise api.SkillError("未找到默认字体，请在配方 font 字段指定字体文件。")


def validate_glyphs(font, text, api):
    missing = bytes(font.getmask(chr(0x10ffff)))
    for char in set(text):
        if char.isspace():
            continue
        mask = bytes(font.getmask(char))
        if mask == missing:
            raise api.SkillError(f"字体缺少字符 {char!r}，请更换覆盖该文字的字体。")


def wrap_text(text, draw, font, width, stroke):
    lines = []
    for paragraph in text.split('\n'):
        line = ''
        for char in paragraph:
            candidate = line + char
            box = draw.textbbox((0, 0), candidate, font=font, stroke_width=stroke)
            if line and box[2] - box[0] > width:
                lines.append(line)
                line = char
            else:
                line = candidate
        lines.append(line)
    return '\n'.join(lines)


def run(args, api):
    extra = api.features()
    Image, ImageDraw, ImageFont, ImageOps = extra.pillow(api.SkillError)
    spec_path = Path(args.spec).expanduser().resolve()
    spec = extra.read_json(spec_path, api.SkillError)
    root = spec_path.parent
    allowed = {'background', 'sizes', 'formats', 'font', 'safe_margin', 'crop_focus', 'texts', 'overlays', 'jpeg_quality'}
    if set(spec) - allowed:
        raise api.SkillError("封面配方含未知字段，请参阅 covers.md。")
    bg_path = resolve_file(spec.get('background'), root, api)
    sizes = spec.get('sizes', ['1280x720'])
    formats = spec.get('formats', ['png', 'jpeg'])
    if not isinstance(sizes, list) or not 1 <= len(sizes) <= 10 or any(not isinstance(v, str) for v in sizes) or len(set(sizes)) != len(sizes):
        raise api.SkillError("sizes 需要 1–10 个不重复的 WIDTHxHEIGHT 字符串。")
    dimensions = []
    import re
    for size in sizes:
        if not isinstance(size, str) or not re.fullmatch(r'[1-9]\d*x[1-9]\d*', size):
            raise api.SkillError("尺寸必须使用 WIDTHxHEIGHT。")
        w, h = map(int, size.split('x'))
        if max(w, h) > 8192 or w * h > 32_000_000:
            raise api.SkillError("本地导出最大边为 8192，最大总像素为 3200 万。")
        dimensions.append((w, h))
    if sum(w*h for w, h in dimensions) > 64_000_000:
        raise api.SkillError('一次导出的总像素不超过 6400 万，请拆分配方。')
    if not isinstance(formats, list) or not formats or any(f not in ('png', 'jpeg', 'webp') for f in formats) or len(set(formats)) != len(formats):
        raise api.SkillError("formats 必须是 png/jpeg/webp 的不重复数组。")
    margin = number(spec.get('safe_margin', .04), 'safe_margin', api, 0, .4)
    focus = spec.get('crop_focus', [.5, .5])
    if not isinstance(focus, list) or len(focus) != 2:
        raise api.SkillError("crop_focus 必须是两个 0–1 数值。")
    focus = tuple(number(v, 'crop_focus', api) for v in focus)
    jpeg_quality = int(number(spec.get('jpeg_quality', 94), 'jpeg_quality', api, 1, 100))
    texts, overlays = spec.get('texts', []), spec.get('overlays', [])
    if not isinstance(texts, list) or not isinstance(overlays, list) or len(texts) + len(overlays) > 50:
        raise api.SkillError("texts/overlays 必须是数组，总图层数不超过 50。")
    assets = [bg_path]
    layer_fonts = []
    for layer in texts + overlays:
        if not isinstance(layer, dict):
            raise api.SkillError("图层必须是对象。")
        x, y, w, h = [number(layer.get(k, d), k, api) for k, d in [('x', .06), ('y', .1), ('width', .88), ('height', .3)]]
        if w <= 0 or h <= 0 or x + w > 1.000001 or y + h > 1.000001:
            raise api.SkillError("图层边框必须在画面内，宽高大于 0。")
    for text in texts:
        if set(text) - {'text', 'font', 'x', 'y', 'width', 'height', 'font_size', 'min_font_size', 'fill', 'stroke_width', 'stroke_fill', 'shadow', 'align', 'spacing'}:
            raise api.SkillError("文字图层含未知字段。")
        if not isinstance(text.get('text'), str) or not text['text'].strip():
            raise api.SkillError("文字图层必须含非空 text 原文。")
        if text.get('align', 'left') not in ('left', 'center', 'right'):
            raise api.SkillError("align 必须为 left/center/right。")
        font = font_path(text.get('font', spec.get('font')), root, api)
        layer_fonts.append(font)
        assets.append(font)
    loaded_overlays = []
    try:
        with Image.open(bg_path) as image:
            image.load()
            background = ImageOps.exif_transpose(image).convert('RGBA')
        for layer in overlays:
            if set(layer) - {'image', 'x', 'y', 'width', 'height', 'opacity'}:
                raise api.SkillError("图片图层含未知字段。")
            path = resolve_file(layer.get('image'), root, api)
            assets.append(path)
            with Image.open(path) as image:
                image.load()
                loaded_overlays.append(ImageOps.exif_transpose(image).convert('RGBA'))
        rendered = []
        layouts = []
        for width, height in dimensions:
            canvas = ImageOps.fit(background, (width, height), method=Image.Resampling.LANCZOS, centering=focus)
            for layer, source in zip(overlays, loaded_overlays):
                boxw = max(1, round(layer.get('width', .88) * width))
                boxh = max(1, round(layer.get('height', .3) * height))
                fitted = ImageOps.contain(source, (boxw, boxh), Image.Resampling.LANCZOS)
                opacity = number(layer.get('opacity', 1), 'opacity', api)
                fitted.putalpha(fitted.getchannel('A').point(lambda v: round(v * opacity)))
                x = round(layer.get('x', .06) * width) + (boxw - fitted.width) // 2
                y = round(layer.get('y', .1) * height) + (boxh - fitted.height) // 2
                canvas.alpha_composite(fitted, (x, y))
            draw = ImageDraw.Draw(canvas)
            layout = []
            for layer, path in zip(texts, layer_fonts):
                x, y = round(layer.get('x', .06) * width), round(layer.get('y', .1) * height)
                boxw, boxh = round(layer.get('width', .88) * width), round(layer.get('height', .3) * height)
                if min(x / width, y / height, (width-x-boxw) / width, (height-y-boxh) / height) + .00001 < margin:
                    raise api.SkillError("文字框超出安全边距，请调整位置/尺寸或 safe_margin。")
                scale = height / 720
                maximum = round(number(layer.get('font_size', 88), 'font_size', api, 1, 1000) * scale)
                minimum = max(1, round(number(layer.get('min_font_size', 24), 'min_font_size', api, 1, 1000) * scale))
                if minimum > maximum:
                    raise api.SkillError("min_font_size 不能大于 font_size。")
                stroke = round(number(layer.get('stroke_width', 2), 'stroke_width', api, 0, 50) * scale)
                spacing = round(number(layer.get('spacing', 8), 'spacing', api, 0, 100) * scale)
                shadow = layer.get('shadow')
                shadow_dx = shadow_dy = 0
                if shadow is not None:
                    if not isinstance(shadow, dict) or set(shadow) - {'dx', 'dy', 'fill'}:
                        raise api.SkillError("shadow 必须含 dx/dy/fill 中的字段。")
                    shadow_dx = round(number(shadow.get('dx', 3), 'shadow.dx', api, -50, 50) * scale)
                    shadow_dy = round(number(shadow.get('dy', 4), 'shadow.dy', api, -50, 50) * scale)
                for size in range(max(1, maximum), minimum-1, -1):
                    font = ImageFont.truetype(str(path), size)
                    wrapped = wrap_text(layer['text'], draw, font, boxw - abs(shadow_dx), stroke)
                    bounds = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=spacing, stroke_width=stroke, align=layer.get('align', 'left'))
                    tw, th = bounds[2]-bounds[0], bounds[3]-bounds[1]
                    if tw + abs(shadow_dx) <= boxw and th + abs(shadow_dy) <= boxh:
                        break
                else:
                    raise api.SkillError("文字在最小字号下仍放不进文本框；未裁切或删改原文。")
                validate_glyphs(font, layer['text'], api)
                align = layer.get('align', 'left')
                offset = 0 if align == 'left' else ((boxw - tw - abs(shadow_dx)) // 2 if align == 'center' else boxw - tw - abs(shadow_dx))
                origin = (x - bounds[0] + offset + max(0, -shadow_dx), y - bounds[1] + max(0, -shadow_dy))
                kwargs = dict(font=font, spacing=spacing, stroke_width=stroke, align=align)
                if shadow:
                    draw.multiline_text((origin[0]+shadow_dx, origin[1]+shadow_dy), wrapped, fill=shadow.get('fill', '#000000'), stroke_fill=shadow.get('fill', '#000000'), **kwargs)
                draw.multiline_text(origin, wrapped, fill=layer.get('fill', '#ffffff'), stroke_fill=layer.get('stroke_fill', '#111827'), **kwargs)
                layout.append({'text': layer['text'], 'rendered_text': wrapped, 'font': str(path), 'font_size': size,
                               'box': [x, y, boxw, boxh]})
            rendered.append(canvas)
            layouts.append({'size': f'{width}x{height}', 'texts': layout})
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise api.SkillError("图片、字体或颜色无法解析，请检查封面配方。") from None
    output = Path(args.output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    run_id = uuid4().hex[:12]
    files = []
    for image in rendered:
        for fmt in formats:
            path = output / f'cover-{run_id}-{image.width}x{image.height}.{ "jpg" if fmt == "jpeg" else fmt}'
            kwargs = {'quality': jpeg_quality} if fmt in ('jpeg', 'webp') else {}
            image.convert('RGB' if fmt == 'jpeg' else 'RGBA').save(path, format=fmt.upper(), **kwargs)
            files.append(str(path))
    manifest = output / f'cover-{run_id}.json'
    result = {'ok': True, 'operation': 'local_cover', 'files': files, 'spec': spec,
              'source_spec': str(spec_path), 'layouts': layouts,
              'assets': [{'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()} for path in dict.fromkeys(assets)],
              'manifest': str(manifest), 'note': '本地裁切、合成和排版输出；不是接口原生尺寸。'}
    extra.atomic_json(manifest, result)
    return result
