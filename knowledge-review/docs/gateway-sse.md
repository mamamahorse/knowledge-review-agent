# Knowledge Review 的 SSE、共享执行槽与请求取消适配

2026-10-16：复用原同步轮次流程，新增进度 SSE，不改变已有 JSON 入口、表结构、引用规则或学习计划。

2026-10-18 M2.2：在保留上述业务流程的基础上，让网关已接入的7个JSON模型入口与SSE共用有上限的执行器。数据库和业务请求/成功响应格式不变；达到上限时新增503及Retry-After。

2026-10-19 M2.3.2：上述JSON/SSE工作在请求取消范围内执行。客户端断连、网关超时/关闭上游连接时，Python请求取消实际模型HTTP传输；会话沿用failed并保存安全的取消说明。该段取代下文M1/M2.2“不停止同步模型调用”的旧语义；不代表强制停止CPU工具或远端供应商计算，未纳入本池的后台202/标题仍待治理。

## 文件和调用链

- `web_app.create_app()` 创建一个 `StreamRunner`，新增 `GET /healthz`、`POST /api/conversations/{identity}/turns/stream`。前者不调用模型；后者接受原 `TurnRequest`。
- 新入口的资料范围检查在工作线程运行，Schema、空问题、过期资料、会话不存在或占用等错误尽量在 HTTP 200 之前返回。原 `execute_turn()` 保留会话锁、幂等、恢复、权限检查和数据库提交，只增加可选 `event_sink`。
- `generate_answer()` 把可选回调传给 `run_agent()`；没有回调时不改变旧接口的调用参数。事件只描述程序已执行的阶段，不发送模型内部推理、问题正文、工具参数或工具返回全文。
- `agent_loop.run_agent()` 在 Agent 模型请求前、工具开始/完成和最终生成前通知。最终回答仍由原 `answer_question()` 做结构化解析与引用校验。
- `agent_stream.py` 把同步回调与异步 HTTP 输出连接起来。`result` 在 `ConversationStore.finish()` 返回后生成，客户端收到的 `turn` 与原 `/messages` 的公开轮次格式一致。完成后的自动标题流程仍调用原 `auto_title()`，不阻塞结果输出，但在结束前占用执行槽。

这些修改改变执行进度的可见性，不把同步模型调用改成逐 token 生成，也不另建一套 Agent 或数据库服务。

## 事件契约

`Content-Type: text/event-stream`，每条为 `event: 名称\ndata: 单行 JSON\n\n`。网络 chunk 和 SSE 事件不是一一对应的，客户端要保留跨块残片，按空行解析。

| 事件 | 内容和时机 |
| --- | --- |
| `meta` | `request_id`、`turn_id`、`trace_id`；轮次准入成功后发送 |
| `progress` | `phase`、`status`；工具阶段另含 `tool`，不含工具参数 |
| `result` | `{"turn": <public_turn>}`；成功保存后发送一次 |
| `error` | 固定安全 `code/message` 与 `turn_id`；不回显供应商异常全文 |
| `done` | `status` 为 `completed/failed` 与 `turn_id`；表示本次流的终态 |

15 秒没有事件时发送 `: ping` 注释。HTTP 头已经发出后的业务错误通过 `error/done` 表达；客户端不能只看到 HTTP 200 就判定生成成功。传输中断且没有 `done` 时，应读取 `/messages` 确认真实保存状态。

完成轮次在输入相同且当前范围预检查通过时，直接返回原结果，不重跑工具和模型。不提供事件日志重放，也不使用 Last-Event-ID 恢复半条流。旧 JSON 接口的恢复语义不变。

## 为什么需要有界线程桥

模型与工具仍为同步函数，不能放进 FastAPI 的事件循环直接执行。`KNOWLEDGE_REVIEW_MODEL_WORKERS`控制每个应用进程的共享工作上限，范围1–32，独立Python默认4、Compose默认2。提交执行器前先取得槽位，满时立即HTTP503及Retry-After，没有额外等待队列。

7个JSON入口为`ask/submit_turn/retry_turn/create_practice/submit_practice_answer/retry_practice_grade/review`，通过`@streams.bounded`保留FastAPI参数签名，在共享池执行原函数。SSE通过同一个`_submit()`提交。异步调用者使用shield等待；名额由concurrent Future的真实完成回调释放，不由HTTP等待协程的finally释放，所以取消等待不会让仍运行的线程被误计为结束。

事件从工作线程写入标准库线程安全队列，异步生成器每 20 ms 检查一次，不跨线程操作 `asyncio.Queue`。队列最多 32 条普通事件；单事件最多 8 KiB，累计进度最多 256 KiB。超限使任务明确失败，不截断或静默丢弃。终态使用独立保留槽，即使进度队列满也能返回 error/done。最终 result 帧最多 4 MiB。

若最终结果超过传输预算，业务可能已经保存完成，流返回 `RESULT_TOO_LARGE` 和 `done.failed`；这表示该次结果传输失败，不能据此重做 POST 或判定事务回滚。较大历史需另行读取；当前网关的普通 JSON 响应同样有 4 MiB 上限。

当前客户端断连会请求协作式取消。`agent_cancel.Cancellation`在线程范围内绑定；模型、摘要、练习和视觉HTTP请求通过`model_http_options()`接入可取消传输。外层仍用同步OpenAI接口解析完整JSON，内层在工作线程拥有的asyncio事件循环运行httpx异步连接；每20ms检查取消标志，取消后等待异步连接关闭，再让工作线程退出。已接入工作范围内禁用SDK自动重试，避免取消后继续等待重试退避；CLI和范围外后台任务保持原传输。不是逐token输出，不共享或跨线程强行关闭httpx客户端。

JSON包装器只增加FastAPI内部注入的Request，不增加请求字段；正文解析后轮询断连。SSE在响应头前同样监测，响应开始后由StreamingResponse的断连取消触发生成器finally。Agent在模型迭代/工具之间及最终持久化前检查取消；`execute_turn()`捕获独立的GenerationCancelled，将已有轮次标为failed，保留原输入和重试流程。这个异常继承BaseException，专门越过SDK/业务的宽泛Exception捕获，避免把取消变成重试或普通模型失败；边界由StreamRunner与会话入口显式处理。

取消与提交存在竞争：提交已经完成则保留completed，取消不回滚已保存答案/评分/摘要；CPU工具、文件/SQLite及DNS底层阻塞不会被强杀，须退出或到检查点才结束。SSE/JSON名额始终保持到真实Future完成，包含连接清理；取消信号不能代替完成确认。JSON原有BackgroundTasks标题、读取触发的标题和其他后台202作业尚未纳入本池，不能称为所有模型任务的全局治理。

应用lifespan关闭入口并请求取消当前已绑定工作；不支持取消的阻塞操作仍需自行退出。Python线程不能强制中止，部署的停机宽限期不等于硬截止时间。

## 应用补丁

独立维护的Python项目不应包含Gateway网络代码。网关仓库保存历史`knowledge-review.patch`、本阶段相对M2.2的`m232-cancellation.patch`及校验过的完整`changes.json`。运行模块新增agent_cancel.py，修改既有StreamRunner、少量模型创建点/检查点和execute_turn异常边界；不修改数据库Schema或另写Agent。

在 Gateway 根目录执行：

```sh
python3 tools/apply_knowledge_review.py --project '/mnt/e/agentic project/knowledge-review'
python3 tools/apply_knowledge_review.py --project '/mnt/e/agentic project/knowledge-review' --apply
```

工具先检查所有目标文件的SHA-256，全部匹配才开始写入。支持原始基线、已验证M1.3/M2.2升级和当前M2.3.2重复执行；只接受清单列出的版本，其他修改仍拒绝覆盖。`knowledge-review.patch`是旧阶段历史补丁，本阶段增量见`m232-cancellation.patch`，实际安装/升级以当前带版本校验的工具为准。原.env、.data、.models、网页资产不在修改清单中。

在 Python 项目已安装基础依赖的环境中：

```sh
python -m unittest -v test_agent_sse test_agent_admission test_agent_cancellation
python -m uvicorn web_app:app --host 127.0.0.1 --port 8000 --workers 1
```

网关照常连接 `http://127.0.0.1:8000`，探针改用 `/healthz`。提交流式请求需先使用现有接口获得会话 ID、文档 ID 和当前 source_id，再提交原 TurnRequest 加 request_id；不要编造标识符。浏览器使用 POST fetch 读取流并添加 `X-API-Key`，原生 EventSource 不支持这套请求契约。当前网页仍使用原 JSON API，没有改成 SSE UI。

## 本轮验证边界

新增 12 项测试覆盖事件预算、终态保留、心跳、断连槽位、HTTP 错误、实际 SQLite 保存和幂等。相关原有 80 项测试通过，覆盖 Agent、会话、网页、记忆、练习、学习计划和模型客户端；不是原项目全量回归。

实际 C++ Gateway → Uvicorn → Agent → 本地可控 OpenAI HTTP 服务完成 10 项链路检查，执行了真实 `read_source`，经过最终结构化校验并保存。首次运行进度约 45 ms 到达，最终结果约 887 ms 到达，证明事件在执行期间输出。供应商响应为公开合成数据，没有外部模型调用，也没有真实 embedding/RAG 或回答质量验收。

上面12/80/10项为M1.3历史结果。当前M2.3.2普通/ASan同批94项、Python相关115项及工具4项通过；真实隔离链路与容器分别通过同9组取消、同8组指标检查。范围及原始结果见Gateway根目录docs/CANCELLATION.md、README和`evidence/m232-*`。Redis限流在Gateway实现，Python不另建Redis策略。仍未实现逐token输出、所有后台作业治理、Python实际任务指标和前端流式交互；请求范围模型HTTP取消已实现，但不承诺强杀阻塞工具或停止供应商远端算力。


2026-10-20 M2.3.3：8个清单/归组/来源核查/路线的后台提交与继续入口，和自动标题，接入原共享模型工作池。先预留槽再创建记录，202返回不释放实际名额；提交者断连/Gateway重启不取消已接受工作。原pause入口先使attempt失效再请求取消模型HTTP，真实Future结束（含连接清理）才释放。嵌套自动核查复用父工作槽与Token；已完成结果保留，原暂停/继续/幂等契约及SQLite结构不变。

标题最多1个；workers>1时至少保留一个未占槽，单worker只在空闲启动且不抢占。忙时跳过无等待队列，失败用已有title_started_at冷却30秒。增加内部GET /internal/metrics，只在进程内聚合request/background/title；Gateway通过受Key保护的GET /agent-metrics固定代理读取。/metrics仍是Gateway本地请求观察，两者不能混同。Agent端口不要直接对不可信网络开放。

相关回归255项（含新增治理16项）、普通/ASan同批103项、真实受控链路与容器各同12组治理检查通过；工具/源码同步最终状态以独立Gateway的evidence/m233-verification-20261020.json为准。原Python不是全量discover，本轮没有外部模型/回答质量/embedding-RAG验收。进程重启计数重置，中断工作沿用原恢复后显式继续，不自动重放。CPU/文件/SQLite/DNS等阻塞不强杀，供应商远端停算未验证，分布式并发/V3仍未实现。详细改动理由、字段、接口和命令见Gateway仓库docs/JOB_GOVERNANCE.md；继续维护其docs/DEVELOPMENT_LOG.md。未自动提交或推送。
