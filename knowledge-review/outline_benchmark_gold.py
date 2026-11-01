"""Agent's initial annotation, kept separate from all extraction inputs."""

import json
from pathlib import Path
from outline_benchmark_prepare import DEFAULT_RUN, write_json, digest


def annotations():
    rows=[]
    def add(doc, topic, ability, conditions, ranges, cross=False, uncertainty='无额外争议；仍待用户确认', parser='intact'):
        number=1+sum(r['document_id']==doc for r in rows)
        rows.append(dict(id=f'{doc}-G{number:02d}',document_id=doc,topic=topic,ability=ability,
            required_conditions=[dict(id=f'C{i+1:02d}',text=c) for i,c in enumerate(conditions)],
            evidence_ranges=[dict(line_start=a,line_end=b) for a,b in ranges],
            cross_section_dependency=cross,annotation_basis='明确规则、反例或可执行示例；优先选择可据此判分的能力，不把主题出现当完整覆盖',
            uncertainty=uncertainty,parsed_evidence_status=parser))

    add('D01','Connection / 正常退出','判断 with con 在正常退出时的事务行为',
        ['存在打开的事务且上下文管理器生效时，正常退出提交事务','描述的是离开 with 的行为，不能据此声称进入 with 自动开启事务'],[(5,9),(16,24)])
    add('D01','Connection / 异常退出','区分执行体异常和提交失败的回滚分支',
        ['with 执行体抛出未捕获异常时回滚','正常执行体之后的 commit 本身失败也触发回滚'],[(8,12)])
    add('D01','autocommit=False','解释提交/回滚后事务重新打开的条件',
        ['限定 autocommit 为 False','提交或回滚后隐式打开新事务；不能误解为进入 with 无条件开事务'],[(13,14),(22,24)])
    add('D01','不执行事务动作的分支','根据连接状态判断上下文管理器何时无操作',
        ['离开 with 时没有打开的事务，管理器不做事务动作','autocommit=True 时同样无操作；不能由 with 推断发生提交或回滚'],[(16,18)])
    add('D01','连接生命周期','区分事务上下文和连接关闭上下文',
        ['Connection 上下文管理器不隐式开启事务，也不关闭连接','需要关闭语义时可使用 contextlib.closing，而不是假设 with con 自动 close'],[(22,24)])

    add('D02','一致性读取瓶颈','解释带过滤条件的 LIST 为何增加控制面负担',
        ['限定文章描述的旧路径：namespace 可在 etcd 过滤','标签/字段选择器可能先拉取整个数据集再由 apiserver 内存过滤','kubelet 只需本节点 Pod 的需求与全量处理形成资源浪费'],[(18,26)])
    add('D02','watch cache 与新鲜度','解释 watch 更新为何不足以单独证明一致读安全',
        ['watch cache 保存快照并接收 etcd watch 更新','接收更新不等于已足够新，旧实现缺少相应保证'],[(30,32)])
    add('D02','progress notifications','按步骤说明缓存一致读的机制',
        ['先检查 watch cache 是否足够新','不足时查询 etcd progress notifications，直到确认足够新再从缓存服务','仍需读取少量 etcd 元数据，不能声称完全不访问 etcd'],[(34,44)])
    add('D02','版本与回退','判断启用缓存一致读的版本条件及回退路径',
        ['文章语境是 Kubernetes v1.31 Beta 默认启用','要求 etcd 3.4.31+ 或 3.5.13+','旧 etcd 自动回退直接从 etcd 服务一致读，不能无条件承诺缓存命中'],[(46,47),(71,83)],True,'按2024文章语境评分，不作为当前所有Kubernetes版本的配置说明')
    add('D02','收益与证据边界','区分机制收益、特定规模实测和普遍保证',
        ['减少 etcd 数据读取与控制面资源消耗可改善延迟/扩展性','文中的百分比与 P99 延迟来自 5000 节点测试，不是任何集群的保证'],[(51,67)],False,'不要求死记百分比；若候选引用数值，必须保持测试规模及P99口径')
    add('D02','已实现与未来工作','区分文章已经启用的机制与分页计划',
        ['v1.31 默认启用只针对支持的 etcd 版本','watch cache 分页在文章中是未来探索，不能说已由本文实现'],[(71,77)],False,'历史文章；不对2026年的分页状态作判断')

    add('D03','11.1 / 映射与查找','按键值映射解释字典及查找失败',
        ['一个键映射一个值，按键而非整数位置查找','不存在的键下标访问产生 KeyError，不能等同 get 默认值'],[(17,26),(76,109)])
    add('D03','11.2 / 计数器','实现并解释字典频数统计',
        ['首次出现初始化为1，之后递增已有计数','get(k, default) 在键存在时返回值、不存在时返回默认值','字典不要求事先枚举所有字符，只为出现的字符存储'],[(149,173),(179,209)])
    add('D03','11.4 / 反查','处理值到键查找的多解与无解',
        ['多个键可能对应同值，必须说明返回一个还是全部','示例逐键搜索并返回第一个匹配，无匹配抛 LookupError'],[(254,284)])
    add('D03','11.5 / 反转映射','将多对一映射反转为一对多结构',
        ['反转时同值对应多个原键，目标值应保存列表','首次创建单元素列表，后续 append，不能覆盖以前的原键'],[(323,348)])
    add('D03','11.6 / memo','用缓存避免递归子问题重复计算',
        ['字典缓存已求得的 Fibonacci 结果，并以0、1为初始已知值','先检查并直接返回已知结果；未知时计算、写入缓存再返回','解决重复子问题，不能只描述为使用递归'],[(432,465)])
    add('D03','11.7 / 名称绑定与可变对象','区分全局名称重新绑定和容器原地修改',
        ['函数中给同名变量赋值默认创建局部名称，重新绑定全局名称需要 global','读取并更新 count 会有局部变量赋值前读取问题','修改全局字典元素不等于重新绑定字典名称，不需要为元素赋值声明 global'],[(493,522),(524,566)])
    add('D03','11.5 → 12.6 / 键与容器','将哈希约束与复合键联系起来',
        ['键必须可哈希，列表不能作为键但可以作为值','元组可表达如姓和名的复合键，按元组解包遍历','不能概括为一切元组无条件可作键；正文例子只保证字符串等可哈希成员的组合'],[(323,328),(380,410),(1180,1200)],True,'最后一项是防止过度泛化的否定约束；正文未完整讲元组含可变成员的哈希实现，不要求候选主动教授额外实现细节')
    add('D03','12.1 / 元组语法和不变性','识别单元素元组并解释不可变',
        ['元组本身不可变','单元素元组需要逗号；只有括号不构成元组'],[(770,809)])
    add('D03','12.2 / 解包','解释并执行交换与序列解包',
        ['右侧表达式先全部求值，再依次赋给左侧变量','普通解包左右数量必须相同，否则 ValueError','右侧可为其他序列，不能限于 tuple'],[(887,917)])

    add('D04','§3 / 可存储条件','按规范判断普通 HTTP 响应能否存储',
        ['理解请求方法、最终状态及适用状态语义；响应无 no-store 等禁令','共享缓存还受 private 和 Authorization 约束','必须具有明确允许缓存/过期信息/可启发式缓存状态等准入依据','缓存扩展可覆盖通用条件；不能把单个 max-age 当成无条件许可'],[(284,331)],True,'范围是§3通用规则，不要求逐字背完所有允许项；must-understand例外见另一Gold')
    add('D04','§4.1 / Vary','匹配缓存变体并处理例外',
        ['不经重新验证直接复用时，Vary 指定字段须与原请求匹配','只允许规范认可的等价规范化；字段缺失只能匹配同样缺失','Vary:* 始终不匹配，不能当作匹配所有请求'],[(528,557)])
    add('D04','§4.2 / freshness','计算新鲜度并解释优先级',
        ['freshness_lifetime > current_age 才是 fresh，等于不算','共享缓存优先 s-maxage，其次 max-age，再 Expires-Date','没有显式过期才考虑适用的启发式；冲突遵循更严格限制'],[(619,624),(654,684)],True)
    add('D04','§4.2.4 + §5.2.2.2 / stale','解释断网不能绕过 must-revalidate',
        ['一般 stale 仅在断连或明确许可等条件下才可返回','显式禁止陈旧响应的指令优先','must-revalidate 要求 stale 成功验证后才能复用，断网须报错而非返回旧数据；504是SHOULD而非绝对MUST'],[(790,799),(1224,1235)],True)
    add('D04','§4.3.4 / 304','选择可由 304 更新的缓存表示',
        ['先限定可用于本请求的初始响应集，不是同URL全部更新','强验证器匹配才更新匹配项；无匹配不得更新','无强但有弱验证器时选最新匹配项；无验证器仅在唯一且原项也无验证器时适用'],[(953,988)])
    add('D04','§5.2.2.3–5 / no-cache与no-store','区分重验证和禁止存储，并保留限定例外',
        ['无参数 no-cache 是未经成功验证不能复用，不是完全禁止存储','有字段参数的 no-cache 可移除指定字段或成功验证后使用，仍受其他限制','no-store 原则上禁止存储请求/响应且不应用于后续请求，适用于私有与共享缓存','支持 must-understand 且理解并实现对应状态缓存要求时，规范 SHOULD 忽略同带的 no-store'],[(1248,1256),(1264,1281),(1297,1313)],True,'条件较多；允许由多个候选共同覆盖，但每个必要条件须有明确证据')
    add('D04','§5.2.2.7 / private','区分 private 的无参数与字段限定形式',
        ['无参数 private 禁止共享缓存存储，私有缓存仍受§3约束','字段限定 private 只禁止共享缓存存列出的字段，其余部分可在满足条件后存储','private 控制存储位置，并不保证隐私安全'],[(1327,1353)])
    add('D04','§5.2.2.10 / s-maxage','解释共享缓存的覆盖和重验证语义',
        ['仅针对共享缓存，覆盖 max-age 和 Expires','包含 proxy-revalidate 语义，stale 后必须成功验证才能复用','可允许带 Authorization 的响应共享复用，但仍受最大年龄和重验证条件约束'],[(1390,1402)],True)

    add('D05','p1 / 字典ADT','用 insert/delete/search 描述字典而非泛泛存储',
        ['insert 插入项目、delete 删除项目、search 按键查找','键唯一；插入相同键覆盖旧项，不能声称为多值映射'],[(9,38)],False,'页1示例 d.items 的数值疑有源文笔误，不把该例的输出数值作为Gold')
    add('D05','p2–3 / 直接寻址与预哈希','解释直接寻址的容量与键域问题',
        ['直接用键索引数组可实现常数操作','要求非负整数键且键宇宙可能过大而浪费空间','预哈希将其他键映到非负整数，但仍要处理实际冲突；键不能在表中变动'],[(109,111),(144,170)],True,'图示未可靠解析；Gold依赖文字，不从导航图标推断细节')
    add('D05','p3–4 / 链式散列','解释散列到小范围、冲突与链式查找',
        ['散列把大键宇宙映到 m 个槽位，不保证无碰撞','同槽位冲突用链表/链式容器保存，查找沿链搜索','最坏全部 n 个键同槽时查找退化为Θ(n)'],[(175,179),(209,221),(269,273)],True,'图被误识别成表格；该Gold以相邻明确文字为依据')
    add('D05','p5 / SUH与负载因子','推导链式散列的期望复杂度及常数条件',
        ['SUH 假设每个键均匀且独立地映到任一槽','n 为键数、m 为槽数，α=n/m','期望操作时间Θ(1+α)；只有负载因子有界才能据此称期望常数，而非最坏恒定'],[(283,304)],False,'α与公式以PDF页5为准；条件由公式与散列模型直接推出')
    add('D05','p5 / division hash','解释取余散列和参数选择的启发',
        ['h(k)=k mod m，输出槽位0到m-1','讲义建议 m 为不接近2或10幂的质数是实践建议，不能声称可保证无冲突'],[(312,317)])
    add('D05','p6 / universal hashing','区分随机函数选择与固定键对的碰撞保证',
        ['对固定的两个不同键，随机选择哈希函数；不是随机改变键','碰撞概率1/m（该讲义给出的族模型），不能说碰撞概率为0','应保留“不同键”前提；不能沿用解析文本的等号错误'],[(360,371)],False,'原PDF页6写 k1≠k2，解析L366丢失否定变为相等；Judge只用可见解析证据时必须标 uncertain/解析冲突，不可编造可见正确符号','contradictory_symbol')
    add('D05','p6 / 线性期望','用指示变量解释期望冲突数',
        ['对其他 n 个键分别使用碰撞指示变量','利用期望线性性把各键碰撞概率求和，得到 n/m=α','这是期望链长论证，不是所有执行都恰好α'],[(374,402)],False,'求和与分数二维排版被压平；原PDF页6可核对，解析证据只部分可靠','formula_flattened')

    add('D06','R01–03,R06 / 幂等作用域','判断重放、冲突和记录失效',
        ['键为租户、大小写敏感action、request_id三元组','同键同载荷completed重放，running只返回in_progress；同键异载荷冲突','首次接受起24小时TTL，读/重放不续期；过期可重新执行，不保证无限exactly-once'],[(7,17)])
    add('D06','R07–08 / 发布栅栏','解释事务边界与旧执行失效',
        ['领取/发布用短SQLite事务，网络调用在事务外','发布须同时匹配当前attempt及source_revision','暂停先撤销attempt再协作取消；晚到结果不能发布或覆盖其他已完成批次'],[(21,23)])
    add('D06','R09,R12 / 执行资源','区分连接、实际工作和人机等待',
        ['每进程最多2个实际工作，断连只发取消信号','Future退出且I/O清理后才释放槽，不能推断远端已停','waiting_user保存状态并释放槽，确认后重新准入'],[(25,31)])
    add('D06','R10 / 队列边界','按三个边界判断排队拒绝',
        ['容量同时受4请求与32768字节两维约束，不可只检查一维','等待超过2秒拒绝，不可无限排队'],[(27,27)],False,'原文“最多/不超过”与“达到即拒绝”在恰好等于边界时有歧义；本Gold不对等号边界评分，是否改写资料由用户本次审核决定')
    add('D06','R13–14 / 原子限流','解释固定窗口与并发限制的区别',
        ['首次接受起10秒固定窗口，每租户最多50次，Redis Lua原子更新和过期','不是滑动窗口或token bucket','限速与每进程实际并发限制互相独立'],[(25,25),(35,37)],True)
    add('D06','R04,R15–16,R19–20 / 重试','组合状态、身份、协议和额度约束决定能否重试',
        ['429最多2次显式重试，遵循Retry-After且有额度；401和载荷冲突不自动重试','超时不证明未执行，先确认原键未completed且业务支持同键重放，否则unknown','重试保留同租户/action/键/载荷，不能换ID绕过','失败、回退、重做均计入任务12次额度；两次重试不额外赠送且SDK隐式重试关闭'],[(13,13),(39,50),(66,68)],True)
    add('D06','R17 / 计量表和单位','正确计算缓存输入成本并保留未知用量',
        ['input包含cached子集，非缓存输入=input-cached，不能双计','每1000tokens非缓存2、缓存0.5、输出8，单位为虚构credits不是实际货币','无usage记未知，不填0'],[(52,60)])
    add('D06','R18,R23–24 / 去重边界','区分实际调用去重与学习能力去重',
        ['按唯一请求汇总，superseded父批真实调用仍算，展示副本不重复算','同主题解释与应用不同能力可分别保留','重复案例不自动形成新规则或新背诵目标'],[(62,62),(74,76)],True)
    return rows


def prepare(directory=DEFAULT_RUN):
    directory=Path(directory)
    if (directory/'gold-approved.json').exists():
        raise ValueError('Approved gold must not be regenerated')
    corpus=json.loads((directory/'corpus.json').read_text(encoding='utf-8'))
    docs={d['id']:d for d in corpus['documents']}
    rows=annotations()
    for r in rows:
        d=docs[r['document_id']];lines=(directory/d['source_file']).read_text(encoding='utf-8').splitlines()
        r['source_sha256']=d['sha256']
        for e in r['evidence_ranges']:
            a,b=e['line_start'],e['line_end']
            assert 1<=a<=b<=len(lines),(r['id'],e)
            e['text']='\n'.join(lines[a-1:b])
        if r['document_id']=='D05':
            r['original_pdf']='corpus/raw/D05-MIT6_006F11-lec08.pdf'
            r['page_basis']=r['topic'].split(' / ')[0]
    write_json(directory/'GOLD.json',dict(version=1,status='agent_draft_pending_user_review',
        exhaustive=False,scope='已标注关键目标覆盖率；非整份文档知识覆盖率',goals=rows))
    review=directory/'review';review.mkdir(exist_ok=True)
    for d in docs.values():
        own=[r for r in rows if r['document_id']==d['id']]
        lines=['# '+d['id']+' Gold初标：'+d['title'],'',
            '状态：Agent 初标，待用户审核。必要条件须全部被正式候选明确覆盖才记 complete；允许多候选共同覆盖，必须记录对应条件与候选ID。','',
            f"原文：[规范化文本](../{d['source_file'].replace(chr(92),'/')})；SHA-256：`{d['sha256']}`。",'',
            '证据行号采用规范化文本的1-based行号；原始抓取/PDF及许可见corpus.json。','']
        for r in own:
            lines += [f"## {r['id']} · {r['topic']}",'',r['ability'],'','完整覆盖必须包含：','']
            lines += [f"- {c['id']}：{c['text']}" for c in r['required_conditions']]
            ranges=', '.join(f"L{e['line_start']}–L{e['line_end']}" for e in r['evidence_ranges'])
            lines += ['',f"证据：{ranges}；跨节依赖：{'是' if r['cross_section_dependency'] else '否'}。",
                f"标注依据：{r['annotation_basis']}。",f"不确定性：{r['uncertainty']}。解析状态：`{r['parsed_evidence_status']}`。",'',
                '<details><summary>原文证据（完整证据同时保存在GOLD.json）</summary>','']
            for e in r['evidence_ranges']:
                lines += [f"L{e['line_start']}–L{e['line_end']}：",'', '```text',e['text'],'```','']
            lines += ['</details>','']
        (review/(d['id']+'-GOLD.md')).write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print('Draft Gold goals:',len(rows))


if __name__=='__main__':prepare()
