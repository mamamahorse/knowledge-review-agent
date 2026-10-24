# 2026-10-24 技术审计后续修复

## 授权与范围

按用户本轮要求修复默认应用导入副作用、网页PDF VLM执行准入。Embedding先给具体方案；Context Budget只核验成熟方案，不改变算法。最终回答语义评测后置。

## 默认数据库导入副作用

原模块末尾 `app = create_app()` 在测试仅导入工厂时也打开默认Store。改为模块 `__getattr__('app')` 延迟装配：只有显式访问默认ASGI应用时才构造，并用锁及模块属性缓存确保并发首次访问只创建一次。

`uvicorn web_app:app` 和显式 `from web_app import app` 仍会初始化默认应用；这是有意启动应用的行为。`import web_app`、`from web_app import create_app` 不再初始化默认库。测试通过工厂显式给临时目录，避免默认库接触。

Python模块的 `__getattr__` 只在普通属性查找没有找到名称时调用。第一次创建后将app放入模块globals，后续普通访问直接命中；锁内再次检查以避免两个线程同时初始化。未知属性照常抛AttributeError。数据库本身的建表/兼容迁移逻辑不在这次改动中重写。

## PDF VLM与聊天共用准入

导入、替换、重解析三处共用 `create_app` 内的 `parse_document_pdf`：先取得解析配置，VLM启用时通过原 `StreamRunner.run` 执行整次解析，否则沿用普通线程池。

- 许可取得发生在解析执行前。槽满直接503/Retry-After，既不会开始VLM，也不会被解析器的“模型失败保留partial”分支吞掉。
- 使用同一工作池/指标/取消Token，没有新增页队列。断连请求取消；CPU阻塞不强杀，真实Future退出后释放名额。
- 解析返回前执行checkpoint，防止最后一次模型调用后收到取消却仍进入资料发布。正常模型错误仍保留原有partial解析行为，来源版本检查继续执行。
- 已启用VLM的本地解析阶段也占用名额，即使这份PDF最终无需转写。选择此范围是为了复用已有经过测试的生命周期；代价是可能减少并发聊天容量。纯本地解析不占名额，CLI解析没有被宣称纳入网页工作池。将来若容量数据支持，再单独拆本地解析和按页模型准入。

## 本轮验证

| 范围 | 结果 | 本机日志 |
| --- | --- | --- |
| 新增 `test_web_import_isolation`、`test_pdf_admission` | 最终8项通过，8.888秒 | `.data/evaluations/audit-fixes-new-tests-20261024.txt` |
| 既有 `test_web_app`、`test_pdf_documents`、`test_agent_admission`、`test_agent_governance`、`test_agent_sse` | 66项通过，34.860秒 | `.data/evaluations/audit-fixes-tests-20261024.txt` |

两组模块不重叠，共74项相关检查。新增用例覆盖：子进程拒绝任意SQLite连接/默认目录写入时仍能导入工厂、并发首次访问只构造一次、Uvicorn属性入口兼容、三条PDF路径槽满不发布、VLM反向占用聊天槽、纯本地解析不需要槽、普通失败释放并保留partial、断连后真实退出前持续占槽且不发布、VLM替换/重解析成功及旧版本拒绝。

测试资料为合成PDF与公开文字，转写使用受控结果，没有外部生成模型调用。新取消用例的初版使用同进程AsyncClient/ASGITransport，在普通读取请求处挂起；没有归因于业务取消错误。改用现有TestClient和可控Request断连信号验证真正的路由/解析/工作槽边界，超时/中断的自有临时目录已清理。另一个初始用例因请求缺必填字段得到422，补齐真实请求后通过；这些均为测试工具修正，没有修改业务校验来使测试通过。

未重跑全量discover、未测试真实VLM质量/供应商停算、未重启现有服务或重新构建Gateway部署。当前工作区原先存在的其他修改保留；未提交或推送。

## 待评审材料

- [Embedding耗时拆解与受控复用方案](embedding-performance-plan.md)：第一阶段仅测现状，第二阶段根据结果决定模型复用。
- [Context Budget官方项目调研](context-budget-research.md)：Dify、LiteLLM、LlamaIndex、LangChain及DeepSeek官方计数资料，含现有双协议/图片适配边界。

10月23日审计材料及source-snapshot.json继续描述原审计基线，不伪装为本轮修复后的源码哈希或位置。
