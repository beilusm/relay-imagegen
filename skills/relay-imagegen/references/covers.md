# 通用本地封面排版

当文字必须逐字准确、logo/产品应使用原素材、或需要多尺寸导出时，调用 `cover`；不需要 API Key，不发网络请求。依赖 Pillow。文字框、素材位置应由代理结合图片和用户要求确定，不强制套用模板。

```bash
python3 <skill-dir>/scripts/relay_imagegen.py cover \
  --spec /absolute/path/cover-spec.json \
  --output-dir /absolute/path/deliverables
```

示例配方：

```json
{
  "background": "background.png",
  "sizes": ["1280x720", "1920x1080"],
  "formats": ["png", "jpeg"],
  "safe_margin": 0.04,
  "crop_focus": [0.5, 0.5],
  "jpeg_quality": 94,
  "texts": [
    {
      "text": "猫的周末",
      "x": 0.06, "y": 0.1, "width": 0.6, "height": 0.25,
      "font_size": 96, "min_font_size": 48,
      "fill": "#ffffff", "stroke_width": 3, "stroke_fill": "#182439",
      "shadow": {"dx": 3, "dy": 5, "fill": "#172135"},
      "align": "left", "spacing": 8
    },
    {
      "text": "慢下来，发现生活的小美好",
      "x": 0.065, "y": 0.38, "width": 0.8, "height": 0.15,
      "font_size": 38, "min_font_size": 24,
      "fill": "#fce7bd", "stroke_width": 1
    }
  ],
  "overlays": []
}
```

路径相对于配方所在目录解析，也可使用绝对路径。`font` 可在顶层或文字图层指定本地 TTF/OTF/TTC 文件。缺省优先使用常见安装路径下的 Noto Sans CJK，再退回 DejaVu Sans；找不到字体或发现缺字时停止，需指定字体，不用方框代替中文字。

## 布局规则

- `x`、`y`、`width`、`height` 为画布的 0–1 比例，位置代表左上角。图层必须完全在画面内。
- 背景等比裁切到目标尺寸；`crop_focus` 是 0–1 范围的水平、垂直裁切重心。它不是主体自动识别；未做视觉检查时不能保证没有裁掉重要部分。
- `safe_margin` 默认 0.04，约束所有文字框；文字框超出时在输出前报错。
- `font_size`、`min_font_size`、`stroke_width`、`spacing`、阴影 dx/dy 以画布高 720 为基准按比例缩放。
- 保留 `text` 原文和显式换行。长行按字符自动换行，字号逐步缩小；最小字号仍装不下时报错，不裁字、不省略原文。英文需要词组断行时可显式加换行。
- `align` 支持 left/center/right；支持文字颜色、描边和偏移阴影。
- `overlays` 可含 `{ "image": "logo.png", "x": 0.8, "y": 0.08, "width": 0.12, "height": 0.12, "opacity": 1 }`。原始 RGBA 图按比例放入框内、居中合成，不拉伸；透明边缘保留。需要抠图时应先准备透明素材，此命令不提供 AI 自动抠图。
- 图层顺序为背景→按数组顺序的图片图层→按数组顺序的文字图层。文字始终在图片图层上方。
- 支持 1–10 个不重复尺寸和 PNG/JPEG/WebP。最大边 8192、单图 3200 万像素、单批总计 6400 万像素。格式和尺寸完全相同的配方重复运行会生成不同文件名，不覆盖结果。

## 可重现交付

每次生成 `cover-<id>.json`，包含原配方、实际文字框与字号、素材/字体路径及 SHA-256。按用户要求修改原配方重新运行即可改变标题、颜色或位置，不需要再次生成背景。

输出目录中的文件均是本地排版结果，不是接口原生输出。验图遵循 [SKILL.md 的交付规则](../SKILL.md#在对话中交付)：单张默认检查主体裁切、文字、层级、边距以及缩略图可读性；批量默认核对文件和尺寸并注明未做视觉检查，用户要求验图时每次只打开一张，查看失败后停止后续验图。一个配方支持多种比例，但横竖版通常需要不同构图，必要时分别编写配方。
