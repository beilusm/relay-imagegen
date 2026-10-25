# GPT Image 编辑、参数与版本

这些功能仍使用用户指定的中转站 GPT Image Images API。模型能力声明与离线测试不证明远端支持；不要通过付费请求试探参数，也不要在失败后自动切换协议或补发。

## 蒙版局部编辑

```bash
python3 <skill-dir>/scripts/relay_imagegen.py edit \
  --image /absolute/path/original.png \
  --mask /absolute/path/mask.png \
  --prompt-file /absolute/path/edit.txt \
  --session /absolute/path/project/session.json
```

蒙版必须是可完整解码的 PNG，包含透明通道，与第一张参考图像素尺寸相同，且至少有一个非完全不透明像素。透明区域表示允许修改；不透明区域表示希望保留。程序在提交前校验，不改变原图。蒙版约束不能保证模型完全不改动其他区域，只有视觉检查后才能确认保留效果，检查方式遵循 [SKILL.md 的交付规则](../SKILL.md#在对话中交付)。蒙版功能依赖 Pillow。

本地素材默认 multipart，使用 `mask` 文件字段。JSON 编辑支持 `mask.image_url`（URL/Data URL）或 `mask.file_id`；不能同时设置。

## 本地版本会话

给 generate/edit 添加 `--session /absolute/path/session.json` 即可记录每次成功保存的图片。一次多图默认选中此次第一张；失败时已成功保存的图片也进入历史。预览图不算正式版本。

```bash
python3 <skill-dir>/scripts/relay_imagegen.py history import --session /path/session.json --from-run /path/old-run.json
python3 <skill-dir>/scripts/relay_imagegen.py history list --session /path/session.json
python3 <skill-dir>/scripts/relay_imagegen.py history select --session /path/session.json --revision 2
python3 <skill-dir>/scripts/relay_imagegen.py edit --session /path/session.json --prompt-file /path/change.txt
python3 <skill-dir>/scripts/relay_imagegen.py history undo --session /path/session.json
```

- `history import` 导入旧运行记录，不调用接口；同一清单和图片重复导入不产生重复版本。
- 编号从 1 开始。edit 未给 `--image` 时使用当前选中版本。
- `undo` 返回当前版本的父图；不删除图片或丢弃分支。
- `edit --from-run /path/run-….json --image-index 2` 可从任意已有运行记录继续。附加的 `--image` 放在选中原图之后。
- 使用手动 `--image` 开始的新编辑不自动推断父图；需要版本关系时使用会话选中图或 `--from-run`。
- 不改写旧清单和原图。会话记录依赖绝对路径，移动项目后需同步更新路径。
- 会话有 `.lock` 锁防止并发写入。进程被强制杀死后可能留下锁；先确认该进程已经停止，再移除遗留锁，不能在请求仍运行时强行解锁。
- 超时/中断的请求仍可能已计费。版本管理不会重新提交旧请求。

## 本地工作台

```bash
python3 <skill-dir>/scripts/relay_imagegen.py studio --session /path/session.json
```

终端输出带随机令牌的本地地址。打开后可并排比较版本、选择当前版本、调节画笔、涂抹/擦除区域、撤销笔画、保存或下载 PNG 蒙版。保存后显示原运行记录、图片编号和蒙版路径，可用于下一次 edit。

工作台仅监听 `127.0.0.1`，不读取 API Key，不提交生成请求；需要 Pillow。不要把令牌地址公开。Ctrl+C 关闭。图片和蒙版通过本地服务读取，原图保持不变。当前工作台用于本地比较与蒙版制作，并非官方 Canvas。

## 模型能力配置

能力声明按精确模型 ID 保存，不按名字猜测后端型号。先查看：

```bash
python3 <skill-dir>/scripts/relay_imagegen.py capabilities show --model '实际模型ID'
```

用户或服务商确认后，写一个 JSON 文件，例如：

```json
{
  "backends": ["images"],
  "edit": true,
  "mask": true,
  "stream": false,
  "max_n": 1,
  "max_reference_images": 4,
  "sizes": ["auto", "1024x1024", "1536x1024"],
  "quality": ["auto", "low", "medium", "high"],
  "background": ["auto", "opaque"],
  "output_format": ["png", "jpeg", "webp"],
  "output_compression": true,
  "input_fidelity": ["high"],
  "edit_transports": ["multipart"]
}
```

这只是格式示例，不是任何模型的官方能力表。仅填写已确认字段。

```bash
python3 <skill-dir>/scripts/relay_imagegen.py capabilities set --model '实际模型ID' --file /path/capabilities.json
```

也可在 generate/edit 传 `--capabilities /path/capabilities.json` 临时覆盖。缺省字段表示未知；布尔 false、空枚举数组表示不支持。声明与请求冲突时默认记录警告，仍按用户参数提交，交给服务端判断。只有用户明确要求能力表作为硬性限制时，添加 `--strict-capabilities` 才会在联网前拒绝。`max_n` 记录每次请求的图片数上限（仅严格模式拦截）；用户显式选择 separate 时允许拆成单图请求。`max_reference_images` 记录参考图数量上限（仅严格模式拦截）。能力表不会覆盖官方 API 自身边界：每请求最多 10 张输出、16 张参考图。

旧能力表可能过时，不能据此认定新模型没有能力，也不能据此改写提示词或更换参数。能力声明随配置保存，只有 `config status` 安全查看配置状态；不要打印包含 Key 的配置文件。`/models` 返回的名字不构成能力验证。

## 额外参数和用量

`--api-params-file /path/params.json` 将服务商新增参数的 JSON 对象原样加入 Images 请求。嵌套对象/数组在 multipart 中按 JSON 编码。不接受覆盖 model、prompt、n、参考图、蒙版或已有标准选项；标准选项直接用 CLI 设置。插件不会根据固定参数白名单删除未知字段。不要将凭据放入此文件，参数会记录在运行清单。

尺寸、质量、背景、输入保真和审核等值默认不发送，也不靠固定枚举限制服务商新增取值；能否使用由所选接口判断。保存器目前识别 PNG/JPEG/WebP，这属于本地解码能力，不代表模型仅支持这些格式。

本地内存保护默认单张 32 MiB、参考图总计 64 MiB、单个 JSON 响应/流式事件 128 MiB。需要更大输入/输出时设置正数环境变量 `RELAY_IMAGE_MAX_IMAGE_MIB`、`RELAY_IMAGE_MAX_REFERENCE_MIB`、`RELAY_IMAGE_MAX_RESPONSE_MIB`；流式总量保护为响应值的四倍。这些值是可调整的资源保护，不是模型能力限制，不会自动压缩、裁切或缩小图片。

- `--output-format jpeg|webp --output-compression 0..100`：原样传递压缩参数，仅在显式 JPEG/WebP 时接受。
- `--input-fidelity low|high`：只给支持调节该参数的模型使用；默认不发送。有些模型固定高保真，不能发送此参数。
- `--moderation auto|low`：原样传递服务端支持的审核等级，不保证特定内容获准生成。
- `usage`、流式 `event_usage`、响应元数据和 `revised_prompt`（接口提供时）保存到运行清单。接口未提供时不估算 token 或金额。
- 返回元数据中的配置 Key 会脱敏。原始服务端错误内容不会直接打印。

## 流式预览

```bash
python3 <skill-dir>/scripts/relay_imagegen.py generate \
  --prompt-file /path/prompt.txt --stream --partial-images 2
```

仅在中转站确认 SSE 协议后使用。`partial-images` 为 0–3，必须搭配 `--stream`；模型可能少返回或不返回中间图。预览保存为 `preview-*.png/jpg/webp`，并实时写入运行清单 `previews`；终端输出路径，代理可查看已经落盘的文件。

最终结果才进入 `files`。收到预览不代表成功。断流时保留预览与已收到的最终图片，停止剩余批次，不重试。不会将普通 JSON 响应静默当作流式结果，也不会失败后再发非流式请求。

## 官方 GPT Image 参数

`generate` 和 `edit` 直接对应官方 Images API。支持 `model`、`prompt`、`background`、`moderation`、`n`、`output_compression`、`output_format`、`partial_images`、`quality`、`safety_identifier`、`size`、`stream`，并兼容已弃用的 `user`。编辑另支持 `images`、`mask` 和 `input_fidelity`。GPT Image 不使用旧式 `style` / `response_format`，风格写在用户提示词中，图片默认 Base64 返回。

## URL、Data URL 与文件 ID

本地图默认 multipart。使用 `--image-url https://...`、`--image-file-id file-...`、`--mask-url https://...` 或 `--mask-file-id file-...` 时自动选择官方 JSON 编辑请求，不下载/重绘素材，不在请求失败后切换协议。Files API ID 必须来自当前服务商已有的上传，本插件不实现通用文件管理。

`--images-file /path/images.json` 按数组原顺序提交引用，例如：

```json
[{"image_url":"https://example.com/reference.png"},{"file_id":"file-existing-image"}]
```

需要精确混合顺序时使用此数组；单独参数组合按本地图、URL、文件 ID、数组顺序组合。每项只能指定 `image_url` 或 `file_id`，不能同时设置。蒙版同样三选一：本地 `--mask`、`--mask-url`、`--mask-file-id`。远端图片无法本地核验尺寸或蒙版透明度时，由 API 校验，不能声称已验证。

## 官方边界与兼容性

截至 2026-10-24，官方接口每次 `n=1..10`、最多 16 张输入图片、提示词最多 32,000 字符，单张 Data URL 最长 20 MiB。总张数超过 10 时自动按 10 张分批，最后一批仅提交剩余数量，失败即停止。资源保护可调整，但不会绕过 API 自身边界。

原生响应中的创建时间、尺寸、格式、质量、背景、安全标识及用量保存到请求的 `response_metadata`；流式事件元数据和预览序号同时保留。实际文件规格与请求不符时发出警告，不本地转换成看似合规的结果。参数已发送并不证明服务端执行了该参数。

官方参考：[生成](https://developers.openai.com/api/reference/resources/images/methods/generate)、[编辑](https://developers.openai.com/api/reference/resources/images/methods/edit)。
