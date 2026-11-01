# PDF 自主视觉导航：第一批实现

日期：2026-10-30。对应大文件与多模态方案中的视觉工具基础；不代表整套学习清单改造完成。

## 行为变化与理由

原 inspect_visual 只能查看既有资源，查看局部总会附带整页。现在同一个工具支持：

- 使用 page-N 读取指定物理页；读取下一页或前页仍由模型决定。
- 使用已识别资源 ID 直接查看表格或图片区域。
- 使用 bbox=[x0,y0,x1,y1] 选择尚未被解析器识别的区域。坐标相对于所选图像，左上角原点，值域为 0..1。
- include_page 默认 false；模型需要背景时显式设为 true。
- VLM 可返回 suggested_regions 和 next_read，解释需要放大的区域或尚缺的信息。后端把区域建议转换成稳定 ID，但不会自动执行建议。

整页、放大、邻页阅读没有固定执行顺序，也没有新增“遇到某关键词必翻页”的规则。图像仍由 VLM 实际读取，聊天主模型收到有归属的文字观察与导航信息，不声称主模型直接看到了图像。

## 位置、身份与引用

新模块 pdf_regions.py 负责坐标校验、旋转变换和区域定位。工具坐标先映射到渲染方向下的目标区域，再转换为原 PDF 坐标；嵌套裁剪以当前选中区域为基准。

crop1 区域 ID 编码原页码与坐标，并绑定 source_id 的指纹。同一冻结来源与位置得到相同 ID，恢复后可以重建，不向不可变 Document 快照追加临时资源。它不是签名、密码或访问授权：文档访问权限仍由现有入口校验，引用权另外依赖成功查看回执。

区域建议与 navigation 只是定位信息。viewed_assets 仅记录此次实际送给 VLM 的图像，答案只能引用其中的来源、区域和页面锚点；看过局部不自动获得整页或相邻页的引用资格。显式附带整页时，回执记录两幅实际图像。旧版单资产回执继续可用。

视觉引用以页面导航行作为文本视图中的锚点，实际区域由 asset_id 和 bbox 表达，不把局部观察冒充整段 PDF 文本。新区域可通过原资源接口重开，网页引用展示支持动态区域 ID。历史来源已移出或替换时仍沿用既有历史快照展示行为，未新增完整历史 PDF 浏览。

## 资源与恢复

局部从原 PDF 重新渲染；不是把低清预览放大。精读按像素预算使用更高分辨率，单次图像不超过约 400 万像素，单边受 8192 像素资源约束；普通页面预览保留原先的缩放上限。极细区域也按实际栅格取整计算预算。

VLM_MAX_IMAGES 默认 8，可配置 1–64；它是资源上限，不要求每次提供这么多图。当前 inspect_visual 一次读取一个目标，可选附带整页；通用 vision_request 不再固定只能收两图。单图 8 MiB、图像原始字节合计 32 MiB 的应用限制继续约束请求。

VLM_IMAGE_TOKEN_ALLOWANCE 默认 8192，是每图应用估算，可配置，并继续标记为未经供应商计费验证。文字、Schema 与工具结果仍纳入完整预算。上下文、输出和安全余量沿用现有配置；未修改个人 .env。

VLM 请求继续通过现有结构化解析、受限纠错重试和请求取消机制；成功观察随 Agent context_snapshot 留存。调用次数仍受现有 Agent 工具预算约束，工具次数不等同 HTTP 次数。没有新增跨任务视觉缓存、供应商精确视觉 tokenizer 或账户费用硬限额。

## 代码入口

- pdf_regions.py：纯定位身份、坐标变换及导航元数据。
- pdf_parser.py：有界原件渲染与动态区域重开。
- pdf_vision.py：可选整页、视觉区域建议、查看回执与可配置图片预算。
- knowledge_tools.py：扩展原工具参数；复用原 Agent 循环，不新建 Agent 框架。
- answers.py / document_model.py：实际查看集合校验和引用位置展示。
- web/static/app.js：动态区域引用预览。

## 验证及未完成部分

新增 test_pdf_navigation.py 使用合成真实 PDF、实际 PyMuPDF 渲染和受控模型响应，覆盖四种旋转、CropBox、嵌套裁剪、细长区域、非法坐标、来源版本变化、建议区域不能提前引用、可选整页、网页重开、Agent 直接裁剪后跨页及请求预算。外部 VLM 和 Docling 均未实际调用。

运行命令：

```powershell
$env:NO_PROXY='localhost,127.0.0.1,::1'
$env:no_proxy=$env:NO_PROXY
.\.venv\Scripts\python.exe -m unittest test_pdf_navigation test_pdf_documents test_pdf_vision_settings test_table_reading test_agent_loop test_answers test_agent_cancellation test_agent_admission
node --check web/static/app.js
```

相关 84 项测试通过；JavaScript 语法检查通过。这证明受控执行和定位机制，不证明当前真实模型会合理选择裁剪区域，或准确理解复杂图表。

仍待后续开发：大文件解析分片与恢复、Markdown 资源盲点、章节关系、派生多模态检索表示、清单阶段的轻量规划及自主视觉补读、整体复查整合。本批没有修改 V1/V2 默认路径或将未验收的整体复查草稿宣布完成，也没有改 Gold、正式学习数据或启动真实实验。
