# Context Budget：成熟项目做法与本项目采用建议

查阅日期：2026-10-24。状态：**资料核验与设计建议，本轮未修改 generation_budget.py 或更换模型**。以下区分外部项目已有实现与本项目建议；未把外部实现的效果当作本项目实测。

## 1. 结论

token 预算不需要重新研究分词算法。成熟实现通常组合：模型容量元数据、适配该生成模型的 tokenizer/计数服务、输出预留、协议开销、消息/资料裁剪和明确的回退策略。可以复用这些机制，无需先建语义评测集来选择分词方案。

但“完整请求能否放进窗口”和“裁剪后证据是否足以回答”是两个问题。前者可以通过成熟组件和兼容性检查完成；后者仍是独立质量问题，本次按用户要求后置。

## 2. 官方文档与源码中实际做了什么

| 项目/来源 | 查到的实现 | 可复用的原则与限制 |
| --- | --- | --- |
| LiteLLM | `token_counter` 按模型选 tokenizer，支持自定义 tokenizer；源码接收 messages、tools、tool_choice，累计工具定义开销 | 模型适配优于字节计数；未知模型会回退 tiktoken，回退值不能冒充目标模型的精确计数 |
| Dify | `SimplePromptTransform._calculate_rest_token` 从模型 schema 取 CONTEXT_SIZE，用 `model_instance.get_llm_num_tokens` 计算当前消息，减去输出 max_tokens，再把剩余额度交给历史记忆 | 容量和计数由模型适配层提供；本次核对的函数没有同时证明所有工具/图片/原生 JSON Schema 的完整预算行为 |
| LlamaIndex | `PromptHelper` 从 context_window 减 prompt tokens、num_output；计算资料空间时另计工具、system prompt，支持 tokenizer 和 padding | 给输出和固定协议预留空间，再装入检索材料；其中仍使用 estimate 方法，不能声称与任意服务账单精确一致 |
| LangChain | `trim_messages` 支持模型或自定义 token_counter；文档要求保留合法消息顺序、工具调用对应结果；approximate 计数也显式标记为估算 | 不能简单按字符截断消息列表；近似计算可以用于快速裁剪，默认字符/token 比率不适合直接当所有中文模型的精确计数 |
| DeepSeek 官方 | token usage 页面提供 tokenizer 示例包，说明实际输入/输出用量以响应 usage 为准；图片有独立计算规则 | 可采用供应商 tokenizer，但必须确认具体模型版本匹配，不能仅根据 `deepseek-flash` 这个配置字符串选择当前网页的 tokenizer |

可核验来源：

- [LiteLLM token usage 官方文档](https://docs.litellm.ai/docs/completion/token_usage)，[token_counter 源码](https://github.com/BerriAI/litellm/blob/main/litellm/litellm_core_utils/token_counter.py)。本次读取的函数没有 `response_format` 参数，不能凭 messages/tools 接口推断它已覆盖本项目原生结构化输出全部开销。
- [Dify prompt_transform.py 源码](https://github.com/langgenius/dify/blob/main/api/core/prompt/prompt_transform.py)。核对 `_calculate_rest_token`，不把某个函数的行为推广成 Dify 所有 Agent 路径。
- [LlamaIndex PromptHelper 源码](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/indices/prompt_helper.py)，重点 `_get_available_context_size` / `_get_available_chunk_size`。
- [LangChain trim_messages](https://reference.langchain.com/python/langchain-core/messages/utils/trim_messages)、[近似计数](https://reference.langchain.com/python/langchain-core/messages/utils/count_tokens_approximately)、[短期记忆与消息合法性](https://docs.langchain.com/oss/python/langchain/short-term-memory)。
- [DeepSeek Token & Token Usage](https://api-docs.deepseek.com/quick_start/token_usage)。当前页面给的是当前公开 tokenizer，未核验它与项目实际配置别名的映射，因此本轮不下载、不替换。

链接中的 main/在线文档会更新；上述描述是本次读取结果。没有用下载量、星数或所谓行业统一标准来代替实现证据。

## 3. 推荐采用的结构

对任一实际待发请求满足：

`count(实际输入及其协议) + 输出预留 + 安全余量 <= 已确认的上下文容量`

同时遵守供应商单独规定的最大输入、最大输出及图片等限制；如果某模型的上下文/推理输出规则不同，由适配层体现，不能只套一个全局常数。

### 3.1 模型适配与容量

- 按服务/模型身份明确配置容量、输出上限、tokenizer 身份或计数接口，不只看是否兼容 OpenAI HTTP 格式。
- 优先供应商支持的预先计数接口；有匹配的官方 tokenizer/chat template 时可本地计数。不假设 DeepSeek 提供了这样的在线接口。
- LiteLLM 是可评审的统一适配候选；本项目也可直接复用官方 tokenizer 组件，避免为了计数引入完整代理/Agent 框架。是否新增 LiteLLM 依赖应结合其对实际模型与协议的支持决定。
- 未知服务维持有标签的保守回退；不能把默认 tiktoken、E5 tokenizer 或统一“字符数除以4”视为精确答案。

### 3.2 计数对象必须是实际协议

当前项目既有普通工具调用，又有原生结构化输出及兼容的 `submit_result` 工具协议。计数必须涵盖 system、历史、当前问题、工具调用及结果、工具/schema 定义、实际加入的证据和必要格式开销；协议回退前重新检查实际将发送的版本。

本地 tokenizer 只编码普通 JSON 字符串，不等于服务内部 chat template 的精确计数。适配层未支持的 schema 部分应明确增加估算和余量，而不能宣称“接上 tokenizer 就完全精确”。不把 max_tokens 这个数值本身的字符串长度当作完整输出预留。

`GenerationBudget.estimate()` 当前还用于任意字典/历史等对象的空间分配，不能直接把它替换成只接受 messages 的第三方函数。迁移时应区分内部装配估算与最后一次完整请求检查，逐个核对调用者。

### 3.3 保留已有业务裁剪契约

- 固定 system/必要用户选段和输出预留先占预算，再分配历史与可选资料。
- 历史按完整轮次及工具调用/结果的合法序列裁剪；不能留下孤立 tool 消息。
- 原文继续按真实行范围裁剪，表头/条件依赖和原子块规则保留；可引用范围随实际发出的材料同步缩小。
- 图片采用供应商的图片计数规则或显式 allowance，不能按 base64 字节数估算；未知图片模型仍须保守回退。
- 超限时沿用明确错误与原输入保存，不静默截断必要内容，不为追求调用成功放松引用权限。

### 3.4 诊断与验收

诊断记录计数方法、tokenizer/模型身份、容量来源、估计值和服务实际 usage（若返回）。实际 usage 是调用后的记录，不能取代发送前预算。

不需要自行开展 tokenizer 算法实验；需要的只是组件兼容性和边界验证：中文/代码输入、工具定义、两种结构化协议、长工具结果、图片、接近窗口上限、未知模型回退及正确的裁剪权限。服务内部模板不公开时保留误差声明与余量。现有 2048 安全余量不因引入 tokenizer 自动归零。

## 4. 本轮建议的决策

优先确认当前生成服务的模型身份/容量与官方 tokenizer 对应关系，随后选用成熟计数适配。保留现有预算分配和引用裁剪规则，替换计数依据，并验证全协议边界。无需把“建立新的答案质量评测集”作为这一步的前置条件。

在用户决定实施前，当前 UTF-8 字节计数、输出预留、服务配置与运行进程均保持原状。本次不从文档上的容量数字推断当前部署容量。
