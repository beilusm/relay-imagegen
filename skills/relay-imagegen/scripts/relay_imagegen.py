#!/usr/bin/env python3
"""Generate and edit images through a configured relay; save model output unchanged."""
import argparse
import importlib
import base64
import binascii
import getpass
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import sys
import struct
import time
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from uuid import uuid4

DEFAULTS = {"base_url": "", "api_key": "", "model": "image2.5", "timeout": 180}
MAX_JSON_BYTES = 128 * 1024 * 1024
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_REFERENCE_BYTES = 64 * 1024 * 1024


class SkillError(Exception):
    pass


def byte_limit(name, default):
    """Configurable local memory guards, independent of model capabilities."""
    value = os.environ.get("RELAY_IMAGE_MAX_" + name + "_MIB")
    if value is None:
        return default
    try:
        mib = float(value)
        if not math.isfinite(mib) or mib <= 0:
            raise ValueError
        limit = int(mib * 1024 * 1024)
        if limit < 1:
            raise ValueError
        return limit
    except (ValueError, OverflowError):
        raise SkillError(f"RELAY_IMAGE_MAX_{name}_MIB 必须是大于 0 的有限数值。") from None


def config_path(explicit=None):
    if explicit or os.environ.get("RELAY_IMAGE_CONFIG"):
        return Path(explicit or os.environ["RELAY_IMAGE_CONFIG"]).expanduser().absolute()
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser() / "relay-imagegen.json"


def read_config(path, use_env=True):
    data = dict(DEFAULTS)
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise SkillError("配置文件无法读取或不是有效 JSON。") from None
        if not isinstance(stored, dict):
            raise SkillError("配置文件必须是 JSON 对象。")
        data.update(stored)
    if use_env:
        for field in ("base_url", "api_key", "model"):
            value = os.environ.get("RELAY_IMAGE_" + field.upper())
            if value:
                data[field] = value
    for field in ("base_url", "api_key", "model"):
        if not isinstance(data.get(field), str):
            raise SkillError(f"配置字段 {field} 必须是字符串。")
        data[field] = data[field].strip()
    try:
        timeout = float(data["timeout"])
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError
        data["timeout"] = timeout
    except (ValueError, TypeError):
        raise SkillError("timeout 必须是大于 0 的秒数。") from None
    return data


def write_config(path, data, replace=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise SkillError("配置路径是符号链接，请使用实际文件路径。")
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if not replace:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise SkillError("配置文件已存在，未覆盖。可编辑该文件或运行 config setup。") from None
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        return
    fd, temporary = tempfile.mkstemp(prefix=".relay-imagegen-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def check_url(value):
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError:
        raise SkillError("URL 格式或端口无效。") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise SkillError("URL 必须以 https:// 或 http:// 开头并包含主机名。")
    if parts.username or parts.password or parts.fragment:
        raise SkillError("URL 不应包含用户名、密码或 #fragment。")
    if any(c.isspace() or ord(c) < 32 for c in value):
        raise SkillError("URL 不能包含空格或控制字符。")
    return parts


def api_endpoint(base_url, resource):
    parts = check_url(base_url)
    if parts.query:
        raise SkillError("Base URL 不应包含查询参数，API Key 请填入 api_key。")
    path = parts.path.rstrip("/")
    explicit = False
    for suffix in ("/images/generations", "/images/edits", "/models"):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            explicit = True
            break
    if not explicit and not path.endswith("/v1"):
        path += "/v1"
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path + "/" + resource, "", ""))


def generation_endpoint(base_url):
    return api_endpoint(base_url, "images/generations")


def configured_request_mode(config, endpoint, model):
    profiles = config.get("request_modes", {})
    if not isinstance(profiles, dict):
        raise SkillError("request_modes 必须是按接口地址和模型保存的对象。")
    modes = profiles.get(endpoint, {})
    if not isinstance(modes, dict):
        raise SkillError("request_modes 中的接口配置必须是对象。")
    mode = modes.get(model, "single")
    if mode not in ("single", "separate"):
        raise SkillError("request_modes 中的模式必须是 single 或 separate。")
    return mode


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SkillError("API 接口发生重定向，已停止；请将 Base URL 改为最终接口地址。")


class ImageRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = check_url(newurl)
        if req.type == "https" and parts.scheme != "https":
            raise SkillError("图片下载被重定向至不安全的 HTTP 地址。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def safe_error_details(details, request):
    """Expose bounded structured API validation errors, never raw bodies or credentials."""
    if not isinstance(details, dict):
        return ""
    error = details.get("error", details)
    authorization = request.get_header("Authorization", "")
    secret = authorization[7:] if authorization.lower().startswith("bearer ") else authorization

    def clean(value):
        text = str(value)
        if secret:
            text = text.replace(secret, "[REDACTED]")
        text = re.sub(r"(?i)bearer\s+[^\s\"'<>]+", "Bearer [REDACTED]", text)
        text = re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", text)
        return " ".join(text.split())[:500]

    if isinstance(error, dict):
        fields = [f"{name}={clean(error[name])}" for name in ("type", "code", "param", "message")
                  if isinstance(error.get(name), (str, int, float)) and error[name] != ""]
        if fields:
            return "; ".join(fields)[:1800]
    # FastAPI/Pydantic validation uses detail[].loc/msg/type; never echo its input/ctx.
    validation = details.get("detail")
    if isinstance(validation, list):
        fields = []
        for entry in validation[:5]:
            if not isinstance(entry, dict):
                continue
            location = entry.get("loc", [])
            location = ".".join(str(part) for part in location) if isinstance(location, list) else str(location)
            fields.append(clean(location) + ": " + clean(entry.get("msg", entry.get("type", "validation error"))))
        return "; ".join(fields)[:1800]
    return ""


def fetch(request, timeout, limit, generation=False, models=False):
    handler = NoRedirect() if generation or models else ImageRedirect()
    try:
        with urllib.request.build_opener(handler).open(request, timeout=timeout) as response:
            content = response.read(limit + 1)
            if len(content) > limit:
                raise SkillError("响应超出本地大小限制，已停止保存。")
            return content
    except urllib.error.HTTPError as exc:
        code = exc.code
        # Parse bounded structured details; raw HTML/body and validation input stay private.
        details = None
        try:
            details = json.loads(exc.read(64 * 1024))
        except (ValueError, UnicodeError, OSError):
            pass
        finally:
            exc.close()
        if (isinstance(details, dict) and details.get("cloudflare_error") is True
                and str(details.get("error_code")) == "1010"):
            label = "模型列表接口" if models else ("生成接口" if generation else "图片下载")
            raise SkillError(
                f"{label}被 Cloudflare 拦截（HTTP 403 / Error 1010）："
                "站点按客户端标识阻止访问。请联系中转站管理员放行正常 API 客户端访问；"
                "此响应不能证明 API Key 无效。未自动重试。"
            ) from None
        if not generation and not models:
            raise SkillError(f"图片下载返回 HTTP {code}；未重新生成图片。") from None
        hint = {
            400: "检查模型名及 size、quality 等参数是否被中转站支持。",
            401: "检查 API Key 是否正确。",
            403: "检查账号权限或中转站访问限制。",
            404: "检查 Base URL、图片接口路径及模型名。",
            422: "接口拒绝了请求参数；检查下方字段校验信息。",
            429: "检查额度或限流状态。",
            502: "网关或上游服务异常；服务端可能已收到并处理请求，先核对服务商记录。",
            503: "接口暂时不可用；先核对服务状态及本次请求记录。",
            504: "网关等待上游超时；生成可能仍在进行，先核对服务商记录。",
        }.get(code, "检查中转站服务状态。")
        label = "模型列表接口" if models else "生成接口"
        detail = safe_error_details(details, request)
        suffix = f" 参数详情：{detail}" if detail else " 接口未提供可安全展示的结构化错误详情。"
        raise SkillError(f"{label}返回 HTTP {code}：{hint}{suffix} 未自动重试。") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        message = "模型列表请求失败或超时。" if models else ("请求失败或超时；服务端可能已收到请求，未自动重试。" if generation else "图片下载失败或超时；未重新生成图片。")
        raise SkillError(message) from None


def image_bytes(item, timeout):
    limit = byte_limit("IMAGE", MAX_IMAGE_BYTES)
    if not isinstance(item, dict):
        raise SkillError("返回的图片项不是对象。")
    encoded = item.get("b64_json")
    if isinstance(encoded, str) and encoded:
        if encoded.startswith("data:"):
            header, separator, encoded = encoded.partition(",")
            if not separator or ";base64" not in header:
                raise SkillError("图片 Data URL 格式无效。")
        if len(encoded) > ((limit + 2) // 3) * 4 + 1024:
            raise SkillError("图片超过本地内存保护上限；可提高 RELAY_IMAGE_MAX_IMAGE_MIB。")
        try:
            data = base64.b64decode("".join(encoded.split()), validate=True)
        except (ValueError, binascii.Error):
            raise SkillError("接口返回的图片 Base64 无法解码。") from None
    elif isinstance(item.get("url"), str) and item["url"]:
        check_url(item["url"])
        # Never attach relay credentials to image/CDN URLs, including redirects.
        request = urllib.request.Request(item["url"], headers={"Accept": "image/*"})
        data = fetch(request, timeout, limit)
    else:
        raise SkillError("图片项缺少 b64_json 或 url。")
    if len(data) > limit:
        raise SkillError("图片超过本地内存保护上限；可提高 RELAY_IMAGE_MAX_IMAGE_MIB。")
    return data, image_format(data)


def image_format(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 33:
        return "png"
    if data.startswith(b"\xff\xd8\xff") and len(data) >= 4:
        return "jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP" and len(data) >= 20:
        return "webp"
    raise SkillError("文件不是可识别的 PNG/JPEG/WebP 图片。")


def load_reference_images(paths):
    if not paths:
        raise SkillError("图片编辑需要至少一张本地参考图，每张使用一次 --image 参数。")
    image_limit = byte_limit("IMAGE", MAX_IMAGE_BYTES)
    reference_limit = byte_limit("REFERENCE", MAX_REFERENCE_BYTES)
    references = []
    total = 0
    for value in paths:
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise SkillError("参考图文件不存在或不是普通文件；请检查 --image 路径。")
        with path.open("rb") as f:
            data = f.read(image_limit + 1)
        total += len(data)
        if len(data) > image_limit or total > reference_limit:
            raise SkillError("参考图超过本地内存保护上限；可提高 RELAY_IMAGE_MAX_IMAGE_MIB / RELAY_IMAGE_MAX_REFERENCE_MIB。未提交请求。")
        extension = image_format(data)
        width, height = image_dimensions(data, extension)
        if not width or not height:
            raise SkillError("无法读取参考图尺寸，请先确认图片可正常打开。未提交请求。")
        references.append({"path": str(path), "data": data, "extension": extension,
                           "mime": "image/jpeg" if extension == "jpg" else "image/" + extension,
                           "width": width, "height": height,
                           "sha256": hashlib.sha256(data).hexdigest()})
    return references


def image_reference(value):
    if not isinstance(value, dict) or set(value) not in ({"image_url"}, {"file_id"}):
        raise SkillError("每张图片引用必须且只能包含 image_url 或 file_id。")
    key = next(iter(value))
    source = value[key]
    if not isinstance(source, str) or not source.strip():
        raise SkillError("图片引用必须是非空字符串。")
    if key == "image_url":
        if source.startswith("data:"):
            if not re.match(r"data:image/[\w.+-]+;base64,", source):
                raise SkillError("图片 Data URL 必须使用 image/*;base64 格式。")
            if len(source) > 20971520:
                raise SkillError("GPT Image Images API 限定单张 Data URL 为 20 MiB。")
        else:
            check_url(source)
    return dict(value)


def load_edit_references(args):
    references = load_reference_images(args.images) if args.images else []
    references.extend(image_reference({"image_url": value}) for value in (args.image_urls or []))
    references.extend(image_reference({"file_id": value}) for value in (args.image_file_ids or []))
    if args.images_file:
        try:
            values = json.loads(Path(args.images_file).expanduser().read_text(encoding="utf-8"))
        except (ValueError, UnicodeError):
            raise SkillError("images-file 必须是 UTF-8 JSON 图片引用数组。") from None
        if not isinstance(values, list) or not values:
            raise SkillError("images-file 必须是非空图片引用数组。")
        references.extend(image_reference(value) for value in values)
    if not references:
        raise SkillError("edit 需要本地图片、image-url、image-file-id、images-file 或已选中版本。")
    if len(references) > 16:
        raise SkillError("GPT Image Images API 最多接受 16 张输入图片。")
    return references


def reference_metadata(ref):
    if "data" in ref:
        return {key: ref[key] for key in ("path", "width", "height", "sha256")}
    if ref.get("image_url", "").startswith("data:"):
        return {"source": "data_url", "sha256": hashlib.sha256(ref["image_url"].encode()).hexdigest()}
    return dict(ref)


def image_request_payload(parameters, references, transport="multipart", mask=None):
    if not references:
        return json.dumps(parameters, ensure_ascii=False).encode("utf-8"), "application/json"
    if transport == "json":
        images = [{"image_url": features().data_url(ref)} if "data" in ref else dict(ref) for ref in references]
        if any(len(image.get("image_url", "")) > 20971520 for image in images):
            raise SkillError("GPT Image Images API 限定单张 Data URL 为 20 MiB。")
        body = dict(parameters, images=images)
        if mask:
            body["mask"] = {"image_url": features().data_url(mask)} if "data" in mask else dict(mask)
        return json.dumps(body, ensure_ascii=False).encode("utf-8"), "application/json"
    if any("data" not in ref for ref in references) or (mask and "data" not in mask):
        raise SkillError("URL/file_id 图片或蒙版需要 JSON 编辑协议；请使用 --edit-transport json。")
    boundary = "relay-imagegen-" + uuid4().hex
    chunks = []
    for name, value in parameters.items():
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif isinstance(value, (dict, list)) or value is None:
            value = json.dumps(value, ensure_ascii=False, allow_nan=False)
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8"))
    field = "image" if len(references) == 1 else "image[]"
    for index, ref in enumerate(references, 1):
        # Wire filenames are generated, so local names cannot inject headers.
        header = f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="reference-{index:02d}.{ref["extension"]}"\r\nContent-Type: {ref["mime"]}\r\n\r\n'
        chunks.extend((header.encode("ascii"), ref["data"], b"\r\n"))
    if mask:
        header = f'--{boundary}\r\nContent-Disposition: form-data; name="mask"; filename="mask.png"\r\nContent-Type: image/png\r\n\r\n'
        chunks.extend((header.encode("ascii"), mask["data"], b"\r\n"))
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), "multipart/form-data; boundary=" + boundary


def run_models(args):
    config = read_config(config_path(args.config))
    if args.base_url:
        config["base_url"] = args.base_url
    if args.timeout is not None:
        config["timeout"] = args.timeout
    missing = [k for k in ("base_url", "api_key") if not config[k]]
    if missing:
        raise SkillError("查询模型前请先配置：" + ", ".join(missing) + "。")
    if not config["api_key"].isascii() or any(c.isspace() for c in config["api_key"]):
        raise SkillError("API Key 应为不含空白的 ASCII 字符串。")
    if not math.isfinite(config["timeout"]) or config["timeout"] <= 0:
        raise SkillError("timeout 必须大于 0。")
    endpoint = api_endpoint(config["base_url"], "models")
    request = urllib.request.Request(endpoint, headers={"Authorization": "Bearer " + config["api_key"], "Accept": "application/json"})
    raw = fetch(request, config["timeout"], 8 * 1024 * 1024, models=True)
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError):
        raise SkillError("模型列表接口未返回有效 JSON，请检查 API 地址。") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise SkillError("模型列表响应缺少 data 数组，需要兼容 GET /v1/models 的接口。")
    ids = []
    for item in payload["data"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
            raise SkillError("模型列表中出现无效条目，每个条目必须含非空字符串 id。")
        ids.append(item["id"])
    ids = sorted(set(ids))
    total = len(ids)
    if args.filter:
        ids = [model for model in ids if args.filter.casefold() in model.casefold()]
    return {"ok": True, "endpoint": endpoint, "total": total, "count": len(ids), "models": ids,
            "configured_model": config["model"], "filter": args.filter,
            "note": "模型列表不保证每个模型都支持图片生成；筛选仅匹配名称。"}


def run_config(args):
    path = config_path(args.config)
    if args.action == "init":
        write_config(path, DEFAULTS)
        return {"ok": True, "config_path": str(path), "message": "模板已创建；请填写 base_url、api_key 和实际模型名。"}
    if args.action == "set-request-mode":
        if args.model_id not in ("auto", "single", "separate"):
            raise SkillError("用法：config set-request-mode auto|single|separate [--model 模型ID] [--operation generate|edit]")
        config = read_config(path, use_env=False)
        effective = read_config(path)
        if not effective["base_url"]:
            raise SkillError("请先配置 Base URL，以便按接口和模型保存请求模式。")
        model = args.model or effective["model"]
        endpoint = api_endpoint(effective["base_url"], "images/edits" if args.operation == "edit" else "images/generations")
        configured_request_mode(config, endpoint, model)
        modes = config.setdefault("request_modes", {}).setdefault(endpoint, {})
        if args.model_id == "auto":
            modes.pop(model, None)
        else:
            modes[model] = args.model_id
        if not modes:
            config["request_modes"].pop(endpoint, None)
        if not config["request_modes"]:
            config.pop("request_modes")
        write_config(path, config, replace=True)
        return {"ok": True, "endpoint": endpoint, "model": model,
                "request_mode": configured_request_mode(config, endpoint, model),
                "note": "仅保存此接口和模型的提交方式；不会重试或限制模型参数。显式 --request-mode 可覆盖。"}
    if args.action == "set-model":
        if not args.model_id or not args.model_id.strip():
            raise SkillError("用法：config set-model 模型ID")
        config = read_config(path, use_env=False)
        config["model"] = args.model_id.strip()
        write_config(path, config, replace=True)
        return {"ok": True, "config_path": str(path), "model": config["model"], "effective_model": read_config(path)["model"]}
    if args.action == "setup":
        if not sys.stdin.isatty():
            raise SkillError("config setup 需要用户本机交互式终端；也可运行 config init 后自行编辑配置。")
        config = read_config(path, use_env=False)
        base = input(f"Base URL [{config['base_url']}]: ").strip() or config["base_url"]
        generation_endpoint(base)
        model = input(f"模型名 [{config['model']}]: ").strip() or config["model"]
        key = getpass.getpass("API Key（输入隐藏；留空保留已有值）: ").strip() or config["api_key"]
        if not model or not key:
            raise SkillError("模型名和 API Key 不能为空。")
        if not key.isascii() or any(c.isspace() for c in key):
            raise SkillError("API Key 应为不含空白的 ASCII 字符串。")
        config.update(base_url=base, model=model, api_key=key)
        write_config(path, config, replace=True)
        return {"ok": True, "config_path": str(path), "message": "配置已保存。"}
    config = read_config(path)
    missing = [k for k in ("base_url", "api_key", "model") if not config[k]]
    endpoint = generation_endpoint(config["base_url"]) if config["base_url"] else None
    return {"ok": True, "ready": not missing, "config_path": str(path), "endpoint": endpoint, "models_endpoint": api_endpoint(config["base_url"], "models") if config["base_url"] else None,
            "edits_endpoint": api_endpoint(config["base_url"], "images/edits") if config["base_url"] else None,
            "generation_request_mode": configured_request_mode(config, endpoint, config["model"]),
            "edit_request_mode": configured_request_mode(config, api_endpoint(config["base_url"], "images/edits") if config["base_url"] else None, config["model"]),
            "model": config["model"], "api_key_configured": bool(config["api_key"]), "missing": missing}


def image_dimensions(data, extension):
    """Read dimensions without changing images or requiring Pillow."""
    if extension == "png" and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        return (width, height) if width and height else (None, None)
    if extension == "webp" and len(data) >= 30:
        kind = data[12:16]
        if kind == b"VP8X":
            return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
        if kind == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            return int.from_bytes(data[26:28], "little") & 0x3fff, int.from_bytes(data[28:30], "little") & 0x3fff
    if extension == "webp" and len(data) >= 25 and data[12:16] == b"VP8L" and data[20] == 0x2f:
        bits = int.from_bytes(data[21:25], "little")
        return 1 + (bits & 0x3fff), 1 + ((bits >> 14) & 0x3fff)
    if extension == "jpg":
        offset = 2
        sof = {0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf}
        while offset + 4 <= len(data):
            if data[offset] != 0xff:
                break
            while offset < len(data) and data[offset] == 0xff:
                offset += 1
            if offset >= len(data):
                break
            marker = data[offset]
            offset += 1
            if marker in (0xd9, 0xda):
                break
            if marker == 0x01 or 0xd0 <= marker <= 0xd8:
                continue
            if offset + 2 > len(data):
                break
            length = int.from_bytes(data[offset:offset+2], "big")
            if length < 2 or offset + length > len(data):
                break
            if marker in sof and length >= 7:
                height, width = struct.unpack(">HH", data[offset+3:offset+7])
                return (width, height) if width and height else (None, None)
            offset += length
    return None, None


def generation_plan(args):
    if args.n < 1:
        raise SkillError("n 必须是正整数。")
    if args.prompts_file:
        if args.n != 1:
            raise SkillError("--prompts-file 中每条提示词生成一张，不与 --n 叠加。")
        raw = sys.stdin.read() if args.prompts_file == "-" else Path(args.prompts_file).expanduser().read_text(encoding="utf-8")
        try:
            prompts = json.loads(raw)
        except ValueError:
            raise SkillError("prompts-file 必须是 UTF-8 JSON 字符串数组。") from None
        if not isinstance(prompts, list) or not prompts:
            raise SkillError("prompts-file 必须是非空提示词数组。")
        if any(not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000 for prompt in prompts):
            raise SkillError("每条提示词必须是 1–32,000 字符的非空字符串；已在发送任何请求前停止。")
        return [(prompt, 1) for prompt in prompts], "separate"
    if args.prompt_file:
        prompt = sys.stdin.read() if args.prompt_file == "-" else Path(args.prompt_file).expanduser().read_text(encoding="utf-8")
    else:
        prompt = args.prompt
    if not prompt or not prompt.strip():
        raise SkillError("提示词不能为空。")
    if len(prompt) > 32000:
        raise SkillError("GPT Image Images API 提示词上限为 32,000 字符。")
    if args.request_mode == "separate":
        return [(prompt, 1) for _ in range(args.n)], "separate"
    return [(prompt, min(10, args.n - offset)) for offset in range(0, args.n, 10)], "single" if args.n <= 10 else "chunked"


def save_run_manifest(result):
    # Credential-free, atomic checkpoint. An interrupted request stays marked
    # in_progress so it cannot be mistaken for a known unsubmitted request.
    path = Path(result["manifest"])
    fd, temporary = tempfile.mkstemp(prefix=".image-run-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_gallery(result, path):
    cards = []
    for image in result["images"]:
        name = html.escape(Path(image["path"]).name, quote=True)
        size = f"{image['width']} × {image['height']}" if image["width"] else "尺寸未知"
        cards.append(f'<figure><a href="{name}"><img src="{name}" alt="图片 {image["index"]}" loading="lazy"></a><figcaption>图片 {image["index"]} · {size} · <a href="{name}" download>下载原图</a></figcaption></figure>')
    page = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>生成的图片</title><style>body{font:16px system-ui,sans-serif;margin:32px;background:#f4f5f7;color:#202124}main{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:20px;max-width:1400px}figure{margin:0;padding:12px;background:white;border-radius:12px}img{width:100%;height:340px;object-fit:contain}figcaption{padding:12px 0}a{color:#245ac4}</style>'
    page += f'<h1>生成的图片</h1><p>{html.escape(result["model"])} · 已保存 {result["saved_count"]} / 请求 {result["requested_count"]} 张</p><main>' + "".join(cards) + '</main></html>'
    path.write_text(page, encoding="utf-8")


def features():
    # Works both as an executable and when imported by the offline test suite.
    folder = str(Path(__file__).resolve().parent)
    if folder not in sys.path:
        sys.path.insert(0, folder)
    return importlib.import_module("relay_features")


def api_module():
    # Tests may load this file without registering it in sys.modules.
    from types import SimpleNamespace
    return SimpleNamespace(**globals())


def run_generate(args):
    return features().run_with_session(args, api_module(), _run_generate)


def _run_generate(args):
    # Reject invalid local output guards before making a billable request.
    byte_limit("IMAGE", MAX_IMAGE_BYTES)
    byte_limit("REFERENCE", MAX_REFERENCE_BYTES)
    response_limit = byte_limit("RESPONSE", MAX_JSON_BYTES)
    config = read_config(config_path(args.config))
    for field in ("base_url", "model", "timeout"):
        value = getattr(args, field)
        if value is not None:
            config[field] = value
    missing = [k for k in ("base_url", "api_key", "model") if not config[k]]
    if missing:
        raise SkillError("尚未配置：" + ", ".join(missing) + "。请先运行 config init/setup，或设置 RELAY_IMAGE_* 环境变量。")
    if not config["api_key"].isascii() or any(c.isspace() for c in config["api_key"]):
        raise SkillError("API Key 应为不含空白的 ASCII 字符串。")
    operation = args.command
    if operation != "edit" and (args.mask or args.mask_url or args.mask_file_id or args.from_run or args.input_fidelity):
        raise SkillError("蒙版、from-run 和 input-fidelity 仅用于 edit。")
    references = load_edit_references(args) if operation == "edit" else []
    has_remote = any("data" not in ref for ref in references) or bool(args.mask_url or args.mask_file_id)
    transport = (args.edit_transport or ("json" if has_remote else "multipart")) if operation == "edit" else "json"
    if has_remote and transport != "json":
        raise SkillError("URL/file_id 图片或蒙版需要 JSON 编辑协议；请使用 --edit-transport json。")
    endpoint = api_endpoint(config["base_url"], "images/edits" if operation == "edit" else "images/generations")
    request_mode_source = "cli" if args.request_mode is not None else "config/default"
    if args.request_mode is None:
        args.request_mode = configured_request_mode(config, endpoint, config["model"])
    extra = features()
    api = api_module()
    mask = extra.load_mask(args.mask, references, api) if args.mask else None
    if args.mask_url:
        mask = image_reference({"image_url": args.mask_url})
    elif args.mask_file_id:
        mask = image_reference({"file_id": args.mask_file_id})
    if transport == "json":
        for ref in references + ([mask] if mask else []):
            length = ((len(ref["data"]) + 2) // 3) * 4 + len("data:" + ref["mime"] + ";base64,") if "data" in ref else len(ref.get("image_url", ""))
            if length > 20971520:
                raise SkillError("GPT Image Images API 限定单张 Data URL 为 20 MiB；本地图可使用 multipart，不能自动缩小原图。")
    if args.safety_identifier is not None and len(args.safety_identifier) > 128:
        raise SkillError("safety-identifier 不能超过 128 字符。")
    profile = extra.capability_check(args, config, references, api)
    if args.output_compression is not None and not 0 <= args.output_compression <= 100:
        raise SkillError("output-compression 必须在 0–100 之间。")
    if args.output_compression is not None and args.output_format not in ("jpeg", "webp"):
        raise SkillError("output-compression 需要显式指定 jpeg 或 webp 格式。")
    if args.partial_images is not None and (not args.stream or not 0 <= args.partial_images <= 3):
        raise SkillError("partial-images 需要 --stream，且取值为 0–3。")
    if not math.isfinite(config["timeout"]) or config["timeout"] <= 0:
        raise SkillError("timeout 必须大于 0。")
    plan, mode = generation_plan(args)
    if args.size and args.size != "auto" and not re.fullmatch(r"[1-9]\d*x[1-9]\d*", args.size):
        raise SkillError("size 应为 auto 或 WIDTHxHEIGHT。")
    if args.background == "transparent" and args.output_format == "jpeg":
        raise SkillError("透明背景不能使用 JPEG，请使用 PNG 或 WebP。")
    options = extra.read_json(args.api_params_file, SkillError) if args.api_params_file else {}
    if any(not name or any(ord(c) < 32 or c in '\\"' or ord(c) == 127 for c in name) for name in options):
        raise SkillError("API 参数名不能为空或包含控制字符、引号、反斜线。")
    reserved = {"model", "prompt", "n", "image", "image[]", "images", "mask", "stream", "partial_images",
                "size", "quality", "background", "output_format", "response_format", "input_fidelity", "output_compression", "moderation", "safety_identifier", "user"}
    if reserved.intersection(options):
        raise SkillError("api-params-file 中的标准字段请用对应 CLI 选项设置；不覆盖提示词、参考图或请求数量。")
    try:
        json.dumps(options, allow_nan=False)
    except ValueError:
        raise SkillError("api-params-file 不能包含 NaN 或 Infinity。") from None
    options["model"] = config["model"]
    for field in ("size", "quality", "background", "output_format", "input_fidelity", "output_compression", "moderation", "safety_identifier", "user"):
        value = getattr(args, field)
        if value is not None:
            options[field] = value
    if args.stream:
        options["stream"] = True
        if args.partial_images is not None:
            options["partial_images"] = args.partial_images
    # Validate all local inputs before submitting any potentially billable request.
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    result = {
        "ok": False, "model": config["model"], "operation": operation, "request_mode": mode,
        "backend": "images", "endpoint": endpoint, "request_mode_source": request_mode_source,
        "parent": getattr(args, "parent", None), "capabilities": profile,
        "mask": reference_metadata(mask) if mask else None,
        "previews": [],
        "references": [reference_metadata(ref) for ref in references],
        "edit_transport": transport if references else None,
        "requested_count": sum(n for _, n in plan), "saved_count": 0,
        "planned_requests": len(plan), "attempted_requests": 0,
        "files": [], "images": [], "errors": [], "warnings": list(args.capability_warnings),
        "manifest": str(output_dir / f"run-{run_id}.json"),
        "requests": [{"index": i, "status": "pending", "parameters": dict(options, prompt=prompt, n=n)}
                     for i, (prompt, n) in enumerate(plan, 1)],
    }
    save_run_manifest(result)
    start = time.monotonic()
    for entry in result["requests"]:
        request_index = entry["index"]
        entry["status"] = "in_progress"
        result["attempted_requests"] += 1
        try:
            save_run_manifest(result)
        except OSError:
            entry["status"] = "pending"
            result["attempted_requests"] -= 1
            result["errors"].append({"error": "无法记录请求状态，已停止提交后续请求。"})
            break
        print(f"请求 {request_index}/{len(plan)}：正在生成 {entry['parameters']['n']} 张图片。", file=sys.stderr, flush=True)
        try:
            first_error = len(result["errors"])
            entry["returned_count"] = 0

            def save_image(item, preview=False):
                if not preview:
                    entry["returned_count"] += 1
                index = len(result["previews"]) + 1 if preview else entry["returned_count"]
                try:
                    data, extension = image_bytes(item, config["timeout"])
                    width, height = image_dimensions(data, extension)
                    prefix = "preview" if preview else "image"
                    path = output_dir / f"{prefix}-{run_id}-r{request_index:02d}-{index:02d}.{extension}"
                    with path.open("xb") as f:
                        f.write(data)
                    image = {"index": index if preview else len(result["files"]) + 1,
                             "request_index": request_index, "path": str(path), "width": width, "height": height,
                             "requested_size": args.size, "sha256": hashlib.sha256(data).hexdigest()}
                    image["response_metadata"] = extra.clean_metadata({key: value for key, value in item.items()
                        if key not in ("b64_json", "partial_image_b64", "url")}, config["api_key"])
                    if preview:
                        result["previews"].append(image)
                        print(f"预览已保存：{path}", file=sys.stderr, flush=True)
                    else:
                        for field in ("revised_prompt", "usage", "id"):
                            if item.get(field) is not None:
                                image[field] = extra.clean_metadata(item[field], config["api_key"])
                        result["files"].append(str(path))
                        result["images"].append(image)
                        result["saved_count"] = len(result["files"])
                        if args.size and args.size != "auto" and width and height and f"{width}x{height}" != args.size:
                            result["warnings"].append({"image_index": image["index"],
                                "warning": f"请求尺寸 {args.size}，实际返回 {width}x{height}；已保留原图，未缩放。"})
                        actual_format = "jpeg" if extension == "jpg" else extension
                        if args.output_format and args.output_format != actual_format:
                            result["warnings"].append({"image_index": image["index"], "warning": f"请求格式 {args.output_format}，实际返回 {actual_format}；已保留原图，未转码。"})
                        if not width or not height:
                            result["warnings"].append({"image_index": image["index"], "warning": "未能解析图片尺寸，请用图片查看工具检查。"})
                    save_run_manifest(result)
                except (SkillError, OSError) as exc:
                    message = str(exc) if isinstance(exc, SkillError) else "无法保存图片或更新运行记录。"
                    result["errors"].append({"request_index": request_index, "index": index, "error": message})

            request_data, content_type = image_request_payload(entry["parameters"], references, transport, mask)
            request = urllib.request.Request(endpoint, data=request_data,
                headers={"Authorization": "Bearer " + config["api_key"], "Content-Type": content_type,
                         "Accept": "text/event-stream" if args.stream else "application/json"}, method="POST")
            if args.stream:
                payload = extra.stream_request(request, config["timeout"],
                    lambda item: save_image(item, True), save_image, api, entry["parameters"]["n"])
            else:
                raw = fetch(request, config["timeout"], response_limit, generation=True)
                try:
                    payload = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise SkillError("接口未返回有效 JSON；检查是否误填了网站首页或流式接口。未自动重试。") from None
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list) or not payload["data"]:
                    raise SkillError("响应缺少非空 data 数组，可能与 Images API 不兼容；未自动重试。")
                for item in payload["data"]:
                    save_image(item)
            entry["response_metadata"] = extra.clean_metadata({key: value for key, value in payload.items() if key != "data"}, config["api_key"])
            for field in ("usage", "event_usage"):
                if payload.get(field) is not None:
                    entry[field] = extra.clean_metadata(payload[field], config["api_key"])
            if len(payload["data"]) != entry["parameters"]["n"]:
                result["errors"].append({"request_index": request_index,
                    "error": f"请求 {entry['parameters']['n']} 张，接口返回 {len(payload['data'])} 项；未补发请求。"})
            entry["status"] = "succeeded" if len(result["errors"]) == first_error else "failed"
        except (SkillError, OSError, UnicodeError) as exc:
            message = str(exc) if isinstance(exc, SkillError) else "请求处理或文件操作失败；未自动重试。"
            result["errors"].append({"request_index": request_index, "error": message})
            entry["status"] = "failed"
        except KeyboardInterrupt:
            result["errors"].append({"request_index": request_index, "error": "操作已取消；已发出的请求状态可能未知，未自动重试。"})
            entry["status"] = "interrupted"
        result["saved_count"] = len(result["files"])
        result["elapsed_seconds"] = round(time.monotonic() - start, 2)
        try:
            save_run_manifest(result)
        except OSError:
            result["errors"].append({"error": "无法更新本地结果清单；图片路径仍在本次输出中。"})
            break
        print(f"请求 {request_index}/{len(plan)}：{entry['status']}，累计保存 {result['saved_count']} 张。", file=sys.stderr, flush=True)
        if entry["status"] != "succeeded":
            break
    pending = [entry for entry in result["requests"] if entry["status"] == "pending"]
    result["unsubmitted_requests"] = len(pending)
    if pending:
        result["warnings"].append({"warning": f"因前序失败，剩余 {len(pending)} 个请求未提交；未自动补发。"})
    if len(result["files"]) > 1:
        gallery = output_dir / f"gallery-{run_id}.html"
        try:
            save_gallery(result, gallery)
            result["gallery"] = str(gallery)
        except OSError:
            result["warnings"].append({"warning": "预览画廊保存失败；原始图片仍可打开。"})
    result["ok"] = result["saved_count"] == result["requested_count"] and not result["errors"]
    if result["errors"]:
        result["error"] = result["errors"][0]["error"]
    try:
        save_run_manifest(result)
    except OSError:
        result["ok"] = False
        result["error"] = "无法更新本地结果清单；图片路径仍在本次输出中。"
    return result


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("config", help="初始化、交互配置或查看不含 Key 的状态")
    c.add_argument("action", choices=("init", "setup", "status", "set-model", "set-request-mode"))
    c.add_argument("model_id", nargs="?", help="set-model 的模型 ID")
    c.add_argument("--model", help="set-request-mode 的目标模型，默认使用当前模型")
    c.add_argument("--operation", choices=("generate", "edit"), default="generate", help="set-request-mode 的目标接口")
    c.add_argument("--config", help="覆盖默认配置文件路径")
    m = sub.add_parser("models", help="查询中转站模型列表，不生成图片")
    m.add_argument("--config")
    m.add_argument("--base-url")
    m.add_argument("--timeout", type=float)
    m.add_argument("--filter", help="按模型 ID 进行不区分大小写的包含匹配，例如 image")
    for name in ("generate", "edit"):
        g = sub.add_parser(name, help="文生图" if name == "generate" else "根据一张或多张参考图生成或修改图片")
        if name == "edit":
            g.add_argument("--image", dest="images", action="append", help="本地参考图路径；多张图重复此参数")
            g.add_argument("--image-url", dest="image_urls", action="append", help="参考图 HTTPS URL 或 Base64 Data URL；自动使用 JSON")
            g.add_argument("--image-file-id", dest="image_file_ids", action="append", help="已上传参考图的 Files API ID；自动使用 JSON")
            g.add_argument("--images-file", help="有序 JSON 数组，每项包含 image_url 或 file_id")
            g.add_argument("--edit-transport", choices=("multipart", "json"), help="本地图默认 multipart；URL/file_id 默认 JSON，不做失败后切换")
        masks = g.add_mutually_exclusive_group()
        masks.add_argument("--mask", help="编辑区域 PNG 蒙版；透明区域可修改")
        masks.add_argument("--mask-url", help="蒙版 HTTPS URL 或 Base64 Data URL；使用 JSON")
        masks.add_argument("--mask-file-id", help="已上传蒙版的 Files API ID；使用 JSON")
        g.add_argument("--session", help="本地版本会话 JSON；edit 可复用选中版本")
        g.add_argument("--from-run", help="复用运行清单中的图片")
        g.add_argument("--image-index", type=int, default=1)
        g.add_argument("--backend", choices=("images",), default="images", help="仅支持 GPT Image Images API")
        g.add_argument("--stream", action="store_true")
        g.add_argument("--partial-images", type=int)
        g.add_argument("--input-fidelity", help="原样传递服务商支持的输入保真参数")
        g.add_argument("--output-compression", type=int)
        g.add_argument("--moderation", help="原样传递服务商支持的审核参数")
        g.add_argument("--safety-identifier", help="传递用户提供的稳定匿名标识，不自动使用本机身份信息")
        g.add_argument("--user", help="已弃用的兼容用户标识；优先使用 safety-identifier")
        g.add_argument("--capabilities", help="本次使用的能力配置 JSON 文件")
        g.add_argument("--strict-capabilities", action="store_true", help="显式启用能力表拦截；默认只提示冲突")
        g.add_argument("--api-params-file", help="额外 API 参数 JSON 对象；原样传递，不覆盖已有标准选项")
        g.add_argument("--config")
        source = g.add_mutually_exclusive_group(required=True)
        source.add_argument("--prompt")
        source.add_argument("--prompt-file", help="UTF-8 文件；- 表示从标准输入读取")
        source.add_argument("--prompts-file", help="非空 JSON 字符串数组；每条提示词生成一张")
        g.add_argument("--base-url")
        g.add_argument("--model", help="原样传递中转站的模型 ID")
        g.add_argument("--n", type=int, default=1, help="总图片数，默认 1；按 Images API 每请求最多 10 张分批")
        g.add_argument("--request-mode", choices=("single", "separate"),
                       help="默认使用当前接口/模型的配置；single 每请求最多 10 张，separate 每请求 1 张；失败即停止")
        g.add_argument("--size")
        g.add_argument("--quality", help="例如 auto/low/medium/high，具体以中转站为准")
        g.add_argument("--background", help="例如 auto/opaque/transparent，原样传递")
        g.add_argument("--output-format", help="例如 png/jpeg/webp，原样传递；保存器识别这三种图片格式")
        g.add_argument("--timeout", type=float, help="每次网络操作的超时秒数，默认 180")
        g.add_argument("--output-dir", default="output/relay-imagegen")
    cap = sub.add_parser("capabilities", help="查看或保存模型能力声明，不发送生图请求")
    cap.add_argument("action", choices=("show", "set"))
    cap.add_argument("--model")
    cap.add_argument("--config")
    cap.add_argument("--file")
    hist = sub.add_parser("history", help="列出、选择、回退本地图片版本")
    hist.add_argument("action", choices=("list", "select", "undo", "import"))
    hist.add_argument("--session", required=True)
    hist.add_argument("--revision", type=int)
    hist.add_argument("--from-run", help="将已有运行清单导入本地历史，不调用 API")
    studio = sub.add_parser("studio", help="本地版本比较与涂抹蒙版界面；不提交生图")
    studio.add_argument("--session", required=True)
    studio.add_argument("--port", type=int, default=0)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        handlers = {"config": run_config, "models": run_models, "generate": run_generate, "edit": run_generate}
        if args.command == "capabilities":
            result = features().run_capabilities(args, api_module())
        elif args.command == "history":
            result = features().run_history(args, api_module())
        elif args.command == "studio":
            features()
            module = importlib.import_module("relay_" + args.command)
            result = module.run(args, api_module())
        else:
            result = handlers[args.command](args)
    except SkillError as exc:
        result = {"ok": False, "error": str(exc)}
    except (OSError, UnicodeError):
        result = {"ok": False, "error": "无法读取或写入本地文件；请检查路径、权限及 UTF-8 编码。"}
    except (EOFError, KeyboardInterrupt):
        result = {"ok": False, "error": "操作已取消。若生成请求已发出，请先检查服务端状态，避免重复计费。"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
