# 局部编辑、版本、模型能力与可选后端

这些功能仍使用用户指定的中转站。模型能力声明与离线测试不证明远端支持；不要通过付费请求试探参数，也不要在失败后自动切换后端或补发。

## 蒙版局部编辑

```bash
python3 <skill-dir>/scripts/relay_imagegen.py edit \
  --image /absolute/path/original.png \
  --mask /absolute/path/mask.png \
  --prompt-file /absolute/path/edit.txt \
  --session /absolute/path/project/session.json
```

蒙版必须是可完整解码的 PNG，包含透明通道，与第一张参考图像素尺寸相同，且至少有一个非完全不透明像素。透明区域表示允许修改；不透明区域表示希望保留。程序在提交前校验，不改变原图。蒙版约束不能保证模型完全不改动其他区域，只有视觉检查后才能确认保留效果，检查方式遵循 [SKILL.md 的交付规则](../SKILL.md#在对话中交付)。蒙版功能依赖 Pillow。

默认 multipart 使用 `mask` 文件字段。显式 `--edit-transport json` 时发送 `mask.image_url` Data URL；只有中转站声明支持该 JSON 协议时才使用。

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

也可在 generate/edit 传 `--capabilities /path/capabilities.json` 临时覆盖。缺省字段表示未知；布尔 false、空枚举数组表示不支持。声明与请求冲突时在联网前拒绝。`max_n` 限制每次请求的图片数；用户显式选择 separate 时允许拆成单图请求。`max_reference_images` 限制参考图数量。

能力声明随配置保存，只有 `config status` 安全查看配置状态；不要打印包含 Key 的配置文件。`/models` 返回的名字不构成能力验证。

## 额外参数和用量

- `--output-format jpeg|webp --output-compression 0..100`：原样传递压缩参数，仅在显式 JPEG/WebP 时接受。
- `--input-fidelity low|high`：只给支持调节该参数的模型使用；默认不发送。有些模型固定高保真，不能发送此参数。
- `--moderation auto|low`：原样传递服务端支持的审核等级，不保证特定内容获准生成。
- `usage`、流式 `event_usage`、`revised_prompt`、Responses ID（接口提供时）保存到运行清单。接口未提供时不估算 token 或金额，不把多个不同口径的用量强行相加。
- 返回元数据中的配置 Key 会脱敏。原始服务端错误内容不会直接打印。

## 流式预览

```bash
python3 <skill-dir>/scripts/relay_imagegen.py generate \
  --prompt-file /path/prompt.txt --stream --partial-images 2
```

仅在中转站确认 SSE 协议后使用。`partial-images` 为 0–3，必须搭配 `--stream`；模型可能少返回或不返回中间图。预览保存为 `preview-*.png/jpg/webp`，并实时写入运行清单 `previews`；终端输出路径，代理可查看已经落盘的文件。

最终结果才进入 `files`。收到预览不代表成功。断流时保留预览与已收到的最终图片，停止剩余批次，不重试。不会将普通 JSON 响应静默当作流式结果，也不会失败后再发非流式请求。

## 显式 Responses 后端

先确认中转站支持 Responses 图像工具，以及主模型与图片模型组合。它们是两个不同模型参数，程序不替用户选择：

```bash
python3 <skill-dir>/scripts/relay_imagegen.py generate \
  --backend responses \
  --responses-model '中转站支持的主模型ID' \
  --model '中转站支持的图片模型ID' \
  --prompt-file /path/prompt.txt \
  --session /path/session.json
```

- `--model` 发给 `tools[].model`，`--responses-model` 发给顶层 `model`。
- 支持参考图、蒙版、流式预览及工具返回图片保存。Images API 的 `response-format` 不用于这个后端。
- 多图用 `--n N --request-mode separate`，或 `--prompts-file`；每次工具请求一张，不承诺服务端严格只返回一张。数量不符会报告，不补发。
- 默认 `store=false`。需要服务端会话留存时，显式加 `--responses-store`；可传 `--previous-response-id` 继续服务端上下文。
- 同一 session、相同 endpoint 和主模型，且父请求曾启用 store 时，启用 store 的后续编辑会复用选中父图对应的 response ID；同时上传选中图片。否则使用本地图像重新建立上下文。
- 不向新的主机迁移 response ID，不自动变更 API 协议、主模型或图片模型。
- 服务商未开放 Responses 时，现有 Images API 仍可用本地选图实现连续编辑。
