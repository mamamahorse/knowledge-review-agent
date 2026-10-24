# 2026-10-24面试项目源码基线

两个独立Git项目使用同名本地标签interview-baseline-20261024。Knowledge Review保留原learning-loop-mvp历史；Gateway保留独立main历史。Gateway的release/baseline.json记录配套Python提交及全部公开运行文件SHA，Gateway本身由同名标签确定，避免互相记录对方提交产生循环。

本基线包含网页与模型增量输出、取消与重试、网关会话恢复，以及2026-10-24默认应用导入隔离/PDF VLM准入修复。既有业务表结构和冻结检索语料不变。后续embedding测量/预算替换/答案语义评测仍按已有方案安排。

## 获取和验证

在各自检出目录使用git switch --detach interview-baseline-20261024；不要在有未保存改动的工作区直接切换。干净检出可使用本地git clone --no-hardlinks --branch interview-baseline-20261024建立新目录。

Python仓库的Git根当前是父目录，项目在knowledge-review子目录。命令中--project始终指向含web_app.py的实际目录。

在Gateway根执行：

```sh
python3 tools/check_release.py --project '/path/to/python-checkout/knowledge-review'
python3 tools/apply_knowledge_review.py --project '/path/to/python-checkout/knowledge-review'
python3 tools/prepare_agent_context.py --project '/path/to/python-checkout/knowledge-review' --output build/agent-context-baseline
```

验证器只读Git、源码和公开资产，不导入web_app、不读.env、不打开业务库。44项安装目标应与源码完全匹配，重复安装为0项。文本换行使用.gitattributes固定LF，网关安装资产保存精确字节。

## 依赖、配置与数据

新环境按requirements.txt安装基础依赖，需要检索时使用requirements-local.txt及显式模型准备流程。精确测试环境与受控检查结果记录在Gateway的release/目录；该记录不是已经锁定所有跨平台wheel和模型权重的容器发布。

模型凭据从.env.example自行建立本机.env；Git不包含Key、SQLite、个人资料、.models或.venv。新检出不会恢复原学习记录，也不能仅凭冻结ID还原原审核数据库。

完整三服务操作见Gateway的docs/RELEASE_BASELINE.md和deploy/README.md。本轮仅冻结源码并验证干净检出，不启动/重建常驻部署，不调用真实模型或执行容量实验。当前运行实例是否为该基线，必须以后续显式重建及运行身份检查为准。

## Git范围

本轮保存应用源码、测试、公开文档与已有审计/面试材料。父目录三份设计稿及docs/project-context中的原有未跟踪材料保留，不混入此次发布提交；不删除、不追加忽略来隐藏它们。两个本地标签及提交尚不意味着已经推送远程。
