# 任务识别与持久澄清：实现说明

2026-10-26。原始设计见 task-routing-and-clarification-proposal.md。

## 已接通的行为

聊天 Agent 首次决策可以使用 select_task 或 ask_question，无额外的必经分类模型。普通解释、比较和历史回顾沿用原检索/读取；ask_question 仅用于重要任务歧义。注册表包含九项现有任务及一项不可执行的整份资料总结，模型不得把未实现任务作为选项。

ask_question 发布结构化卡片后直接停止，不进入最终回答模型。ConversationStore 保存 waiting_user、卡片身份/revision/真实选择，保持原始 request_json 不变。原 pending 唯一索引和取消逻辑扩展到 waiting_user；恢复不重置等待卡片，等待无运行锁/模型连接，真实 Future 完成释放槽。

用户选择解释/历史回顾后继续原问答；选择业务工作流后返回明确标注“尚未创建”的任务入口卡片，打开原练习/学习清单界面并填入方向，用户在原界面选择具体对象、核对集合后执行。这里有意采用设计中允许的入口卡片方式：聊天所选文档与集合及子集合的范围未必相同，不将两者静默映射，也不自动接受归组或加入计划。暂未实现从聊天直接创建后台清单、通用总结任务或多阶段自动编排。

首次 capability 目录直接随首页渲染，浏览器无需额外网关白名单。兼容原 Gateway 的前端通过既有 retry URL 以 save_only/clarification 保存答案，再请求原 retry SSE 执行。保留独立的直接 Python 澄清 API；它未加入旧 Gateway 白名单，网页不依赖它。retry 兼容入口仍需短暂执行准入，繁忙时选择先保留浏览器 localStorage，重试不换 response_id；直接澄清 API 无需模型名额。没有更改或部署 Gateway。

控制动作必须独占模型响应；冲突时整组不执行任何读取/写入，返回有界错误。非法任务、重复选项、旧卡片、冲突回答和跨会话轮次均拒绝。澄清最多三轮；可取消后重新说明。自由文本另存并作为显式用户补充传入，来源权限仍由旧资料版本解析校验。

## 验证边界

执行命令：`.venv/Scripts/python.exe -m unittest -q test_task_routing test_agent_loop test_conversations test_agent_sse test_agent_admission test_agent_governance test_conversation_memory`，以及 `node test_chat_stream.cjs` 和前端语法检查。

受控测试验证执行、持久化、幂等、等待释放和消息协议；不证明真实模型意图判断准确率。没有外部模型、生产数据库迁移或常驻服务部署。实际结果与次数见 development-log 最新记录。
