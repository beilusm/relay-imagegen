"""Offline behavioral tests; no real credentials or external API requests."""
import base64
from email.parser import BytesParser
from email.policy import default as email_policy
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zlib

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "relay-imagegen" / "scripts" / "relay_imagegen.py"
spec = importlib.util.spec_from_file_location("relay_imagegen", SCRIPT)
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


PNG = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0")) + chunk(b"IEND", b"")
ENCODED = base64.b64encode(PNG).decode()


@contextmanager
def server(mode="base64"):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def cloudflare_denial(self):
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "cloudflare_error": True, "error_code": 1010,
                "error_name": "browser_signature_banned",
                "detail": "Untrusted message containing sk-local-test-secret",
            }).encode())

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            if self.headers.get("Content-Type", "").startswith("multipart/form-data"):
                envelope = ("Content-Type: " + self.headers["Content-Type"] + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + raw
                message = BytesParser(policy=email_policy).parsebytes(envelope)
                body = {"_uploads": []}
                for part in message.iter_parts():
                    name = part.get_param("name", header="Content-Disposition")
                    content = part.get_payload(decode=True)
                    if part.get_filename():
                        body["_uploads"].append({"field": name, "filename": part.get_filename(), "content_type": part.get_content_type(), "data": content})
                    else:
                        body[name] = content.decode("utf-8")
                body["n"] = int(body["n"])
            else:
                body = json.loads(raw)
            calls.append(("POST", self.path, dict(self.headers), body))
            if mode == "cloudflare":
                self.cloudflare_denial()
                return
            if mode == "batch-fail-second" and sum(call[0] == "POST" for call in calls) == 2:
                self.send_response(503)
                self.end_headers()
                return
            if mode == "unauthorized":
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'{"error":"sk-local-test-secret"}')
                return
            if mode == "redirect":
                self.send_response(307)
                self.send_header("Location", "/redirected")
                self.end_headers()
                return
            if mode == "multi":
                data = [{"b64_json": ENCODED} for _ in range(body["n"])]
            elif mode == "multi-partial":
                data = [{"b64_json": ENCODED}, {"b64_json": "invalid!"}, {"b64_json": ENCODED}]
            elif mode == "url":
                data = [{"url": f"http://127.0.0.1:{self.server.server_port}/image-redirect"}]
            elif mode == "partial":
                data = [{"b64_json": ENCODED}, {"b64_json": "invalid!"}]
            elif mode == "data-uri":
                data = [{"b64_json": "data:image/png;base64," + ENCODED}]
            elif mode == "html":
                data = [{"b64_json": base64.b64encode(b"<html>not an image</html>").decode()}]
            else:
                data = [{"b64_json": ENCODED}]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"data": data}).encode())

        def do_GET(self):
            calls.append(("GET", self.path, dict(self.headers), None))
            if mode == "cloudflare":
                self.cloudflare_denial()
                return
            if self.path.endswith("/models"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                payload = {"data": [{"id": "image2.5"}, {"id": "text-model"}, {"id": "Custom-Image"}, {"id": "image2.5"}]}
                if mode == "bad-models":
                    payload = {"data": [{"name": "missing-id"}]}
                self.wfile.write(json.dumps(payload).encode())
                return
            if self.path == "/image-redirect":
                self.send_response(302)
                self.send_header("Location", "/image.png")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(PNG)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", calls
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "config.json"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("RELAY_IMAGE_")}
        self.env["NO_PROXY"] = "127.0.0.1,localhost"

    def configure(self, base):
        self.config.write_text(json.dumps({"base_url": base, "api_key": "sk-local-test-secret", "model": "image2.5"}))

    def cli(self, *args):
        proc = subprocess.run([sys.executable, str(SCRIPT), *args, "--config", str(self.config)], capture_output=True, text=True, env=self.env, timeout=10)
        self.assertNotIn("sk-local-test-secret", proc.stdout + proc.stderr)
        return proc.returncode, json.loads(proc.stdout)

    def generate(self, *args):
        return self.cli("generate", "--prompt", "雨夜橘猫 '$()' `literal`", "--output-dir", str(self.root / "out"), *args)

    def test_endpoint_rules(self):
        for base in ("https://example.test", "https://example.test/v1/", "https://example.test/v1/images/generations"):
            self.assertEqual(relay.generation_endpoint(base), "https://example.test/v1/images/generations")
        self.assertEqual(relay.generation_endpoint("https://example.test/gateway/v1"), "https://example.test/gateway/v1/images/generations")
        self.assertEqual(relay.generation_endpoint("https://example.test/api"), "https://example.test/api/v1/images/generations")
        self.assertEqual(relay.api_endpoint("https://example.test/api/v1/images/generations", "models"), "https://example.test/api/v1/models")
        self.assertEqual(relay.generation_endpoint("https://example.test/api/v1/models"), "https://example.test/api/v1/images/generations")
        self.assertEqual(relay.generation_endpoint("https://example.test/custom/images/generations"), "https://example.test/custom/images/generations")
        for base in ("ftp://example.test", "https://a:b@example.test", "https://example.test?key=secret", "https://example.test#frag"):
            with self.assertRaises(relay.SkillError):
                relay.generation_endpoint(base)

    def test_init_permissions_no_overwrite_and_status(self):
        code, result = self.cli("config", "init")
        self.assertEqual(code, 0)
        if os.name == "posix":
            self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.cli("config", "init")[0], 1)
        self.assertFalse(self.cli("config", "status")[1]["ready"])
        self.configure("https://example.test/v1")
        status = self.cli("config", "status")[1]
        self.assertTrue(status["ready"])
        self.assertTrue(status["api_key_configured"])
        self.assertNotIn("api_key", status)

    def test_environment_overrides_file(self):
        self.configure("https://example.test/v1")
        self.env.update(RELAY_IMAGE_BASE_URL="https://other.test/custom", RELAY_IMAGE_MODEL="custom-image")
        result = self.cli("config", "status")[1]
        self.assertEqual(result["endpoint"], "https://other.test/custom/v1/images/generations")
        self.assertEqual(result["model"], "custom-image")

    def test_base64_request_and_unique_files(self):
        with server() as (base, calls):
            self.configure(base)
            code, result = self.generate()
            self.assertEqual(code, 0)
            self.assertEqual(Path(result["files"][0]).read_bytes(), PNG)
            self.assertEqual(calls[0][1], "/v1/images/generations")
            self.assertEqual(calls[0][2]["Authorization"], "Bearer sk-local-test-secret")
            self.assertEqual(calls[0][3], {"model": "image2.5", "prompt": "雨夜橘猫 '$()' `literal`", "n": 1})
            second = self.generate()[1]
            self.assertNotEqual(second["files"], result["files"])
            self.assertTrue(Path(result["files"][0]).exists())

    def test_optional_fields_and_prompt_file(self):
        prompt = self.root / "prompt.txt"
        prompt.write_text("中文\n文字：你好", encoding="utf-8")
        with server() as (base, calls):
            self.configure(base + "/custom/v1")
            code, result = self.cli("generate", "--prompt-file", str(prompt), "--output-dir", str(self.root / "out"), "--model", "custom-model", "--size", "1024x1024", "--quality", "high", "--output-format", "png", "--background", "transparent", "--response-format", "b64_json")
            self.assertEqual(code, 0)
            self.assertEqual(calls[0][1], "/custom/v1/images/generations")
            self.assertEqual(calls[0][3]["prompt"], "中文\n文字：你好")
            self.assertEqual(calls[0][3]["model"], "custom-model")
            self.assertEqual(calls[0][3]["background"], "transparent")

    def test_url_download_and_redirect_never_send_auth(self):
        with server("url") as (base, calls):
            self.configure(base)
            code, result = self.generate()
            self.assertEqual(code, 0)
            self.assertEqual(Path(result["files"][0]).read_bytes(), PNG)
            self.assertEqual([c[0] for c in calls], ["POST", "GET", "GET"])
            for call in calls[1:]:
                self.assertNotIn("Authorization", call[2])

    def test_generation_redirect_is_not_followed(self):
        with server("redirect") as (base, calls):
            self.configure(base)
            code, result = self.generate()
            self.assertEqual(code, 1)
            self.assertIn("重定向", result["error"])
            self.assertEqual(len(calls), 1)

    def test_http_error_redacts_secret_and_never_retries(self):
        with server("unauthorized") as (base, calls):
            self.configure(base)
            code, result = self.generate()
            self.assertEqual(code, 1)
            self.assertIn("401", result["error"])
            self.assertEqual(len(calls), 1)

    def test_partial_result_keeps_successful_image(self):
        with server("partial") as (base, calls):
            self.configure(base)
            code, result = self.generate("--n", "2")
            self.assertEqual(code, 1)
            self.assertEqual(len(result["files"]), 1)
            self.assertEqual(Path(result["files"][0]).read_bytes(), PNG)
            self.assertEqual(len(result["errors"]), 1)
            self.assertEqual(len(calls), 1)

    def test_non_image_rejected(self):
        with server("html") as (base, calls):
            self.configure(base)
            code, result = self.generate()
            self.assertEqual(code, 1)
            self.assertEqual(result["files"], [])
            self.assertEqual(list((self.root / "out").glob("*.png")), [])
            self.assertEqual(len(list((self.root / "out").glob("run-*.json"))), 1)

    def test_data_uri(self):
        with server("data-uri") as (base, calls):
            self.configure(base)
            code, result = self.generate()
            self.assertEqual(code, 0)
            self.assertEqual(Path(result["files"][0]).read_bytes(), PNG)

    def test_count_mismatch_does_not_request_more(self):
        with server() as (base, calls):
            self.configure(base)
            code, result = self.generate("--n", "2")
            self.assertEqual(code, 1)
            self.assertEqual(len(result["files"]), 1)
            self.assertEqual(len(calls), 1)

    def test_invalid_parameters_rejected_before_request(self):
        with server() as (base, calls):
            self.configure(base)
            for args in (("--n", "0"), ("--timeout", "nan"), ("--size", "invalid"), ("--background", "transparent", "--output-format", "jpeg")):
                self.assertEqual(self.generate(*args)[0], 1)
            self.assertEqual(calls, [])

    def test_models_auth_filter_and_no_generation(self):
        with server() as (base, calls):
            self.configure(base)
            code, result = self.cli("models")
            self.assertEqual(code, 0)
            self.assertEqual(result["models"], ["Custom-Image", "image2.5", "text-model"])
            self.assertEqual(calls[0][1], "/v1/models")
            self.assertEqual(calls[0][2]["Authorization"], "Bearer sk-local-test-secret")
            code, result = self.cli("models", "--filter", "image")
            self.assertEqual(result["models"], ["Custom-Image", "image2.5"])
            self.assertEqual(result["total"], 3)
            self.assertEqual(result["count"], 2)
            self.assertTrue(all(c[0] == "GET" for c in calls))

    def test_models_from_prefix_and_full_generation_url(self):
        with server() as (base, calls):
            for suffix in ("/api", "/api/v1", "/api/v1/", "/api/v1/images/generations", "/api/v1/models"):
                self.configure(base + suffix)
                self.assertEqual(self.cli("models")[0], 0)
                self.assertEqual(calls[-1][1], "/api/v1/models")

    def test_models_does_not_need_selected_model(self):
        with server() as (base, calls):
            self.configure(base)
            data = json.loads(self.config.read_text())
            data["model"] = ""
            self.config.write_text(json.dumps(data))
            self.assertEqual(self.cli("models")[0], 0)

    def test_malformed_models_response_is_error(self):
        with server("bad-models") as (base, calls):
            self.configure(base)
            self.assertEqual(self.cli("models")[0], 1)
            self.assertEqual(len(calls), 1)

    def test_set_model_preserves_credentials_and_reports_env_override(self):
        self.configure("https://example.test")
        code, result = self.cli("config", "set-model", "custom-model")
        self.assertEqual(code, 0)
        self.assertEqual(result["effective_model"], "custom-model")
        stored = json.loads(self.config.read_text())
        self.assertEqual(stored["api_key"], "sk-local-test-secret")
        self.assertEqual(stored["base_url"], "https://example.test")
        self.env["RELAY_IMAGE_MODEL"] = "environment-model"
        code, result = self.cli("config", "set-model", "other-model")
        self.assertEqual(result["model"], "other-model")
        self.assertEqual(result["effective_model"], "environment-model")

    def test_cloudflare_model_denial_is_specific_and_does_not_retry(self):
        with server("cloudflare") as (base, calls):
            self.configure(base)
            code, result = self.cli("models")
            self.assertEqual(code, 1)
            self.assertIn("Cloudflare", result["error"])
            self.assertIn("1010", result["error"])
            self.assertIn("管理员", result["error"])
            self.assertNotIn("Untrusted", result["error"])
            self.assertEqual(len(calls), 1)

    def test_cloudflare_generation_denial_is_specific_and_does_not_retry(self):
        with server("cloudflare") as (base, calls):
            self.configure(base)
            code, result = self.generate()
            self.assertEqual(code, 1)
            self.assertIn("生成接口被 Cloudflare 拦截", result["error"])
            self.assertIn("1010", result["error"])
            self.assertEqual(len(calls), 1)

    def test_multiple_images_in_one_request_with_gallery_and_manifest(self):
        with server("multi") as (base, calls):
            self.configure(base)
            code, result = self.generate("--n", "4", "--size", "1024x1024")
            self.assertEqual(code, 0)
            self.assertEqual(result["requested_count"], 4)
            self.assertEqual(result["saved_count"], 4)
            self.assertEqual(result["attempted_requests"], 1)
            self.assertEqual(len(set(result["files"])), 4)
            self.assertEqual(calls[0][3]["n"], 4)
            self.assertEqual(len(calls), 1)
            for path in result["files"]:
                self.assertEqual(Path(path).read_bytes(), PNG)
            manifest = Path(result["manifest"]).read_text()
            self.assertNotIn("sk-local-test-secret", manifest)
            self.assertEqual(json.loads(manifest), result)
            gallery = Path(result["gallery"]).read_text()
            self.assertEqual(gallery.count("<figure>"), 4)
            for path in result["files"]:
                self.assertIn(Path(path).name, gallery)
            self.assertEqual([(im["width"], im["height"]) for im in result["images"]], [(1, 1)] * 4)
            self.assertEqual(len(result["warnings"]), 4)
            self.assertTrue(all("未缩放" in entry["warning"] for entry in result["warnings"]))

    def test_separate_requests_for_same_prompt(self):
        with server() as (base, calls):
            self.configure(base)
            code, result = self.generate("--n", "3", "--request-mode", "separate")
            self.assertEqual(code, 0)
            self.assertEqual(result["saved_count"], 3)
            self.assertEqual(result["planned_requests"], 3)
            self.assertEqual(result["attempted_requests"], 3)
            self.assertEqual([call[3]["n"] for call in calls], [1, 1, 1])
            self.assertEqual(len({call[3]["prompt"] for call in calls}), 1)
            self.assertEqual([im["request_index"] for im in result["images"]], [1, 2, 3])

    def test_distinct_prompts_batch_preserves_order_and_content(self):
        prompts = ["窗边橘猫", "雨中的城市", "海边日落"]
        source = self.root / "prompts.json"
        source.write_text(json.dumps(prompts, ensure_ascii=False))
        with server() as (base, calls):
            self.configure(base)
            code, result = self.cli("generate", "--prompts-file", str(source), "--output-dir", str(self.root / "out"))
            self.assertEqual(code, 0)
            self.assertEqual(result["saved_count"], 3)
            self.assertEqual([call[3]["prompt"] for call in calls], prompts)
            self.assertEqual([call[3]["n"] for call in calls], [1, 1, 1])
            self.assertEqual([entry["status"] for entry in result["requests"]], ["succeeded"] * 3)

    def test_batch_failure_preserves_first_image_and_stops_unsubmitted_work(self):
        with server("batch-fail-second") as (base, calls):
            self.configure(base)
            code, result = self.generate("--n", "3", "--request-mode", "separate")
            self.assertEqual(code, 1)
            self.assertEqual(len(calls), 2)
            self.assertEqual(result["saved_count"], 1)
            self.assertEqual(result["attempted_requests"], 2)
            self.assertEqual(result["unsubmitted_requests"], 1)
            self.assertEqual([entry["status"] for entry in result["requests"]], ["succeeded", "failed", "pending"])
            self.assertEqual(Path(result["files"][0]).read_bytes(), PNG)
            self.assertEqual(json.loads(Path(result["manifest"]).read_text()), result)

    def test_multi_partial_saves_other_images_and_reports_counts(self):
        with server("multi-partial") as (base, calls):
            self.configure(base)
            code, result = self.generate("--n", "3")
            self.assertEqual(code, 1)
            self.assertEqual(result["saved_count"], 2)
            self.assertEqual(result["requested_count"], 3)
            self.assertEqual(len(result["errors"]), 1)
            self.assertTrue(Path(result["gallery"]).exists())
            self.assertEqual(len(calls), 1)

    def test_entire_batch_validated_before_network(self):
        source = self.root / "prompts.json"
        with server() as (base, calls):
            self.configure(base)
            for value in (["valid", ""], [], ["a"] * 11, {"prompt": "not an array"}, ["valid", 1]):
                source.write_text(json.dumps(value))
                self.assertEqual(self.cli("generate", "--prompts-file", str(source), "--output-dir", str(self.root / "out"))[0], 1)
            source.write_text(json.dumps(["one", "two"]))
            self.assertEqual(self.cli("generate", "--prompts-file", str(source), "--n", "2", "--output-dir", str(self.root / "out"))[0], 1)
            self.assertEqual(calls, [])

    def test_image_dimensions_match_decoded_files(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Optional Pillow decoder is not installed")
        import io
        for fmt, extension, extra in (("PNG", "png", {}), ("JPEG", "jpg", {}), ("WEBP", "webp", {}), ("WEBP", "webp", {"lossless": True})):
            stream = io.BytesIO()
            Image.new("RGB", (37, 23), "orange").save(stream, format=fmt, **extra)
            self.assertEqual(relay.image_dimensions(stream.getvalue(), extension), (37, 23))

    def test_edit_uploads_single_reference_and_exact_text(self):
        reference = self.root / '素材 "一号".png'
        reference.write_bytes(PNG)
        with server() as (base, calls):
            self.configure(base)
            code, result = self.cli("edit", "--image", str(reference), "--prompt", "标题必须是：窗边的橘猫", "--output-dir", str(self.root / "out"))
            self.assertEqual(code, 0)
            self.assertEqual(result["operation"], "edit")
            self.assertEqual(calls[0][1], "/v1/images/edits")
            self.assertEqual(calls[0][3]["prompt"], "标题必须是：窗边的橘猫")
            self.assertEqual(calls[0][3]["_uploads"], [{"field": "image", "filename": "reference-01.png", "content_type": "image/png", "data": PNG}])
            self.assertEqual(result["references"][0]["path"], str(reference))
            self.assertEqual(result["references"][0]["width"], 1)
            manifest = Path(result["manifest"]).read_text()
            self.assertNotIn(ENCODED, manifest)
            self.assertNotIn("sk-local-test-secret", manifest)

    def test_edit_uploads_multiple_references_in_order_and_returns_multiple_images(self):
        references = [self.root / "one.png", self.root / "two.png"]
        for reference in references:
            reference.write_bytes(PNG)
        with server("multi") as (base, calls):
            self.configure(base + "/api")
            code, result = self.cli("edit", "--image", str(references[0]), "--image", str(references[1]), "--prompt", "图1为主体，图2为背景", "--n", "2", "--output-dir", str(self.root / "out"))
            self.assertEqual(code, 0)
            self.assertEqual(result["saved_count"], 2)
            self.assertEqual(calls[0][1], "/api/v1/images/edits")
            uploads = calls[0][3]["_uploads"]
            self.assertEqual([entry["field"] for entry in uploads], ["image[]", "image[]"])
            self.assertEqual([entry["filename"] for entry in uploads], ["reference-01.png", "reference-02.png"])
            self.assertEqual([entry["path"] for entry in result["references"]], [str(p) for p in references])

    def test_edit_json_transport(self):
        reference = self.root / "one.png"
        reference.write_bytes(PNG)
        with server() as (base, calls):
            self.configure(base)
            code, result = self.cli("edit", "--image", str(reference), "--edit-transport", "json", "--prompt", "制作封面", "--output-dir", str(self.root / "out"))
            self.assertEqual(code, 0)
            self.assertEqual(calls[0][2]["Content-Type"], "application/json")
            self.assertEqual(calls[0][3]["images"], [{"image_url": "data:image/png;base64," + ENCODED}])
            self.assertEqual(result["edit_transport"], "json")

    def test_invalid_reference_rejected_before_request(self):
        reference = self.root / "invalid.png"
        reference.write_text("not a picture")
        with server() as (base, calls):
            self.configure(base)
            for candidate in (reference, self.root / "absent.png"):
                code, result = self.cli("edit", "--image", str(candidate), "--prompt", "封面", "--output-dir", str(self.root / "out"))
                self.assertEqual(code, 1)
            self.assertEqual(calls, [])

    def test_reference_limits_checked_locally(self):
        reference = self.root / "one.png"
        reference.write_bytes(PNG)
        with self.assertRaises(relay.SkillError):
            relay.load_reference_images([str(reference)] * 17)
        with patch.object(relay, "MAX_IMAGE_BYTES", len(PNG) - 1):
            with self.assertRaises(relay.SkillError):
                relay.load_reference_images([str(reference)])
        with patch.object(relay, "MAX_REFERENCE_BYTES", len(PNG)):
            with self.assertRaises(relay.SkillError):
                relay.load_reference_images([str(reference)] * 2)

    def test_full_edit_endpoint_can_resolve_models_and_generation(self):
        base = "https://example.test/api/v1/images/edits"
        self.assertEqual(relay.api_endpoint(base, "models"), "https://example.test/api/v1/models")
        self.assertEqual(relay.api_endpoint(base, "images/edits"), base)
        self.assertEqual(relay.generation_endpoint(base), "https://example.test/api/v1/images/generations")

    def test_timeout_is_not_retried(self):
        with patch.object(relay.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = TimeoutError
            with self.assertRaises(relay.SkillError):
                relay.fetch(relay.urllib.request.Request("https://example.test"), 1, 100, generation=True)
            opener.return_value.open.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
