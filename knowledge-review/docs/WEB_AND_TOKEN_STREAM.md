# 联合网页与聊天增量输出

本功能把原Knowledge Review网页通过C++ Gateway统一访问；两个项目继续独立维护，由Compose运行Gateway、Agent、Redis。

## 如何运行

Windows PowerShell，从Gateway目录执行：

```powershell
Set-Location 'E:\projects\ai-gateway'
& 'E:\agentic project\knowledge-review\.venv\Scripts\python.exe' tools/start_local.py
```

WSL使用已安装基础依赖的Python，在Gateway目录运行python3 tools/start_local.py。可用--project、--credentials、--port指定Python源码、模型配置和端口。

启动工具导出公开运行代码，启用CPU embedding，优先复用已验证的公开E5缓存；没有该缓存时显式准备模型。模型Key只进入忽略的deploy/agent.env，Gateway连接Key保存在忽略的deploy/.env；已有agent.env不覆盖。具体操作与停止方式见Gateway的deploy/README.md。

访问http://127.0.0.1:8080，在连接窗口输入deploy/.env里的GATEWAY_API_KEY值。页面不持久保存Key；Gateway验证后签发进程内随机的HttpOnly、SameSite=Strict会话Cookie。Gateway重启后浏览器需要重新连接。普通API客户端仍可直接发送X-API-Key。

## 一次聊天的数据流

1. 网页和明确的静态文件路径公开读取，业务API、文档页面图片和指标继续鉴权；Cookie不转发到Python。
2. 网页POST /api/conversations/{id}/turns/stream；失败轮次通过POST /api/conversations/{id}/turns/{turn_id}/retry/stream重试，保留原轮次与幂等语义。
3. Agent按原权限和预算执行工具；最终生成使用SDK的stream=True，传输层边读上游边交付字节。
4. 程序从结构化JSON或submit_result工具参数里增量解析blocks[].text，发送event: delta、data: {"text":"内容增量"}。SDK块与单个token不保证一一对应。
5. 网页用TextDecoder跨块解码UTF-8，按SSE空行拼接事件，通过textContent显示生成预览。工具决策、隐藏推理及原始JSON不展示。
6. 全量结果仍经Pydantic、引用权限和业务校验，再保存SQLite，发送result/done；正式回答与引用以保存结果为准。失败或连接中断会清除预览并读取历史确认状态。

“停止生成”撤回当前请求，沿用已有模型HTTP取消与实际工作槽释放规则。已经提交的结果保留；CPU/文件/SQLite等阻塞不强杀。浏览器解析失败或没有done时，也读取聊天记录确认实际状态。

## 资源与兼容边界

- Cookie仅用于当前Gateway进程，本地默认HTTP与127.0.0.1入口；不是多用户账号系统。Cookie写请求要求同源Origin，Key方式兼容既有命令行客户端。
- Gateway按明确方法/路径白名单开放原网页业务及PDF页面图片；/internal/metrics仍只由固定/agent-metrics代理，客户端不能指定任意上游。
- 增量事件使用有界队列；队列满时对生成读取施加背压。单事件和累计字节预算、Gateway超时、取消和工作完成确认仍生效。
- 当前增量展示覆盖聊天新提问与失败重试；出题、评分、标题及工具决策保留原结构化调用。原JSON聊天接口和CLI继续可用。
- 新业务卷与原Windows资料隔离；原业务迁移、容量实验、全面答案质量和高级V3不属于本次交付。

详细实际步骤、问题及本轮验证结果见Gateway的docs/DEVELOPMENT_PROCESS.md和对应日期记录。

## 2026-10-22实际检查

三服务已从本轮源码重建并在127.0.0.1:8080运行。核对容器内89份公开文件的SHA与导出上下文、当前Python源码一致；Agent和Redis未发布宿主机端口，模型缓存正常运行时只读。启动工具记录逐条Docker命令、目的、退出码与耗时，运行元数据见Gateway的build/local-deployment.json。

独立Edge浏览器、公开Key和受控模型已验证登录、Markdown/PDF导入、原图加载、保存引用、提前显示增量、停止和同轮次流式重试；没有页面脚本/接口错误。真实deepseek-flash另做一条公开TCP样例问答，263个delta，首段15.586秒、正式结果17.899秒，聊天历史保存确认通过。时间是该次观测，包含Agent前置流程，不是容量或延迟基准；一次业务生成不等于一次供应商HTTP请求，后者没有计数。

C++编译及本轮相关65项原生用例通过，最终构建另复查4项网页鉴权用例；Python选择105项相关用例，首次104通过，1项因隔离目录没有模型配置失败，补充仅测试用公开假配置后定向通过。另有5项网页事件解码和5项适配安装保护检查。没有重跑Python全量或新增ASan验收，不将重叠次数相加。

Gateway保存带日期的汇总：evidence/web-token-verification-20261022.json。逐步命令、理由和失败记录见docs/development/2026-10-22-web-and-token-stream.md；两个项目保持独立，未自动提交/推送。临时浏览器测试项目已清理，保留正式三服务和公开模型缓存。

## 网页错误提示与重新连接（2026-10-23补充）

Gateway返回限流时显示“请求过于频繁”和Retry-After建议秒数；排队过载显示“当前生成任务较多”；连接故障、服务检查失败与等待超时各有说明。超时/连接中断仍建议读取历史确认是否保存，不能据此认定模型没有执行。普通Python业务错误继续沿用原detail，独立Python没有/gateway/session时不启用网关会话逻辑。

初次会话探测网络失败或503后，下一次操作可以重新探测；不再缓存一个永久失败的初始化Promise。多请求遇到旧会话401时共用登录，迟到的旧401不会在新会话已经建立后再次弹窗。登录框可按“取消连接”或Escape退出，输入保留；再次提交时可重连。等待鉴权的请求支持AbortSignal，取消单个等待者不取消其他请求的共享登录。

问答页取消登录后不立即发起历史读取，避免刚关闭弹窗又被自动打开；这是鉴权在转发前拒绝的路径。真正生成中的停止、断流和超时仍按原流程读取实际历史。只有明确的Gateway 401 INVALID_API_KEY会在连接后重发一次；429/503/超时/网络失败不自动重发POST。已有request_id恢复逻辑继续沿用，输入不丢失。

本轮10项隔离真实浏览器检查、5项既有SSE解码检查和最终镜像完整页面恢复检查通过。没有新增真实模型调用或重跑C++/Python全量；不以旧的65/105项作为本轮重验。具体步骤见Gateway的docs/development/2026-10-23-web-recovery.md；独立汇总为evidence/web-recovery-verification-20261023.json。
