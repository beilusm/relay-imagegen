# 开发与维护

本文面向维护者。面向使用者的安装、配置与操作说明放在根目录 `README.md`；不要把测试数量、架构说明、验收过程或发布步骤添加到用户 README。

## 仓库布局

```text
relay-imagegen/
├── README.md                       # 用户入口
├── requirements.txt                # 可选图片处理依赖 Pillow
├── skills/relay-imagegen/           # 可独立安装、打包的技能
│   ├── SKILL.md                    # 代理调用说明
│   ├── agents/openai.yaml          # 技能显示信息
│   ├── scripts/
│   │   ├── relay_imagegen.py       # CLI、配置、Images 请求、输出记录
│   │   ├── relay_features.py       # 蒙版、SSE、Responses、能力和版本
│   │   ├── relay_cover.py          # 本地排版与多尺寸导出
│   │   └── relay_studio.py         # 本地工作台 HTTP 服务
│   ├── assets/studio.html          # 工作台页面
│   └── references/                 # 详细功能与操作约束
├── examples/                       # 无凭据、可复用的提示词和封面配方
├── tests/                          # 离线行为测试；不随技能分发
├── tools/                          # 安装和打包工具
├── docs/
│   ├── DEVELOPMENT.md              # 本文
│   └── assets/                     # 已去除本机路径的用户展示图
├── output/                         # 本地输出，Git 忽略
└── dist/                           # 生成的安装包，Git 忽略
```

运行时代码留在技能内部，安装后不依赖仓库外的 tests/tools/examples。不要在技能中引用本机绝对路径。`output/` 中旧验收材料属于本地历史，不作为用户文档或仓库交付内容。

## 本地环境与测试

支持 Python 3.9+。API 调用只用标准库；蒙版、封面和工作台按需加载 Pillow。测试封面时需要中文字体和 DejaVu Sans；Ubuntu 可安装 `fonts-noto-cjk`、`fonts-dejavu-core`。其他系统可安装相同字体或调整字体测试的夹具。

```bash
python3 -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell 使用 .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m unittest discover -s tests -p 'test_*.py'
python -m compileall -q skills/relay-imagegen/scripts tools
```

测试使用 loopback HTTP 服务和临时文件，不需要真实配置和 API Key，不提交付费请求。沙箱禁止监听本地端口时，网络模拟测试会失败，需要在允许 loopback 的环境运行；不要用真实中转站替代测试服务。

- `test_relay_imagegen.py`：URL、配置、模型查询、单图/批量、失败保留、重定向、参考图传输等。
- `test_relay_features.py`：蒙版、参数能力、SSE 中间图即时保存、Responses、会话、封面和工作台访问控制。
- `test_repository_tools.py`：安装拒绝覆盖、备份更新、链接处理、安装包内容和可重复打包。

修改界面后需另做实际浏览器检查：比较版本、选择当前图、涂抹/擦除/撤销、保存与下载蒙版，以及桌面/窄屏显示。HTTP 测试不能替代浏览器视觉检查。修改封面排版后，用 `examples/cover.json` 导出并打开图片检查。

## 调用与数据流

`relay_imagegen.py` 统一解析 CLI 并以 JSON 输出结果。generate/edit 经会话包装进入生成流程，在请求前完成文件、参数和能力校验，然后写运行清单，提交请求，保存图片并更新清单。异常批次停止后续提交。

- Images：generate 使用 JSON；edit 默认 multipart，显式选择时才发送 JSON Data URL。
- Responses：顶层主模型与工具图片模型分别配置；不会根据模型名或错误自动切换后端。
- SSE：收到预览/最终图片即落盘并更新清单；预览不计入成功图片。断流时保留已保存结果。
- 工作台：只绑定 `127.0.0.1`，检查 Host/Origin 和随机令牌；不读取密钥、不提交生图。
- 封面：加载 JSON 配方、校验布局与字体、渲染所有尺寸后导出；不调用网络。

## 需要保留的行为

1. 不自动重试付费生成，不自动切换模型、主机、协议或请求数量。
2. 配置检查只表示字段齐全；模型列表及能力声明不证明远端实现。
3. 下载图片 URL 不携带 API Key，API 请求不跟随重定向。
4. stdout 是最终 JSON；进度到 stderr。不要将原始服务端错误或凭据写到日志。
5. 运行记录保留实际尺寸、请求状态和已保存图片；中断状态不能被当作可安全重发。
6. mask 以第一张参考图为基准，保持尺寸和透明通道校验。提示模型保留区域不等于像素级保证。
7. 会话写入使用锁与原子替换；undo 选择父版本，不删除分支或原图。
8. `store=false` 是 Responses 默认值；只有显式留存且 endpoint/主模型匹配时复用父响应 ID。
9. 本地输出尺寸和后处理不能声称为服务端原生结果。

## 安装与打包

```bash
# 安装到临时目标，检查分发内容是否自包含
python3 tools/install.py --destination /tmp/relay-imagegen-install
python3 /tmp/relay-imagegen-install/scripts/relay_imagegen.py --help

# 构建分发包
python3 tools/package.py
```

安装器默认目标为用户级 `~/.agents/skills/relay-imagegen`，允许通过 `--destination` 指定完整目标。更新已有目录必须显式 `--replace`，旧目录备份在技能发现目录外，避免产生重复技能。已有链接指向源码时视为已经安装，不重复复制；不覆盖指向其他位置的链接。

打包默认产物为 `dist/relay-imagegen.zip`，内容是单个 `relay-imagegen/` 顶层文件夹。白名单仅包含 SKILL.md、agents 元数据、运行脚本、references 和工作台 HTML；不包含测试、用户配置、生成图片和缓存。ZIP 使用固定成员时间戳，相同源码产生相同字节内容，并在完成前逐文件核对。

历史工作目录可能保留根目录 `relay-imagegen.zip` 链接，供旧快捷路径继续使用；该链接被 Git 忽略。新使用者以 `dist/` 为打包位置。

## 提交与发布

- `.gitignore` 排除 output/dist、API 配置、环境文件和 Python 缓存。不要 `git add -f` 绕过配置文件规则。
- `examples/` 只使用占位服务地址和无凭据文件；示例配方使用相对路径。
- 展示截图不要包含真实服务地址、令牌、用户目录、密钥或未授权素材。现有工作台展示图已裁去路径区域。
- 提交前运行测试、查看 `git status --short` 和差异；安装包通过构建命令生成，适合附到 GitHub Release，不作为源码跟踪。
- README 只写已实现的用户行为，不把模拟测试通过写成某个服务商已开放功能。
- 真实中转站验收单独记录服务商、模型、参数和结果，明确计费与重试边界；不将配置文件或原始凭据写入报告。
- 对外发布的授权协议由项目所有者选择；当前仓库不代替所有者指定许可证。
