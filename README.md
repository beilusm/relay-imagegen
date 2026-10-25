# Relay ImageGen

**通过你自己的图片 API，在 Codex 对话或终端中生成图片、编辑素材、制作中文封面。**

生成、文字渲染、排版、合成和后续修改均由 image 模型完成。工具忠实传递用户提示词与参考图，原样保存模型输出。

## 模型能力

| 用途 | 示例 |
| --- | --- |
| 文生图 | “生成一张雨夜城市海报，蓝紫色调，不要文字。” |
| 多图方案 | “生成三张水彩插画，分别画橘猫、雪山和海边。” |
| 参考图编辑 | “使用这张产品图，保留主体，换成暖色摄影棚背景。”可传入多张参考图。 |
| 局部编辑 | 提供蒙版，描述选区内需要改变的内容。 |
| 中文封面与改字 | “标题逐字为‘我的周末计划’。”后续通过模型编辑修改文字和布局。 |

所有图片请求使用 GPT Image Images API，具体能力与参数支持取决于所配置的服务商和模型。支持尺寸、质量、透明背景、输出格式及流式预览等参数，详见 [API 覆盖与实测](docs/GPT_IMAGE_API_COVERAGE.md)。

## 安装与配置

需要 Python 3.9+、OpenAI 兼容图片 API 地址、API Key 和准确模型 ID。在 Codex 中使用还需环境支持加载本地技能、执行 Python 和访问图片文件。

下载本仓库并在项目目录执行（Windows 可将 `python3` 换为 `py -3`）：

```bash
python3 -m pip install -r requirements.txt
python3 tools/install.py
```

技能默认安装到 `~/.agents/skills/relay-imagegen`，安装后开启新的 Codex 会话。Pillow 用于蒙版校验和本地工作台；基础 API 调用仅需 Python 标准库。仅使用终端命令时可跳过技能安装。

在自己的交互式终端配置服务商地址、模型 ID 和密钥：

```bash
python3 skills/relay-imagegen/scripts/relay_imagegen.py config setup
python3 skills/relay-imagegen/scripts/relay_imagegen.py config status
```

API Key 输入时隐藏，不要贴到聊天或命令参数里。默认配置保存在 `~/.codex/relay-imagegen.json`；设置了 `CODEX_HOME` 时使用该目录。`ready: true` 仅表示字段齐全，查询模型列表可运行 `python3 skills/relay-imagegen/scripts/relay_imagegen.py models`。

## 在 Codex 中使用

> 使用 $relay-imagegen，生成一张窗边橘猫的水彩插画，左侧留白，不要文字。

> 使用 $relay-imagegen，把这张素材做成三款视频封面，标题是“我的周末计划”，分别给我三张独立图片。

> 使用 $relay-imagegen，继续修改第二张，保持主体和画风，把背景换成夜景。

编辑时提供素材或本地文件路径，标题给出完整原文。需要连续修改时说明“保存到同一个版本会话”。结果保存到本地并返回文件链接；批量默认附编号画廊、不做视觉检查，需要时可要求“检查第 2 张的文字”。单张任务默认验图。

## 在终端中使用

从项目根目录执行，生成一张图片并保存版本会话：

```bash
python3 skills/relay-imagegen/scripts/relay_imagegen.py generate \
  --prompt-file examples/prompt.txt \
  --session output/my-project/session.json \
  --output-dir output/my-project
```

同一提示词生成多张加 `--n 3`；不同提示词使用 `--prompts-file examples/prompts.json`。服务商需要逐张提交时，加 `--request-mode separate`。

编辑自己的素材：

```bash
python3 skills/relay-imagegen/scripts/relay_imagegen.py edit \
  --image /path/to/your-image.png \
  --prompt "保留主体，将背景换成温暖的摄影棚" \
  --session output/my-project/session.json \
  --output-dir output/my-project
```

将图片路径替换为真实文件，可重复 `--image` 传入多张参考图，局部编辑加 `--mask`。已有会话时，省略 `--image` 即可继续编辑当前选中版本。版本比较和蒙版涂抹见 [高级功能](skills/relay-imagegen/references/advanced.md)。

输出 JSON 的 `files` 给出图片路径。即使请求失败，也应检查已保存结果；工具不会自动重试或补足数量。生图和模型编辑可能产生服务商费用。

## 更多说明

- 用户提示词和模型原图原样传递、保存；改字或调整画面继续调用模型，不做本地叠字、合成、裁切或缩放。实际尺寸、格式与请求不符时会报告。
- 可选参数默认省略，新增字段可用 `--api-params-file` 传递；能力表默认仅提示冲突，显式 `--strict-capabilities` 才拦截。
- 更新技能：在新版项目目录运行 `python3 tools/install.py --replace`，旧版自动备份，API 配置保留；自定义安装位置使用 `--destination` 指定。

详细文档：[配置与排错](skills/relay-imagegen/references/configuration.md) · [高级功能](skills/relay-imagegen/references/advanced.md) · [视频封面](skills/relay-imagegen/references/video-covers.md) · [API 覆盖与实测](docs/GPT_IMAGE_API_COVERAGE.md)

本项目为第三方工具，图像生成与编辑由你配置的服务商提供。
