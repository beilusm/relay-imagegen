# 配置与排错

## 首次填写

推荐在自己的终端执行（下面使用安装器的默认路径；如果使用旧安装位置或自定义位置，请替换为实际 skill 目录）：

```bash
python3 ~/.agents/skills/relay-imagegen/scripts/relay_imagegen.py config setup
```

它会询问 Base URL、模型名及 API Key，Key 不回显。也可以执行 `config init`，然后在编辑器中填写默认配置文件 `~/.codex/relay-imagegen.json`：

```json
{
  "base_url": "https://relay.example.com/v1",
  "api_key": "在本地填入中转站的 API Key",
  "model": "image2.5",
  "timeout": 180
}
```

上面的域名与 Key 是占位示例，不能直接调用。`model` 必须与中转站控制台中的模型 ID 完全一致。若中转站使用其他别名，把它填入 `model`，不需要修改脚本。

配置在 skill 文件夹之外，避免分享 skill 时附带 Key。脚本创建的配置文件在 POSIX 系统上权限为 `0600`，Key 以明文保存在本机；手工新建时也应限制文件访问。`config init` 不覆盖已有文件，`config setup` 交互更新已有配置。

也可由当前运行 Codex 的进程继承以下环境变量；环境变量优先于文件：

| 变量 | 用途 |
| --- | --- |
| `RELAY_IMAGE_BASE_URL` | 中转站 API 前缀或完整生成接口 |
| `RELAY_IMAGE_API_KEY` | 中转站 Key；通过本机安全输入/秘密管理工具注入 |
| `RELAY_IMAGE_MODEL` | 模型 ID |
| `RELAY_IMAGE_CONFIG` | 配置文件路径 |

联网及能力配置子命令的 `--config /path/to/config.json` 优先于 `RELAY_IMAGE_CONFIG`；生成命令的 `--base-url`、`--model`、`--timeout` 优先于配置及环境。没有 `--api-key` 参数，避免密钥出现在进程命令行。

## Base URL 如何拼接

| 填写值 | 实际 POST 地址 |
| --- | --- |
| `https://relay.example.com` | `https://relay.example.com/v1/images/generations` |
| `https://relay.example.com/v1/` | `https://relay.example.com/v1/images/generations` |
| `https://relay.example.com/api` | `https://relay.example.com/api/v1/images/generations` |
| `https://relay.example.com/api/v1` | `https://relay.example.com/api/v1/images/generations` |
| `https://relay.example.com/v1/images/generations` | 原样使用 |

域名或路径前缀末尾没有 `/v1` 时自动添加，已经带 `/v1` 时不重复添加。填写完整 `/images/generations`、`/images/edits` 或 `/models` 地址时，保留用户明确指定的 API 前缀。查询模型会使用相同前缀下的 `/models`，例如只填 `https://relay.example.com` 时查询 `https://relay.example.com/v1/models`。Base URL 不接受 URL 内嵌密码、查询参数或片段；通常应使用 HTTPS。本地测试允许 HTTP。生成接口不跟随重定向，以免把凭据发送到另一个主机；遇到重定向时将配置改为实际接口。

## 获取并选择模型

配置 Base URL 与 Key 后，在对话中说“使用 $relay-imagegen 获取中转站模型列表”，或自己执行：

```bash
python3 ~/.agents/skills/relay-imagegen/scripts/relay_imagegen.py models
python3 ~/.agents/skills/relay-imagegen/scripts/relay_imagegen.py models --filter image
```

命令发送 `GET /v1/models`，自动补齐 `/v1`，读取 `data[].id`，不会生成图片。`--filter image` 只按名字筛选，不保证筛选结果就是全部图片模型；不同服务商的命名可能不同。模型列表也可能包含聊天/语音等模型，需选用支持 Images API 的模型。

从结果中选定实际模型 ID 后保存，例如：

```bash
python3 ~/.agents/skills/relay-imagegen/scripts/relay_imagegen.py config set-model '你选择的模型ID'
```

若你设置了 `RELAY_IMAGE_MODEL`，它仍会覆盖文件中的模型；命令会分别返回已保存的 `model` 和实际生效的 `effective_model`。模型列表接口若返回 404/403，说明需检查该站的路径/权限或其是否提供标准模型列表；这不代表一定无法生图。不会通过付费生成请求来猜测模型。

## 直接测试一次生成

填好配置后，在 Codex 对话中输入：

> 使用 $relay-imagegen 生成一张雨夜未来城市的海报，蓝紫色调，无文字。

也可在终端执行以下命令（会提交真实生成请求并可能计费）：

```bash
python3 ~/.agents/skills/relay-imagegen/scripts/relay_imagegen.py config status
python3 ~/.agents/skills/relay-imagegen/scripts/relay_imagegen.py generate \
  --prompt '一只橘猫坐在窗边，水彩插画' \
  --output-dir ./output/relay-imagegen
```

如果本机使用其他 `CODEX_HOME` 或安装路径，替换上面的脚本路径。脚本收到一份生成响应后保存所有有效图片，以时间戳和随机后缀命名，避免覆盖。成功返回 `ok: true` 和 `files`；失败返回 `ok: false`，并可能包含已保存的 `files`。`config status` 不发送网络请求。

## 多图用法

同一提示词生成 4 张独立图片，向中转站提交一次 `n=4` 的请求：

```bash
python3 ~/.agents/skills/relay-imagegen/scripts/relay_imagegen.py generate \
  --prompt '窗边橘猫，温暖午后，水彩插画' --n 4 \
  --output-dir ./output/cat-variants
```

若已确认服务商一次请求只支持一张，可以在开始时选择单图请求模式（总共生成 4 张，依次提交 4 次）：

```bash
python3 ~/.agents/skills/relay-imagegen/scripts/relay_imagegen.py generate \
  --prompt '窗边橘猫，温暖午后，水彩插画' --n 4 --request-mode separate
```

不同内容用 JSON 文件，例如 `prompts.json`：

```json
[
  "窗边橘猫，温暖午后，水彩插画，无文字",
  "雪山与松林，柔和日出，水彩插画，无文字",
  "海边小屋，夕阳余晖，水彩插画，无文字"
]
```

```bash
python3 ~/.agents/skills/relay-imagegen/scripts/relay_imagegen.py generate \
  --prompts-file ./prompts.json --output-dir ./output/watercolors
```

`--prompts-file` 接受非空字符串数组，不设置固定条数上限；每条各生成一张，不与 `--n` 叠加。整份文件会在发送任何请求前校验。`--prompt`、`--prompt-file`、`--prompts-file` 三者互斥。

进度输出到 stderr，最终 JSON 输出到 stdout。每次运行另外保存 `run-*.json`，内容包括提示词、参数、各请求状态、原始图片路径、实际尺寸和错误；记录 Images API 接口地址，但不记录 Key 或 Authorization。提示词仍属于本地数据，分享结果清单前应自行确认内容适合分享。

保存两张及以上图片时，生成 `gallery-*.html` 编号预览页，可在本机浏览器中打开；原图仍是各自独立的文件。原图尺寸与 `--size` 不一致时返回警告，不悄悄缩放。清单中 `saved_count` 表示保存数，`requested_count` 表示请求数；保存数不代表已完整解码或通过视觉检查；验图遵循 [SKILL.md 的交付规则](../SKILL.md#在对话中交付)，批量默认不加载图片到对话。

任一请求失败后停止发送剩余请求，保留已完成文件，返回非零退出码及 `unsubmitted_requests`。不自动重试、不自动补足张数、不自动切换单次/多次模式。每个请求最多只提交一次；如需重新执行，应先看清单判断已完成与未提交部分，避免整批重复。

## 故障排查

- **403 / Cloudflare Error 1010**：站点按客户端标识阻止访问，不能据此判断 Key 是否有效。联系中转站管理员调整 API 访问规则；不反复重试，也不把此错误当成 `/v1` 拼接失败。
- **401 / 其他 403**：检查 Key、账号权限和中转站限制。不要将 Key 粘贴到排错输出。
- **404**：确认这是 API 地址，确认是否需要 `/v1`，确认模型是否在此接口可用。
- **400**：确认模型 ID，以及是否支持请求中的 size、quality、background、output_format 等参数。默认请求只带 model、prompt、n。不要为试探参数自动反复提交。
- **422**：请求未通过接口校验。查看脱敏后的 `param/message` 或 `detail[].loc/msg`，不要把本地权限提升当作参数修复。未返回结构化详情时明确说明原因未知，不推断 `n` 的具体上限。
- **429**：检查额度或限流。脚本不自动重试。
- **超时 / 5xx**：服务端可能已经开始生成；先查看中转站记录，再由用户决定是否重试。调整 `timeout` 只影响后续请求。
- **返回网页 / 缺少 data**：可能填了控制台地址，或站点不兼容 GPT Image Images API。默认需要兼容 Images API 的非流式响应；流式需预先确认支持并显式启用。
- **图片链接下载失败**：不会自动重新生成。已成功保存的图片仍可交付；失败链接可能需要中转站排查，脚本不会将 Key 转发给图片 CDN。
- **图片数量不足 / 部分数据无效**：报告现有结果，不补发生成请求。

多图 `n>1` 失败而 `n=1` 已成功时，可为当前接口/模型保存逐张提交偏好，避免后续每次都先碰一次失败：

```bash
python3 <skill-dir>/scripts/relay_imagegen.py config set-request-mode separate --model '实际模型ID'
```

默认只设置生成接口。编辑接口加 `--operation edit` 单独设置；不同接口和不同模型互不影响。`config status` 显示 `generation_request_mode` / `edit_request_mode`。调用时 `--request-mode single` 明确覆盖；`config set-request-mode auto --model '实际模型ID'` 清除偏好。该设置仅改变提交批次，不限制模型质量/画面能力，也不会把已失败或状态不明的请求自动补发。

HTTP/流式错误只展示结构化错误中已脱敏的 code、type、param、message 或校验字段，不输出原始 HTML、验证 input/ctx、Authorization 或配置 Key。旧清单若只保存了 HTTP 状态，无法恢复当时已被丢弃的错误体。

本地内存保护默认单张图片 32 MiB，生成 JSON 响应 128 MiB；可用 `RELAY_IMAGE_MAX_IMAGE_MIB` 和 `RELAY_IMAGE_MAX_RESPONSE_MIB` 调高，不代表模型能力上限。保存器识别 PNG/JPEG/WebP。Python 的 timeout 作用于网络阻塞操作，不是整个生成流程的总时限。当前支持单图/多图文生图与参考图编辑；支持蒙版和 SSE 流式事件；不支持非标准异步任务轮询。参考图上传的实际可用性取决于中转站的 `/images/edits` 支持。

接口形状参考 OpenAI 官方 Images API 文档：
https://developers.openai.com/api/reference/resources/images/methods/generate

中转站的模型命名、额外参数、计费和实际能力仍以该服务商为准。

模型列表接口参考：
https://developers.openai.com/api/reference/resources/models/methods/list

## 素材图与封面

使用 `edit` 上传参考图，多个素材重复 `--image`：

```bash
python3 ~/.agents/skills/relay-imagegen/scripts/relay_imagegen.py edit \
  --image ./subject.png --image ./background.jpg \
  --prompt-file ./cover-prompt.txt --n 2 --output-dir ./output/covers
```

提示词忠实传递用户意图，必要时说明图1/图2的用途，并保留用户指定的主体、标题原文和构图。不要为满足模板擅自追加限制。`edit` 的数量、清单、尺寸警告和错误处理与 `generate` 相同；继续修改时把上次选定图作为新的 `--image` 输入。

默认请求类型为 multipart/form-data。一张图用 `image` 字段，多张用 `image[]`；上传文件名由脚本生成。URL/Data URL 和 Files API ID 输入自动使用 JSON；也可显式指定 `--edit-transport json`。用 `--image-url`、`--image-file-id` 或有序的 `--images-file` 指定引用，蒙版用 `--mask-url` / `--mask-file-id`。脚本不在失败后自动切换协议。编辑接口不可用时如实说明，不能退回纯文字生图却声称已使用素材。

参考图要求：GPT Image Images API 最多 16 张。单张默认 32 MiB、合计默认 64 MiB 的本地内存保护可用 `RELAY_IMAGE_MAX_IMAGE_MIB` / `RELAY_IMAGE_MAX_REFERENCE_MIB` 调整；单张 Data URL 遵循接口 20 MiB 限额。源图片原字节上传，模型结果原字节另存；清单仅记录素材路径、尺寸和哈希。

蒙版、版本记录、能力配置、参数透传和流式详见 [advanced.md](advanced.md)。视频封面和文字排版流程见 [video-covers.md](video-covers.md)。文字渲染、改字、合成和排版全部交给模型，不使用本地工具修图。API 调用脚本只使用 Python 标准库；Pillow 仅用于蒙版校验和工作台。

编辑接口参考：
https://developers.openai.com/api/reference/resources/images/methods/edit
