# 结构化输出的 JSON 修复、纠错与审计

本次只改结构化结果恢复层和必要接入，不更改V1/V2检索、分块、目标数量、预算、证据权限或业务Schema，不启用新的服务端strict/Beta协议。依赖固定为`json-repair==0.64.2`。

## 执行规则及原因

`structured_output.request_structured`统一管理结果解析和有限重试，`structured_recovery.py`负责受控语法修复、错误反馈和本地审计。

1. 保存原始模型正文/submit_result参数；拒绝、内容过滤和输出length截断不进入修复。HTTP请求失败没有可修复的模型JSON，不能用默认对象冒充成功。
2. 原JSON语法正常时，直接按原Pydantic Schema验证，不调用修复库。重复键不以“后值覆盖前值”的方式静默通过。
3. 语法错误时使用json-repair。若一个完整JSON值后只有多余闭合括号，先用标准JSON解码器确定这个完整值的边界，再交给修复库处理，避免丢弃其他JSON对象或正文。保留修复候选、修复库日志及字符级修改记录。
4. 对照修复前后的完整字符串、键、数值、布尔/null及顺序。不允许补造字段值、接收未闭合字符串的猜测结果、丢掉第二个对象或合并重复键。安全边界不满足则拒绝本地修复，交给模型纠正。
5. 修复后的结果必须通过完整原Schema。清单局部概述、概述归并、V1正式提取和V2直接提取通过validation_scope接入既有证据校验；不合法引用、未知证据ID、范围越界仍拒绝。原有保存前的校验也保留，避免测试替身或其他调用绕过它。
6. 解析、Schema或已绑定业务校验失败时，将前一次实际输出及具体错误以数据形式反馈给模型，要求使用原证据、保留必要条件纠正，不原样重发。初次之后最多两次纠错；已经合法的字段和条件不会通过本地截断/删减来满足Schema。

原生JSON Schema请求仍发送SDK展开的相同Schema，但使用原始Chat Completion返回后在本地统一解析，以保留SDK自动解析失败前的正文。普通协议能力回退保持原规则；强制submit_result的实验不增加原生探测或auto回退。

SDK隐式重试关闭，项目内部对可恢复传输错误也遵守同一个三次尝试边界；认证、未知400、拒绝、截断和预算/来源保护拒绝不盲目重试。已有协议协商400回退仍是额外的显式HTTP操作，须单独计数，不能把“最多两次纠错”说成所有兼容模式绝对最多三次HTTP。当前强制工具实验没有这类协商。

每次新请求都重新检查原预算及已有before_request/source guard；追加纠错上下文装不下时停止，不裁剪必需证据或扩大预算。V2每次真实调用继续预留调用额度、记录usage。后端原有Schema/引用拒绝不会因JSON修好而消失。

## 流式路径

流式预览继续只显示答案正文，不显示原始JSON、错误信息或隐藏思考。完整收集后才统一修复和验证。无效JSON片段不使预览词法器的尾括号异常提前结束整个解析。若已显示第一轮预览，后续纠错轮次不重复追加文本；正式结果仍由现有最终结果事件替换/确认。不能把预览文字当作已经通过证据验证的答案。

## 查看原始返回、修复结果和修改记录

默认目录是项目本地`.data/structured-output/`，每个逻辑请求使用独立UUID的JSONL文件。它受既有`.data/`忽略规则保护，不是公开HTTP接口。记录原始可见返回、original文本、repair_candidate及changes/library_log、Schema/业务校验阶段、纠错反馈、各次失败和最终完成。隐藏reasoning_content、请求认证头及API密钥不写入。

Python读取接口：

```python
from structured_recovery import list_traces, read_trace, trace_id_from_error

recent = list_traces(limit=10)
records = read_trace(recent[0]["trace_id"])
# 对现有业务层包装过的异常，可沿__cause__找到审计ID：
# records = read_trace(trace_id_from_error(error))
```

命令行在项目目录使用虚拟环境执行：

```powershell
.\.venv\Scripts\python.exe -m structured_recovery list
.\.venv\Scripts\python.exe -m structured_recovery show <trace_id>
```

`audit_scope(directory, observer)`可以将记录写入隔离运行目录，并通过观察接口关联业务任务；`validation_scope(validator, guard)`绑定现有纯校验函数与请求前保护，不需要改变输出Schema或给结果增加业务字段。读取ID必须为UUID，不能用相对路径读取任意文件。

## 实验接入与历史边界

当前`outline_benchmark_generation.GenerationRunner`已直接调用项目request_structured并指定forced_tools，不再让旧评测专用解析器绕过修复层。审计保存在新运行的`live/structured-output/`，job记录structured_output_trace_ids；失败类型、HTTP和usage继续单独记录。项目三次尝试耗尽后禁止实验外层再给两次恢复机会，避免变成九次；Judge仍禁用。

后续真实实验必须新建运行目录，重新冻结当前源码、依赖及协议元数据；原有源码哈希会拒绝在旧冻结任务上静默换实现。本轮没有启动真实模型请求，没有改写旧比较文件或把旧失败改成成功。

离线回放历史D03的三条尾括号错误：语法修复后仍被原证据校验拒绝。历史D04首次返回修复后还暴露出五个候选超过V1四个上限，继续被原Schema拒绝；没有擅自删掉第五个目标。回放记录在`.data/structured-recovery-check-20261028/REPLAY.json`，不是新模型实验或质量收益证明。
