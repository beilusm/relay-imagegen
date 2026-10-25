# GPT Image Images API 覆盖与实测

核对日期：2026-10-24。覆盖对象是 GPT Image 的 `POST /v1/images/generations` 与 `POST /v1/images/edits`，不包括 Responses、聊天主模型、已退役模型的专用功能或通用 Files 管理。模型查询、版本选择、保存和蒙版制作只是 Images API 的辅助流程。

官方基准：[生成接口](https://developers.openai.com/api/reference/resources/images/methods/generate)、[编辑接口](https://developers.openai.com/api/reference/resources/images/methods/edit)。官方更新后需要重新核对；字段实现和中转站实际执行不能混为一谈。

## 请求与响应覆盖

| 官方功能/字段 | 插件实现 | 验证范围 |
| --- | --- | --- |
| `model`、`prompt` | 指定模型，提示词原样传递 | 离线验证；当前配置真实生成/编辑 |
| `n` | 总张数按每请求 1–10 张分批，失败即停 | 离线覆盖 11 张分为 10+1；真实 JSON 编辑的服务端数量异常已记录 |
| `size`、`quality` | 原样传递，包含 `max` 等新质量值和 `WIDTHxHEIGHT` | 离线验证；真实传入 `high/max` 与两种尺寸，服务端未按指定尺寸返回 |
| `background` | 透传 `auto/opaque/transparent` 等服务端取值 | 真实透明背景返回 RGBA，alpha 范围 0–255 |
| `output_format`、`output_compression` | PNG/JPEG/WebP 参数，JPEG/WebP 压缩率 | 离线验证；真实 WebP 请求实际返回 PNG，压缩率是否生效无法确认 |
| `moderation` | 原样传递，不另加审核/内容改写 | 真实传入 `auto/low`；本次安全测试内容不能验证其内容审核差异 |
| `safety_identifier`、兼容 `user` | 显式 CLI 参数，不自动采集本机身份 | 离线验证；匿名测试标识已真实传递，服务端是否采用无法确认 |
| `stream`、`partial_images` | SSE 生成/编辑、中间图即时保存、最终图与用量 | 真实收到 `image_edit.completed`；未返回中间图。两类事件及中间图处理已离线验证 |
| 本地图 `image/image[]` | multipart，原字节上传 | 真实蒙版流式编辑 |
| `images[].image_url` | HTTPS URL、Data URL、有序 JSON 引用数组 | Data URL 双参考图真实返回结果；HTTPS URL 引用仅离线验证 |
| `images[].file_id` | 引用当前服务商已有上传文件 | 离线验证；未上传或实测 Files ID，不声称远端已支持 |
| `mask` 本地/URL/Data URL/file ID | 本地 multipart 或 JSON 引用 | 本地 PNG 蒙版真实测试；远端引用离线验证 |
| `input_fidelity` | 编辑专用，可按所选模型传递，默认省略 | 离线验证；当前模型未实测此可选字段，不能从旧模型推断支持 |
| 响应及事件元数据 | 记录 `created`、`background`、`output_format`、`quality`、`size`、`safety_identifier`、`usage` 和预览序号等，剔除图片二进制并脱敏 | 原生响应与事件离线验证；真实流式用量已保存 |
| 后续新增参数 | `--api-params-file` 明确透传扩展字段；不能覆盖标准输入和数量 | JSON/multipart 离线验证 |

官方边界：提示词不超过 32,000 字符、最多 16 张输入图、单张 JSON Data URL 不超过 20 MiB。可配置的本地内存保护和模型能力表不会替代这些接口边界。`style`、`response_format` 是旧接口兼容字段，GPT Image 路径不提供对应 CLI 参数。没有像素级保留或全部参数组合的保证。

## 当前配置真实调用

使用现有配置模型 `gpt-image-2.5-sunburst`，未更改配置或模型。执行一次模型列表查询和三次图片请求；没有重试或补发。完整本地运行记录位于 `output/api-audit-20261024/`，不随源码分发。

| 请求 | 参数要点 | 实际结果 |
| --- | --- | --- |
| 模型列表 | 筛选 GPT Image | 列出 `gpt-image-2.5`、`gpt-image-2.5-flare`、`gpt-image-2.5-sunburst`；列表不构成能力验证 |
| 生成 1 张 | 1536×864、high、WebP、压缩 85、opaque | 成功返回 1 张 1672×941 RGB PNG；尺寸和格式与请求不符 |
| 本地参考图+蒙版流式编辑 | `stream=true`、`partial_images=1`、1536×864、PNG | 收到最终完成事件和用量，保存 1 张 1672×941 PNG；0 张中间图 |
| JSON 双参考图编辑 | `n=1`、1024×1024、max、transparent、WebP、压缩 85 | 返回 2 张 1254×1254 RGBA PNG，透明通道有效；数量、尺寸和格式异常，记录为非完全成功并保留两张原图 |

三次图像请求共保存四张结果。不能据此确认每个质量等级、精确位置、参考图融合、审核等级或所有参数组合的实际效果。插件覆盖了当前文档中的 GPT Image Images API 功能，但当前中转站不能标记为完整兼容；已发送的参数不等于服务端已执行。

## 后续批量失败记录

2026-10-24 另一次猫头鹰任务的已有清单显示：一个 `n=10` 请求报告网络失败/超时，另两个 `n=10` 请求返回 HTTP 422；之后相同模型、相同提示词的单张测试保存 1 张。随后逐张批次保存 3 张，在第 4 次单张请求收到 HTTP 502 后自行停止，5 次尚未提交。当前累计保存 4/10 张；本次修复没有停止该批次或追加图片请求。

旧脚本仅保留状态码，未记录 422 的结构化原因，因此不能确认具体触发字段或推断硬性 `max_n=1`。已新增安全错误详情，并把当前接口 `gpt-image-2.5-sunburst` 的生成提交偏好设为 `separate`；编辑和其他模型不受影响，显式 `--request-mode single` 可覆盖。此偏好不是能力限制，也不是失败后自动重试。

单张请求的 HTTP 502 说明逐张提交并不能消除接口异常；该失败请求的服务端执行/计费状态不能仅由本地清单确认。保留已保存结果与未提交清单，不因 5xx 自动补发或宣称模型无法生成图片。

已补充“改用单张测试”只生成一张后停止的技能规则，避免把测试请求扩展成恢复旧批量任务。
