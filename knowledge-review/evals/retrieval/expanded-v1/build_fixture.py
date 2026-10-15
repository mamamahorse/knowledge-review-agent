"""Author-owned synthetic protocols, not generated labels from retrieval ranks.

Rebuild only before preparing a benchmark: changing any text invalidates its
manifest. Each row has reviewed evidence, two queries and explicit distractors.
The audit is separately bound to the actual production prepared chunks.
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

# title, direct evidence, identifier/condition query, semantic query.
DOCUMENTS = [
 ('import', '批量导入协议', 'dev', [
  ('import_batch 与空行', '合成导入器 import_batch 仅接受 UTF-8 Markdown。空行分隔块但不代表文件结束，文件末尾才完成解析。空文件以 EMPTY_NOTE 拒绝，且不登记 document_id。非空的标题后只有空行仍有标题内容，可以登记；不能用正文段落数为零判断文件为空。本节规则仅针对导入，不针对练习输入。', 'import_batch 遇到空文件与标题后只有空行的文件，登记行为分别是什么？', '只有标题没有正文的笔记算空文件吗，为什么与真正空白的文件不同？'),
  ('替换与身份', 'replace_body(document_id, text) 更新同一资料的正文版本，保留 document_id 与集合归属；text_hash 改变时 source_id 也改变。若新旧正文完全相同，不增加正文版本。替换成功不重建旧练习，也不宣称旧题引用已经对应新正文；旧题仍绑定创建时快照。本节不提供资料名称自动更改规则。', 'replace_body 修改正文后 document_id、source_id、集合归属和旧题依据怎样变化？', '重新编辑一份已导入的笔记，会不会变成另一份文件或让以前的题目引用新内容？'),
  ('相同文本与重复导入', '两个不同路径的文件即使 text_hash 相同，也可作为两份资料登记，各有 document_id。文本相同只可复用向量内容缓存，不合并集合归属。重复调用同一 import_request_id 则返回原登记结果，不再创建资料。路径去重和请求幂等是两套规则，不能用内容哈希代替请求身份。', '相同 text_hash 的不同路径，与重复 import_request_id，是同一种去重吗？', '两份内容一样的笔记都要保留，但网络重试不能多建文件，该如何区分？'),
  ('导入失败的原子范围', '一次 import_batch 最多含 12 个文件，登记先全部校验再用一笔事务发布。其中任一文件非法则本批登记全部失败，返回每个失败文件及原因；不留下半批可见资料。预先复制的原始文件可能仍在暂存区，清理暂存文件不等于回滚资料登记。这里的原子性不承诺磁盘与数据库之间具有统一事务。', 'import_batch 有一个非法文件时，其他登记与暂存文件如何处理？', '一次传多份笔记能只成功一半吗，所谓回滚包含磁盘复制吗？')],
  [ ('import_preview', 'import_preview 只读取文件生成预览，不登记资料，也不发出 import_request_id。预览成功不能证明正式批量导入一定成功，发布仍会校验当前正文。'), ('export_batch', 'export_batch 导出当前可见资料，不修改 document_id。导出空集合得到清单文件，不返回 EMPTY_NOTE；这是集合导出而非单文件导入。'), ('文件名称', 'rename_document 只改展示名称，不改变文件路径、正文哈希或版本。展示名称为空会拒绝，但这个错误不是导入器 EMPTY_NOTE。'), ('编码检测', 'encoding_probe 诊断字节编码，报告候选编码但不转码。它不检查 Markdown 内容是否为空，探测成功不等于 import_batch 登记成功。')]),
 ('writer', '数据库写入与只读模式', 'dev', [
  ('busy_ms', '本合成存储层 busy_ms=1500 仅控制遇到 SQLite 写锁时最长等待毫秒数，不限制 SQL 执行时间。等待后仍无法取得锁，返回 WRITE_BUSY，保留调用方操作身份以便重试。等待不是把失败事务提交成功，也不会把正文截短。读取未遇写锁时不消耗这项额度；这是协议配置，不是所有 SQLite 应用的默认值。', 'busy_ms=1500 限制的是语句执行还是写锁等待，超时后返回什么？', '数据库被另一个写入占着，等了一会还是失败，系统应该保留什么？'),
  ('revision_compare', 'revision_compare 在 BEGIN IMMEDIATE 内读取当前 revision，只有与 expected_revision 相等才更新正文并加一。相等才可提交；不等返回 STALE_EDIT，保留已发布的新正文。不能在事务外比较一次就认为写入安全，网络请求也不得在持有此写事务时等待。', 'revision_compare 的比较应放在哪里，不匹配会覆盖当前正文吗？', '两个人从旧页面编辑同一份资料，怎样避免后提交的人抹掉新内容？'),
  ('read_only', 'read_only=true 时查询与历史快照展示仍允许；新增登记、修改标签和计划加入全部拒绝，返回 STORE_READ_ONLY。只读模式不是仅隐藏网页按钮，服务端所有写入口都须检查。评分预览可计算但不得保存学习事件或修改复习日期；成功预览不能作为一次已完成复习的证据。', 'read_only=true 时能评分预览和保存学习事件吗？', '把页面改成只读之后，查看旧题与改变复习时间分别允许吗？'),
  ('事务与外部调用', 'write_then_generate 先用短事务保存用户输入及 pending 状态，然后释放写锁调用模型；模型返回后另开短事务复查身份与版本，再发布回复。模型失败保留输入和 failed 状态；重试复用原轮次。不得把数据库事务跨越整个外部生成过程，外部服务无法与本地 SQLite 一起回滚。', 'write_then_generate 何时释放锁，模型失败或重试如何保存轮次？', '长时间等模型时应该一直锁住数据库吗，失败输入会丢失吗？')],
  [('query_ms', 'query_ms 是查询性能告警阈值，超出只记录告警而不中止 SQL。它不控制写锁等待，也不能代替 busy_ms 或代表成功提交。'), ('连接池', 'pool_size=4 表示只读连接上限，不意味着允许四个同时写事务。连接等待计时独立于 SQL 查询耗时，连接归还不代表事务提交。'), ('备份', 'backup_snapshot 复制一致的数据库快照供恢复演练。备份本身不清除 failed 轮次，也不把只读模式切换成可写模式。'), ('日志轮转', 'rotate_log 按字节大小轮转运行日志。运行日志不是学习事件表，删除日志不会回滚已提交的复习记录，也不会释放 SQLite 写锁。')]),
 ('idempotency', '作业幂等与重新执行', 'test', [
  ('job_key', '提交合成作业时 job_key 在租户内唯一。相同租户、相同 job_key 及相同 payload_hash 的重试返回原 job_id 和当前状态，不插入第二份作业。不同租户可使用同一字符串。幂等身份不包含当前队列长度；同一身份的失败状态也必须可查，不得因为失败就当作从未创建。', 'job_key 相同且 payload_hash 一致的重试会新建 job_id 吗，跨租户呢？', '网络断了不知道提交成功没有，再发一次同样任务会造成重复吗？'),
  ('冲突正文', '同一租户的 job_key 已存在但新 payload_hash 不同，提交返回 KEY_PAYLOAD_CONFLICT，不更改原作业正文。即使原作业失败也不能覆写；想用新正文重新执行，应选择新的 job_key。这个冲突不会自动撤销原作业，并且不能靠更新网页显示状态解决。', '同一 job_key 换了 payload_hash，原任务失败时能覆盖吗？', '用旧的幂等标识发送另一份内容，系统应该把旧任务改掉还是拒绝？'),
  ('attempt 与重试', 'retry_job(job_id) 仅在 failed 状态启动新 attempt，复用原 job_id 和用户提交记录。每个 attempt 有独立执行日志；成功发布采用唯一约束 (job_id, output_kind)，同一种用户可见输出至多一份。running 或 succeeded 的重试返回当前状态，不启动额外 attempt。', 'retry_job 在 failed、running、succeeded 三种状态分别怎样处理？', '失败任务再跑一次，怎样保留执行记录同时不重复创建用户可见结果？'),
  ('幂等保留', '作业删除后的最小幂等墓碑只保留租户、job_key 与删除身份，不保存原正文。相同身份再次提交返回 JOB_DELETED，不创建新作业；用户明确使用新的 job_key 才可新建。墓碑的作用是拒绝迟到重试，不是允许恢复已删正文。此合成协议没有规定墓碑的自动过期天数。', '删除作业后旧 job_key 再提交，墓碑会恢复原正文吗？', '晚到的重试请求能把已删除任务重新创建出来吗？')],
  [('cache_key', '结果缓存 cache_key 由正文哈希与模型版本决定，可跨作业复用计算。命中缓存仍要检查作业状态；缓存标识不是租户内 job_key。'), ('去重窗口', '监控 duplicate_window=60 秒只用来统计相似提交。统计窗口到期不删除幂等墓碑，不保证相似正文只有一个 job_id。'), ('日志身份', 'trace_id 每次 HTTP 请求新建，用于链路排查。trace_id 不稳定，不能用它替代重复请求中保持一致的 job_key。'), ('取消', 'cancel_job 仅阻止后续发布，已提交的输出不会因为取消而物理清除。取消原因存在日志中，取消与删除作业是不同操作。')]),
 ('lease', '队列租约与迟到结果', 'test', [
  ('lease_ms', '合成 worker 的 lease_ms=9000 表示取走作业后九秒内拥有执行资格。heartbeat 成功按服务端当前时间续租九秒，而不是把原到期时间再累加九秒。客户端时钟只作诊断，是否过期以服务端时间判断。租约不是 SQL 写锁；worker 计算期间不长期持有数据库事务。', 'lease_ms=9000 的 heartbeat 从哪个时刻续租，客户端时钟有决定权吗？', '工作进程发心跳后是从旧截止点延长，还是从服务器现在重新计算？'),
  ('fence_token', '每次重新领取过期作业都会得到更大的 fence_token。发布时必须同时匹配 job_id、active_attempt 和当前 fence_token；旧 worker 即使计算成功也只能记录迟到诊断，不得覆盖新结果。持有旧租约截图不证明当前发布资格，资格由提交事务内重新读取的状态决定。', '旧 worker 持有过期 fence_token，能发布比新 worker 更晚完成的结果吗？', '前一个进程突然恢复，把新进程已经完成的结果覆盖了，应该怎样阻止？'),
  ('领取上限', 'claim_limit=3 表示一个 worker 同时领取的未完成作业最多三份。达到上限后不领取第四份，但仍可为已有租约发送心跳。队列中有空位不取消这个限制；claim_limit 不是输出条数上限，也不是每份任务最多尝试三次。', 'claim_limit=3 到达上限还可以 heartbeat 吗，它限制尝试次数吗？', '三个任务都没完成时还能取第四个吗，续租会不会也被禁止？'),
  ('租约过期与资格', '租约过期只允许调度器重新分配执行资格，不自动标记业务结果为失败，也不创建第二条用户输入。旧 attempt 的日志保留以便追踪；新 attempt 发布成功后作业才进入 succeeded。若调度器尚未重领，旧 worker 仍须续租并取得当前资格才能发布，不能把无人竞争当作有效租约。', 'lease 过期会自动新增用户输入或把业务结果设为失败吗？', '执行者失联后任务被重新分派，用户的请求记录应该发生什么变化？')],
  [('SQL timeout', 'sql_timeout_ms 是另一套语句诊断值，超过阈值写性能日志。它不是 lease_ms，不会使 worker 自动续租，也不产生 fence_token。'), ('优先级', 'priority 仅决定待领取作业的排序，数值更大优先。排序不抢占有效租约；高优先级不能绕过旧结果发布时的 fencing 检查。'), ('心跳计数', 'heartbeat_count 累加成功心跳次数，只作观测。计数高不意味着租约未过期，租约资格必须检查服务端截止时间与 token。'), ('队列深度', 'queue_depth 包含等待领取的作业，不包含已领取且执行中的作业。它不能推算单个 worker 的 claim_limit 或业务失败比例。')]),
 ('cache', '缓存版本与范围', 'test', [
  ('vector_cache_key', 'vector_cache_key 由原文内容哈希、embedding 模型版本及分块算法版本组成。仅修改展示名称不使这些向量失效；修改正文或任一模型、分块版本则新键必须重算。缓存存储内容不代表允许向用户检索，查询仍需根据当前资料范围筛选候选。缓存复用不是权限复用。', 'vector_cache_key 包含哪些版本，rename 是否必须重算向量？', '笔记改名字之后有必要重新计算所有向量吗，哪些变化才真正使缓存过期？'),
  ('ttl 与版本', '缓存 ttl_seconds=300 仅用于短期查询结果；正文版本撤销时必须立即禁止使用，不得等三百秒。向量内容缓存不使用这项 TTL 作为权限期限。过期结果重新计算前检查资料版本；不能拿旧缓存替新版本引用原文。TTL 尚未到期不表示正文仍有效。', 'ttl_seconds=300 没到期但正文版本撤销，结果还能返回吗？', '缓存还在有效期内，资料已经被移除，能继续给用户展示旧检索结果吗？'),
  ('负缓存', 'not_found_cache 只缓存已确认的“当前许可范围内无匹配登记”，寿命十秒。权限变更或新增资料时立即清除相关负缓存；网络失败、数据库锁等待和模型失败不能写成 not_found。负缓存不说明资料在整个本地库不存在，只表达当时的查询范围。', 'not_found_cache 能缓存网络失败吗，新增资料后是否等十秒？', '系统一次暂时查不到文件，会不会因为短期缓存一直说没有？'),
  ('cache_hit 计数', 'cache_hit 计数是在版本与范围检查通过之后累加。发现旧键但版本失效，只累加 stale_rejected，不能算成功命中。对外报告缓存命中率时分子采用 cache_hit，分母为完成检查的查询；失败请求另记。打开缓存文件存在也不等于一次可用的命中。', 'cache_hit 与 stale_rejected 的分子口径怎样区分？', '找到旧缓存却不能使用，这应该算命中以美化缓存命中率吗？')],
  [('页面缓存', 'page_cache 缓存静态 CSS 与脚本，不保存资料原文。浏览器重新加载页面缓存不会自动重新计算向量，也不能清除服务端负缓存。'), ('键前缀', 'cache_namespace 只用于防止不同部署的键冲突。命名空间相同不证明 embedding 模型版本一致，仍须检查完整 vector_cache_key。'), ('淘汰', 'lru_capacity=500 表示内存结果条目数上限，不是 TTL 秒数。因容量淘汰不撤销资料身份，也不删除持久向量内容缓存。'), ('缓存预热', 'warm_cache 只对明确选定的有效资料计算向量。预热成功不能恢复已撤销版本，也不授权把预热的所有资料纳入每次问答。')]),
 ('permission', '查询权限与层级集合', 'test', [
  ('scope_snapshot', 'scope_snapshot 在一次请求开始时记录允许资料的 document_id 与正文版本；模型或历史消息提到别的文件不能扩大它。检索只能遍历快照中的有效原文；全库缓存存在不改变本请求权限。用户主动改变范围后应开启新的请求，不能由生成结果自行授权。快照只记录身份和范围，不是全文强塞入提示词。', 'scope_snapshot 能被历史消息中的文件名扩大吗？', '助手以前聊过另一份文件，现在只选当前范围，能顺便拿那份旧资料回答吗？'),
  ('父集合', '查询父集合默认包含自身与所有子集合的当前可用资料；单份文件只有一个直接归属，但在父集合总览可出现。查询子集合不包含兄弟集合。移动文件改变未来请求范围，不复制 document_id，也不让一个文件拥有两个直接归属。历史练习的创建依据不随移动改写。', '父集合、子集合、兄弟集合查询和移动 document_id 的关系是什么？', '打开一个文件夹应看到下级笔记吗，把笔记移过去会产生副本吗？'),
  ('发布复查', 'request_scope_revision 在模型调用前以及回复发布的短事务内各检查一次。中途资料移出当前范围，迟到回复标为 SOURCE_REVOKED，不向用户发布可用答案。仅调用前检查不能防止在途撤销；错误保留用户输入，重试必须重新建立当前范围，不能用旧范围快照绕过撤销。', 'request_scope_revision 只在模型调用前检查够吗，中途移出资料如何处理？', '模型正在回答时我移走了资料，晚到的答案还可以被保存成有效回复吗？'),
  ('历史说明与证据', '历史助手解释可以帮助理解当前用户问题，但不能作为本轮文档事实的直接证据。source_reference 必须落在实际发送给模型的许可原文行内；memory_reference 指向聊天原话，两者身份空间独立。历史提到旧文件不自动授予文档引用权限，表达不确定也不会创建失败复习记录。', 'memory_reference 能替代 source_reference 来证明文档事实吗？', '以前助手说过的一句话，能直接算本次文档答案的引文依据吗？')],
  [('UI 选中', 'sidebar_focus 只表示页面正在展示哪份文件，未必等于 request_scope。切换阅读位置与修改请求许可范围是两个动作，程序不能凭视觉高亮授权。'), ('管理角色', 'collection_owner 表示可编辑集合结构，不自动获得所有私有文件正文的检索权限。管理目录和读取内容需分别检查。'), ('路径前缀', 'path_prefix 用于展示导入位置，不能代替 document_id 与版本校验。相同文件名或磁盘父目录不构成本请求许可集合。'), ('文件摘要', 'document_summary 是导航信息，只可建议进一步读取哪个章节。摘要中的文件名不扩大 scope_snapshot，摘要内容不算实际发送的原文引用。')]),
 ('snapshot', '原文快照与证据行号', 'test', [
  ('source_id', 'source_id 绑定规范化后的文件绝对路径及完整正文哈希；同一 document_id 替换正文后旧 source_id 保留在历史快照，新请求使用新 source_id。source_id 不是用户展示名称，也不单凭行号识别原文版本。旧行号必须连同旧 source_id 使用，不能直接跳到新正文相同行。', '旧 source_id 的 L20 可以直接跳到替换后正文的 L20 吗？', '笔记更新了，之前保存的第20行引文还能拿现在第20行来显示吗？'),
  ('半开字符范围', 'memory_span 用零起点半开字符范围 [start,end)，指向完整存档原话；end 不包含在证据内。若 start=2,end=5，则只许可字符下标2、3、4。它不是字节范围，UTF-8 汉字不能按三个字节推算字符数。存档不会因 prompt 裁剪而改写下标，历史补回必须保留实际可见范围。', 'memory_span=[2,5) 包含哪个字符下标，能按UTF-8字节算吗？', '一条中文聊天引用的结束位置是不是也包含在内，裁剪提示词会改存档位置吗？'),
  ('裁剪与引用', 'visible_line_range 是本轮实际传给生成模型的原文行范围。若原片段 L10-L30 被裁剪为 L10-L18，引用校验只允许 L10-L18，L19-L30 即使存在数据库中也不能引用。裁剪必须在完整原文块或明确连续范围边界完成；不可用模型改写的句子冒充原文。', 'visible_line_range 从L10-L30缩到L10-L18后，L22能作为本轮引用吗？', '数据库存了全文，但只给模型看了前几行，回答能引用没给它看的后面吗？'),
  ('删除与只读历史', '删除聊天撤销原话及依赖它的派生记忆资格，清理可用于召回的摘要与向量。其他聊天自身原话保留，但依赖被删聊天生成的助手解释不得继续当可用记忆。归档只从日常列表收起，仍可召回。旧学习事件保存作答和日期，不凭删除聊天抹去真实发生的作答证据。', '删除聊天与归档对摘要、派生回复及真实学习事件有什么区别？', '把聊天收起来是不是忘掉它，真正删除后其他聊天里抄来的解释还可靠吗？')],
  [('行号显示', 'line_badge 显示一条引用的原文行号，短标识只是界面文本。仅凭 line_badge=20 不能确定 source_id 或当前版本，点击时必须加载绑定快照。'), ('文件锁', 'snapshot_lock 控制并发快照写入，不是聊天 memory_span。锁名中的数字不构成原文证据范围，也不改变字符下标。'), ('导出格式', 'snapshot_export 输出原文和版本元数据。导出不能重新授权已删除聊天的派生记忆，磁盘上有旧副本不等于召回资格有效。'), ('导航标题', 'section_title 仅描述章节路径。相同标题可出现在多个版本里，不能用标题相等替代 source_id、行范围或实际可见性校验。')]),
 ('budget', '生成请求预算协议', 'test', [
  ('input_budget', 'input_budget = context_capacity - reserved_output - estimate_margin。完整输入包括系统、当前问题、历史、材料、工具定义、工具结果、输出 Schema 和协议开销。若容量12000、预留输出2000、估计余量1000，则输入上限9000。计数应使用生成模型 tokenizer 或明确保守估计，不能用检索 embedding tokenizer 代替。', 'context_capacity=12000、reserved_output=2000、estimate_margin=1000 时 input_budget 是多少，包括Schema吗？', '长上下文窗口是不是全部都能装资料，工具声明和回答空间算在哪里？'),
  ('不能截当前问题', '请求超预算先减少可选资料与较早历史展示，保留完整当前用户问题和原始存档。摘要压缩历史不能覆盖原话。若最小安全请求仍放不下，返回 CONTEXT_TOO_SMALL，并保留输入以便重试；不能为了成功静默截掉用户问题条件。估计无法精确时必须保留估计余量并说明口径。', 'CONTEXT_TOO_SMALL 之前可以静默截掉当前用户问题吗？', '用户的问题太长了，把后半段删了再发送能算成功处理吗，失败输入怎么办？'),
  ('工具循环复算', 'Agent 每追加一批工具结果，都重新检查实际完整请求预算；最终结构化回答阶段也重新检查自己的工具结果与输出 Schema。第一次请求合规不代表下一次合规，决策与最终回答不必完全相同但聊天背景一致。超大工具结果需限制实际可见原文并同步缩小引用权限，不得在零工具调用后暗中补检索。', 'Agent首轮预算通过后，工具返回和最终Schema还要复算吗？', '工具取回大量文字以后，沿用调用前的token检查会漏掉哪些内容？'),
  ('summary_publish', '长历史摘要按输入预算压力及新增未覆盖历史触发，不按固定每十条消息调用。生成前记录覆盖轮次和旧摘要 revision，网络调用期间释放写锁；发布前比较 revision 与历史有效版本。摘要失败保留旧有效摘要和全部原话；旧生成结果不得覆盖并发发布的新摘要。', 'summary_publish 失败或旧revision迟到时，应覆盖旧摘要吗，触发按十条一次吗？', '聊天变长以后怎样压缩，后台压缩失败或比另一个结果更晚回来会怎样？')],
  [('检索长度', 'embedding_max_tokens 是向量模型输入限制，准备资料块时使用。它不是生成模型 context_capacity，不能因为向量能编码就认为带Schema的生成请求一定安全。'), ('输出数量', 'answer_items=8 是页面建议最多展示八个要点，不表示预留输出八个 token。列表数量与生成协议的token预算是不同单位。'), ('调用额度', 'request_limit=24 是一次整理任务的调用次数上限，不是单个请求上下文长度。达到次数上限可暂停续跑，不得删当前问题以降低次数。'), ('预览字符', 'preview_chars=400 控制侧栏预览长度，不修改原始存档，也不授予完整原文的引用资格。界面字符限额不能直接当模型输入token数。')]),
 ('evaluation', '检索实验与标签协议', 'test', [
  ('candidate_pool', '方案B把向量与BM25各自前50名用RRF融合，再取最多50个唯一候选。方案C只对这份已保存的B候选池重排，不新增候选；候选召回率按重排前池计算。若必要证据根本不在池里，重排无法救回。单路排名从一开始，缺失一路的RRF贡献为零，不补零分无命中结果。', 'candidate_pool 中没有必要证据，C重排能救回吗，候选召回按什么排名算？', '把五十段资料重新排个顺序，会不会找出原来这五十段之外的新证据？'),
  ('zero_denominator', 'rescue_rate_at3 的分母是B前三未命中但B池含相关证据的可救样例数；没有可救样例时为 null，不能说成功率百分之零或百分之百。regression_rate_at3 的分母是B前三已命中的样例数。失败请求留在质量分母，失败不以零毫秒混入成功暖查询延迟。', 'zero_denominator 时 rescue_rate_at3 怎么显示，失败计入哪些分母？', '所有题本来前三都命中，重排救回率应该说百分之百吗，报错能从成绩里去掉吗？'),
  ('labels_freeze', 'labels_freeze 固定资料哈希、实际分块、问题、许可范围、完整相关性等级与证据组。BEIR只自动计算同一冻结标签下排名指标，不判定标注是否正确。标签来源区分 Agent 自动采用与用户确认；试运行结果不改测试标签，新增资料必须建立新manifest与审核版本。', 'labels_freeze 之后 BEIR 会替用户判断证据正确吗，新增资料能沿用旧标签吗？', '自动评分工具能证明拟好的答案依据没错吗，扩充资料之后原成绩还可直接比较吗？'),
  ('latency_work', 'C的 warm_ms 仅测读取已保存B池后的纯重排，不包括向量编码和BM25。把独立记录的B暖时间加上C暖时间只能称端到端估计，不能称一次真实问答实测。模型首次加载、索引准备与暖查询分别记录，设备或线程不一致时不将延迟差直接归因算法。', 'C warm_ms 与B+C之和分别测了什么，可以称正式问答实测吗？', '只量重排序要多久，能直接对外说整个问答就是这个耗时吗？')],
  [('回答评测', 'answer_accuracy 属于最终生成答案评测，需要核对依据、条件与拒答。检索 Hit@3 本身不能当回答正确率，前几名有证据不保证模型正确使用。'), ('可见评分', 'review_badge 只提示标签来源与状态，不展示被测排名。审核过程中看不到排名有助减少偏置，但徽章本身不是语义正确保证。'), ('样例扩展', 'query_count 描述查询数，不代表独立问题族数。一个命题的两个同义问法应同属一个分区，不得把两个样例当两个独立能力领域。'), ('外部数据', 'external_dataset 只有许可与领域匹配才可使用。外部标准分数不自动代表本地权限过滤、原文行号或特定笔记语料的检索能力。')]),
 ('release', '学习计划与发布策略', 'test', [
  ('add_target', 'add_target 只有用户明确选择后把审核过的临时题加入学习计划。加入可指定根知识库或某个子集合，题目创建时的原文依据与学习目标身份保留；不移动资料文件，也不因为聊天表示疑惑而记录失败复习。未选择临时题不会影响FSRS日期。', 'add_target 加入子集合会移动资料或因疑惑新增失败复习吗？', '助手临时出了一题，没打算长期复习时它会自动排进我的学习计划吗？'),
  ('prerequisite', 'prerequisite 是可审核的前置关系建议，展示理由并允许用户修改、跳过关系或调整顺序。接受后检查有向图无环，但用户仍可自由学习后面的目标。没有真实作答的目标标未知，不生成未经校准的掌握百分比，更不以这个百分比锁关；跳过不修改FSRS事件。', 'prerequisite 接受后能跳过关系和自由学习吗，未知是否算不会？', '没有答过题的人会被标零分而不许学下一章吗，学习顺序能手动改吗？'),
  ('rollout_gate', 'rollout_gate 要求同资料、同权限、同冻结标签的质量与失败案例比较，并记录延迟代价。小试运行前三命中接近上限只能验证接线，不足以证明稳定提升。正式检索升级仍由Agent按需调用，零工具调用不能暗补RAG；若重排失败，应明确保留失败状态而非伪装成成功重排。', 'rollout_gate 能凭少量简单题直接宣布稳定提升并每轮强制RAG吗？', '新检索的试运行成绩很好，马上让所有闲聊都查资料是不是合适？'),
  ('evidence_revision', 'evidence_revision 绑定某个目标真实首题作答及当前有效评分修订。追问不能抹去首题记录，别的能力维度的成功也不能当本维度达标。推荐下一目标时允许提示与不确定证据不足、未测试未知；评分修订保留原始作答，不覆盖历史证据，不改变目标身份。', 'evidence_revision 能用别的能力或追问成功替代首题证据吗？', '解释题答对了，能直接当应用题也会了吗，后来改评分会删掉原回答吗？')],
  [('展示排序', 'display_order 只改变计划列表顺序，不修改依赖关系或复习日期。把目标拖到顶部不表示它已经通过前置核对，也不创建真实作答证据。'), ('FSRS日期', 'due_date 表示真实评分事件驱动的复习到期时间。页面打开次数、聊天疑惑和跳过路线都不是评分事件，不得据此重新调度。'), ('收藏', 'star_target 收藏目标供快速定位，不等于 add_target。收藏临时题不保存学习事件，取消收藏也不撤销已加入的真实复习记录。'), ('灰度展示', 'feature_preview 只展示实验报告入口，不自动改变正式问答检索模式。开启页面入口并不证明通过rollout_gate，更不授权每轮检索全部资料。')])
]


def build():
    names = [name + '.md' for name, *_ in DOCUMENTS]
    queries, section_audit = [], {}
    for name, title, split, rows, distractors in DOCUMENTS:
        # A deliberate protocol, not an assertion about an external product.
        text = f'# {title}\n\n本文件是公开合成检索协议，仅以本文规则为准，不描述真实部署。每节有独立适用条件；其他文件同名字段不能替代本节证据。\n\n'
        ranges = {}
        for index, (heading, body) in enumerate([(r[0], r[1]) for r in rows] + distractors):
            if index >= len(rows):
                body += ' 本节独立描述这个辅助功能，名称相似不代表与其他协议具有相同身份、单位或权限。不能用观察值推断未提供的业务条件，未写的配置应视为信息不足。'
            text += f'## {heading}\n\n{body}\n\n'
            line_end = len(text.rstrip().splitlines())
            identity = f'{name}-{index+1}'
            ranges[identity] = {'file': name+'.md', 'line_start': line_end, 'line_end': line_end}
            section_audit[identity] = {**ranges[identity], 'heading': heading,
                                      'meaning': body, 'role': 'target' if index < len(rows) else 'distractor'}
        (HERE / 'corpus' / (name+'.md')).write_text(text, encoding='utf-8')
        for index, row in enumerate(rows):
            fact = f'{name}-{index+1}'
            for variant, query in enumerate(row[2:], 1):
                queries.append(case(f'{fact}-q{variant}', query, split, name,
                                    'identifier_condition' if variant == 1 else 'semantic_paraphrase',
                                    names, [ranges[fact]], [fact],
                                    f'原文直接覆盖请求的主体、条件与结果：{row[0]}。其他章节主题、对象或操作不同。'))
        # All fact variants and combinations within a domain share one split.
        combinations = [(1, 2), (3, 4)] if split == 'dev' else [(2, 3)]
        for left, right in combinations:
            facts = [f'{name}-{left}', f'{name}-{right}']
            query = f'请分别回答这两个条件问题，并给出各自依据：{rows[left-1][2]} {rows[right-1][3]}'
            queries.append(case(f'{name}-multi-{left}-{right}', query, split, name, 'multi_evidence',
                                names, [ranges[f] for f in facts], facts,
                                '两项独立问题需两组原文，任何一组不能代替另一组；不以只找到其中一项作为完整证据。'))
    negatives = [
        ('idempotency', '这份作业幂等协议规定墓碑满多少天自动删除？', 'missing_parameter', names,
         '原文明确没有规定墓碑自动过期天数；六十秒监控窗口不是保留期限。'),
        ('lease', 'lease_ms 为9000的 worker 具体部署在哪个 IP 地址？', 'missing_fact', names,
         '所有原文只有执行协议，没有端点IP；时间参数不是网络地址。'),
        ('cache', '只在本次许可的release.md里查 vector_cache_key 的三项组成是什么。', 'permission_negative', ['release.md'],
         '答案只在不许可的cache.md，本请求范围内没有组成定义；不能扩大权限。'),
        ('permission', 'collection_owner 的登录密码是什么？', 'missing_fact', names,
         '原文没有凭据；管理角色说明不支持推断密码。'),
        ('snapshot', '只看lease.md，source_id 的路径和正文绑定公式是什么？', 'permission_negative', ['lease.md'],
         '公式在不许可的snapshot.md，租约token不是原文身份。'),
        ('budget', '本文生成模型的厂商和准确型号叫什么？', 'missing_fact', names,
         '资料描述token预算口径，没有指定生成模型厂商或型号。'),
        ('evaluation', '这组实验给出的最终生成回答正确率是多少？', 'missing_metric', names,
         '检索协议没有提供生成答案样例成绩，Hit指标不能冒充最终回答正确率。'),
        ('release', '只看cache.md，FSRS 最优遗忘曲线的全部权重是多少？', 'permission_negative', ['cache.md'],
         '许可资料没有FSRS权重，其他资料也没有权重值；缓存命中不能推导复习模型参数。'),
    ]
    for name, query, category, scope, reason in negatives:
        queries.append(case(name+'-negative', query, 'test', name, category, scope, [], [], reason))
    partials = {
        'writer-2-q2': ['writer-4'], 'writer-4-q1': ['writer-2'],
        'idempotency-1-q2': ['idempotency-3'], 'idempotency-3-q2': ['idempotency-1'],
        'cache-2-q2': ['permission-3'], 'permission-3-q2': ['cache-2'],
        'budget-3-q2': ['budget-1'], 'release-3-q2': ['evaluation-3'],
    }
    for q in queries:
        for fact in partials.get(q['id'], []):
            section = section_audit[fact]
            q['annotations'].append({k:section[k] for k in ('file','line_start','line_end')} |
                                    {'grade':1, 'note':f'部分支持：{section["heading"]}解释其中的版本、身份或预算条件，未覆盖本题完整操作。'})
        # Explicit editorial ledger for every allowed semantic section, including
        # negatives. The binder is only an identity/range projector, not a judge.
        q['section_judgments'] = []
        by_range = {(a['file'],a['line_start']):a for a in q['annotations']}
        for fact, section in section_audit.items():
            if section['file'] not in q['scope']:
                continue
            annotation = by_range.get((section['file'],section['line_start']))
            q['section_judgments'].append({'section_id':fact, 'grade':annotation['grade'] if annotation else 0,
                'reason': annotation['note'] if annotation else
                f'已审“{section["heading"]}”：{section["meaning"].split("。")[0]}。该命题不提供本问题的必要对象及操作条件，不能以同名字段或相邻主题充作答案。'})
    # Two real boundary uncertainties, not another hundred-item review task.
    for identity, reason in {
        'release-4-q2': '问题的“应用题也会了”与“修订删回答”跨两个条件，首题作答身份与能力维度证据在同段；建议此段2级，但其只说明证据资格而非能力本身。请核对2级是否符合本套直接支持定义。',
        'cache-4-q2': '问题使用“美化”作评价，原文给出命中计数口径但没有评价动机；建议口径段2级，TTL撤销段仅为背景0级。请核对是否应给TTL段1级。'
    }.items():
        q = next(q for q in queries if q['id'] == identity)
        q['audit'] = {'status': 'disputed', 'reason': reason}
    suite = {'schema': 1, 'revision': 'retrieval-expanded-v1-100-draft',
             'description': '独立合成扩展集。100查询不等于100独立命题；10问题族，dev2族/test8族。旧20条单独保留。',
             'sources': names, 'queries': queries,
             'protocol': {'label_method': 'author evidence before retrieval; full section semantic audit',
                          'test_exposure': 'new local synthetic examples, not blind external gold',
                          'tuning': 'no test-label-driven tuning; fixed B/C pilot parameters'}}
    assert len(queries) == 100
    for name, value in [('suite.json', suite), ('section-audit.json', section_audit)]:
        (HERE/name).write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def case(identity, text, split, family, category, scope, evidence, facts, reason):
    return {'id': identity, 'text': text, 'split': split, 'family': family, 'category': category,
            'scope': scope, 'answerable': bool(evidence), 'supporting_sections': facts,
            'annotations': [{**e, 'grade': 2, 'note': reason} for e in evidence],
            'evidence_groups': [{'id': 'g'+str(i+1), 'label': f'必要事实：{fact}', 'evidence': [e]}
                                for i, (fact,e) in enumerate(zip(facts,evidence))],
            'audit': {'status': 'checked', 'reason': reason}}


if __name__ == '__main__':
    build()
