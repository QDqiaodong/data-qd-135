# Offline ranked retrieval evaluation v1

面向搜索质量分析，输入k(1..1000)、1..1000个queries(id,judgments)、最多1000个runs(query,docs)。
每query最多1000个唯一doc判断，grade整数0..3；每run最多1000个唯一有序doc，query已知、run唯一。
显式doc顺序就是rank，不额外排序或去重；同doc可属于不同query。未判断doc按grade0计算并保留列表。
只评估前k。P@k分母固定k，短列表不缩小；相关定义grade>0，Recall分母为全部已知相关数R。
AP@k=sum(相关rank处P@rank)/min(R,k)，不是全量AP；DCG gain=2^grade-1、discount=log2(rank+1)，
IDCG取全部判断中最高的前k grade。R=0时Recall/AP/nDCG为null，P仍0；宏平均分别排除null并给有效query数。
没有run的判断query按空结果纳入；输出queries_with_runs区分覆盖。
样例q1 P=2/3、Recall1、AP5/6；q2缺run全0，q3无已知相关，三项指标null。
不抓取搜索服务、不训练模型、不认证判断完整性；未判断当0是明确政策而非真实不相关结论。
浮点指标有正常数值误差，重跑同输入字节确定；不做统计显著性和线上实验推断。

# Evaluation warehouse v2（SQLite 离线评测仓）

面向检索质量团队：同一组判断服务多个检索系统，判断修订后立即得到一致的系统分数与覆盖率。
仓是单个SQLite文件（WAL模式），只用Python标准库；apply/report可在不同进程间接续，无进程内缓存。

## 命令

- `retrieval-eval warehouse-init --db DB --k K`：建立新仓。K为整数1..1000，建仓后固定。
  DB已存在（含符号链接）则失败；失败不留残文件。新仓revision为0，无任何query/system。
- `retrieval-eval warehouse-apply --db DB --source FILE.jsonl`：原子应用一个批次。
  每个成功的非空批次使revision恰增1；空批次（0条记录）成功但revision不变。
  DB不存在时失败且不创建文件。
- `retrieval-eval warehouse-report --db DB --output REPORT [--system ID]`：发布报告。
  未知系统报错；输入或计算失败退出2并保留旧报告，沿用单文件入口的输出路径安全规则
  （输入≠输出、拒绝符号链接、原子替换）。同仓状态报告字节确定。

## 批次事实层

JSONL逐行严格UTF-8 JSON对象：拒绝空行（含纯空白行）、重复键、非有限数值
（NaN/Infinity/溢出如1e999）、未定义字段；末行可无换行；每行至多1MiB；文件不受旧8MB总量上限约束。
四种记录（kind之外字段必须恰好匹配）：

- `{"kind":"query","id":Q,"judgments":[{"doc":D,"grade":G}]}`：整体替换或创建该query的判断。
- `{"kind":"system","id":S}`：声明系统；已存在时允许（幂等）。
- `{"kind":"run","system":S,"query":Q,"docs":[D,...]}`：整体替换或创建显式run。
- `{"kind":"drop_run","system":S,"query":Q}`：删除显式run；run不存在时允许（幂等）。

ID、grade、每query判断数（≤1000唯一doc）、每run长度（≤1000唯一有序doc）沿用v1校验。
同批记录顺序不具有业务含义：run可先于其query/system声明，引用按整批最终事实验证
（run/drop_run的system与query必须存在于仓中或本批声明中）。一个query在一批中只可出现一次；
一对(system,query)的run/drop_run合计只可出现一次；system声明不得重复。
任何错误（含末行错误、缺失引用）使事实、物化结果、revision完全回滚；
首次失败不会留下可误认成功的新仓（init原子建文件，apply单事务）。

## 指标物化层

scores表恰为每个显式run持久化一行逐query指标（与v1 evaluate同一公式：P@k、Recall、AP@k、nDCG
及hits/retrieved/judged/unjudged字段）；aggregates表持久化每系统queries_with_runs与四项指标和。
失效传播规则：

- query判断替换：重算该query全部现存显式run及本批最终保留的run，每个受影响pair至多评分一次。
  判断按query整体重载，未被任何run检出的doc同样改变R、AP分母与IDCG，不按命中文档定位影响。
- run替换/创建：只重算该pair。drop_run：撤掉旧贡献。无关query的显式run不重新评分。
- 禁止为缺run持久化占位pair，禁止逐一遍历全部系统×query求值。

宏平均分母是全仓口径：P的分母是全仓query数，另三项分母是全仓R>0的query数，与系统覆盖无关；
分母为0时value为null并报告eligible_queries=0。因此判断替换使某query的R跨过0时，
事实层的相关性状态（queries.relevant_total与meta中的全仓计数）直接切换该query全部显式行的
null资格，并改变所有系统的宏平均分母——没有该query显式run的系统也在下一份报告看到新资格。
缺run按当前判断合成空排序：P为0，R=0时Recall/AP/nDCG为null，否则为0；
显式空run计入queries_with_runs，缺run不计入。事实变更、显式分数、汇总与revision在同一事务生效；
报告在单读事务中只观察某一个完整revision（WAL快照隔离）。

## 报告

基础报告：`{schema_version:1, k, revision, query_count, systems:[...]}`，systems按ID排序，
每项含system、query_count（全仓query数）、queries_with_runs及四项macro（value、eligible_queries）。
`--system ID`：systems只含该系统，并附加queries数组——全部query按ID排序，
每行含v1逐query指标字段与has_run；缺run行按当前判断合成（空排序语义）。
指标与v1公式在1e-12内一致；报告只从持久化结果与稀疏默认语义得到，不在报告时重新评分。

## 设计与资源代价

- 批次解析流式进行（逐行≤1MiB），记录插入TEMP暂存表，主键即批内去重约束
  （stage_query(qid)、stage_system(sid)、stage_run(system,query)）；
  引用校验、事实写入、旧分捕获、删除均为集合式SQL，排序由SQL完成（temp_store=FILE落盘），
  不把整批或全仓事实读入Python再排序。
- 持久化索引：runs(query)与scores(query)支撑"按query定位受影响run/score"，
  pair主键支撑按对查找；代价是每行额外一次索引维护与磁盘占用，换取单query替换时
  读写成本只随该query的显式run数与批大小增长，不随无关run数增长。
- 重算脏集 = 判断被替换query的最终显式run ∪ 本批run upsert（query未被替换者）；
  每pair恰评分一次；旧分先捕获到TEMP表，聚合按差量（新−旧）更新，queries_with_runs按
  新建/删除run增减。浮点和按差量累积，宏平均误差在1e-12容差内。
- 容量验收（Linux，/proc VmHWM测量，2万query、2万system、20万显式run的稀疏仓）：
  4万条声明批apply峰值约20.5MiB，20万run批apply峰值约28.9MiB，
  全量报告约43.1MiB，单系统报告约28.2MiB，均远低于192MiB上限；
  单query替换的评分调用数恰等于该query最终显式run数（测试以计数器核对）。
- 并发模型：单操作者批处理；写者BEGIN IMMEDIATE串行化（busy_timeout 5s），
  读者经WAL快照读不被写者阻塞；不承诺断电恢复。

# Search operations session warehouse v1（会话、归因与日报）

面向搜索运营团队，对迟到日志和源端更正构建可修订的离线会话与订单归因数据集。
仓是独立的单个SQLite文件（WAL模式），只用Python标准库；`session-apply`/`session-report`
可在不同进程间接续，无进程内缓存；报告只读取已持久化的派生表，不重新会话化或归因。

## 命令

- `retrieval-eval session-init --db DB`：建立新仓。DB已存在（含符号链接）则失败且不留残文件；
  新仓revision为0，无任何事件、会话或订单。
- `retrieval-eval session-apply --db DB --source FILE.jsonl`：原子应用一个事件批次。
  每个成功的非空批次使revision恰增1；空批次（0条记录）成功但revision不变。
  DB不存在时失败且不创建文件。DB与source按resolve比较不得为同一路径。
- `retrieval-eval session-report --db DB --output REPORT`：发布确定性JSON报告。
  输入/输出失败退出2并保留旧报告；输出路径不得是符号链接、不得与DB同路径，临时文件原子替换。
  同仓同revision报告字节确定（键序、数组序、缩进、null位置均固定）。

## 批次事实层与严格JSONL

逐行严格UTF-8 JSON对象：拒绝空行（含纯空白行）、重复键、非有限数值（NaN/Infinity/溢出）、
额外/缺失字段、非对象；末行可无换行；每行（不含换行）至多1MiB；文件不受旧8MB总量上限约束。
两种操作，同批每个全局事件id至多一条操作，行排列不具有业务含义：

- `{"op":"put","id":ID,"user":ID,"at":T,"kind":"view"}`
- `{"op":"put",...,"kind":"search","channel":ID}`（search必须且只能带channel）
- `{"op":"put",...,"kind":"purchase","amount_cents":A}`（purchase必须且只能带amount_cents）
- `{"op":"delete","id":ID}`（删除不存在的事件为幂等no-op，仍计入批次revision）

`at`为严格整数0..4102444800（布尔拒绝）；`amount_cents`为严格整数0..1000000000（布尔拒绝、
允许0金额订单）。put是对同id事件的整体创建或更正，可改变user、时间、种类与载荷。
任何错误（含末行错误）回滚整批：事实、成员关系、归因、日报、revision全部不变。

## 会话组织

每用户按`(at,id)`排序，相邻事件时间差`>1800`秒才断开；恰等1800仍相连。会话id为该会话
最早事件（最小`(at,id)`）的全局事件id；持久化`start/end/event_count`及开始日`day=floor(start/86400)`。
`members`显式物化每个事件到会话的成员关系，支撑边界定位、拆分与归因。迟到桥接事件可把多个
旧会话合并为一个；删除/移走桥接点必须能把大会话拆回；事件改user时原用户与新用户的会话都重算。

## 转化归因与日报

每个现存purchase恰有一条orders行。在同一会话内选择严格位于订单之前（`(at,id)<(订单at,订单id)`，
同秒不算）且时间差`<=3600`秒的最后一个search（按`at DESC,id DESC`）；没有则search_id与channel
均为null，并保留所选search的channel。每笔金额只贡献一次，计入其所属会话开始日（不是订单发生日）。
稀疏持久化`daily_sessions(day,session_count)`与`daily_orders(day,channel,order_count,amount_cents)`，
计数降到0的分组立即删除；null channel在日报中排在同day字符串channel之前。

会话边界变化会驱动受影响订单撤销旧贡献并按新会话语义重新判定，即使订单自身记录未变：
桥接可使原先隔离的订单获得search归因，拆分可撤回归因。事件、成员、归因、日报与revision
在同一`BEGIN IMMEDIATE`事务生效；报告在单读事务中只观察某个完整revision（WAL快照隔离），
交错读者只能看到全旧或全新状态。

## 增量局部性、显式物化与资源代价

- 批次逐行（≤1MiB）流式进入TEMP stage，主键约束批内id唯一；整批或全仓事实从不读入Python。
- 更正先捕获旧`(user,at)`锚点及其最终顺序上的直接前驱/后继，只定位与这些坐标相接的旧会话；
  新事实的修复区间由窗口函数`LAG`/累计`SUM`在SQL内分段，独立用户与同用户独立会话不被扫描。
  `events(user,at,id)`、`members(user,at,event_id)`等索引把边界查找限定为索引区间/点查。
- `members`显式物化的代价是每事件一行及索引维护，换取O(log n)边界定位、可直接审计的成员关系，
  以及删除/拆分时无需重扫全仓；会话/订单/日报均为稀疏表，报告按游标流式写出，不把全量行载入Python。
- 冷仓首仓直接按`(user,at,id)`索引顺序对最终事实做窗口分段，避免长链上的区间自连接；
  增量路径用一跳前驱/后继UNION而不是范围自连接，长会话桥接仍线性于受影响坐标。
- Linux容量实测（/proc VmHWM）：100万事件（10万用户×10，100万会话）apply峰值约33.7MiB、
  全量报告峰值约18.5MiB；10万事件单用户长会话apply峰值约31.7MiB，均远低于192MiB上限。
- 并发模型：单操作者批处理；写者`BEGIN IMMEDIATE`串行化，读者经WAL快照读不被阻塞；
  不承诺断电恢复。

# Contract impact review v1（离线生产契约升级审查）

面向数据平台团队：切换事件数据集版本前，需要知道每个下游消费者是否仍能安全读取。
只用Python标准库；三份输入均为UTF-8 JSON，输出为确定性JSON报告；不访问任何服务。

## 命令

- `retrieval-eval contract-impact --baseline FILE --candidate FILE --consumers FILE --output REPORT`：
  比较同一数据集的baseline与candidate两个版本契约，结合消费者读取声明给出逐消费者判定。
  任一输入无效或输出失败退出2并保留旧报告；成功时原子替换并输出0。
  沿用既有路径安全规则：任一输入与输出相同（按resolve比较）拒绝、输出为符号链接拒绝。
  同输入重跑字节确定，与输入数组排列、对象键顺序及格式化无关。

## 输入

baseline/candidate：`{"dataset": ID, "version": ID, "fields": [...]}`，
字段恰为 `{"path": P, "type": T, "required": B, "nullable": B}`。
consumers：`{"dataset": ID, "consumers": [...]}`，消费者恰为 `{"id": ID, "reads": [...]}`，
读取恰为 `{"path": P, "accepted_types": [T,...], "required": B, "accepts_null": B}`。

- ID与path是非空标识符（去空白后非空、≤128字符、无控制字符）；type限
  string/integer/number/boolean/object/array；required/nullable/accepts_null必须是JSON布尔。
- 拒绝（均退出2）：重复字段路径（同一版本内）、重复消费者ID、同一消费者内重复读取路径、
  未知键或缺键、空标识符、错型、空accepted_types、accepted_types内重复或未知类型、
  三份输入dataset不一致；JSON层拒绝重复键与非有限数值。
- 容量上限：每份输入≤64MB；每版本字段≤100000，消费者≤20000，每消费者读取≤1000。
- 字段的required属性随added/removed快照保留，但不单独产生差异条目、不参与消费者判定。

## 字段差异

按路径比较两个版本，差异按路径排序，即使无消费者读取也完整保留：

- `added`：候选新增，附candidate快照{type, required, nullable}。
- `removed`：候选删除，附baseline快照。
- `changed`：type或nullable变化，changes逐项记录{from, to}；
  同一路径多个属性同时变化只列一次并保留全部变化。

## 消费者判定

每个候选差异 × 每个实际读取该路径的消费者单独判定，不构造消费者×全量字段的笛卡尔积，
也不把字段级差异汇总成全体消费者通用的结论。问题reason：

- `removed_required_read`：必需读取的字段被删除→阻断。
- `removed_optional_read`：可选读取的字段被删除→警告。
- `type_not_accepted`：候选类型不在该消费者accepted_types内→阻断（附candidate_type）。
- `null_not_accepted`：字段从非空变为可空且该消费者accepts_null=false→阻断。
  仅此情形accepts_null影响判定：新增字段、可空→非空、类型不变的可空字段均不产生该问题。

其余为兼容；未读取变更路径的消费者不产生问题。同一消费者同一路径可同时保留多类原因。
消费者状态：任一阻断问题→blocked，否则任一警告→warning，否则compatible。

## 报告

`{schema_version:1, dataset, baseline_version, candidate_version, field_diffs:[...],
consumers:[{id, status, issues:[{path, reason, ...}]}], summary:{...}}`。
consumers按ID排序，每消费者issues按(path, reason)排序；summary给出
blocked/warning/compatible消费者数、消费者总数与三类字段差异数。

## 规模与资源

验收规模：20000字段、2000消费者、每消费者最多40读取。每份输入解析后立即折叠为紧凑元组
并释放JSON树，峰值内存随逻辑模型而非JSON文本增长；影响判定经path→readers索引连接，
工作量随变更路径数与命中读取数增长。Linux峰值RSS（/proc VmHWM）验收约82MiB，低于192MiB上限；
报告仍包含全部字段差异与所有实际相关的消费者问题。

# Human arbitration review pack v1（人工仲裁样本包）

面向检索质量团队：锚点系统与候选系统对同一 query 的前 k 位相关性判断不一致时，需要一份
覆盖查询分组与系统的、可复现的复核清单，而不是另一份汇总分数。只用 Python 标准库，
只读打开现有评测仓；不改变评测、warehouse、contract-impact 与会话入口。

## 命令

- `retrieval-eval review-pack --db DB --groups FILE --anchor ID --top N --budget B --cell-floor F --output REPORT`：
  从评测仓的一个只读快照生成仲裁样本包并原子发布。
  DB 必须是已有评测仓（只读打开，绝不创建）；groups 为 UTF-8 JSON；anchor 必须是已声明系统；
  top 1..100、budget 1..500、cell-floor 1..20。任一输入/参数无效或输出失败退出2并保留旧报告。
  路径安全沿用既有规则：DB、groups 与输出不得同路径（按resolve比较），输出为符号链接拒绝，
  成功时临时文件原子替换。

## 输入

groups：`{"groups": [{"id": G, "queries": [Q, ...]}]}`，组恰好 `id` 与 `queries` 两个字段。

- G 与 Q 沿用仓库标识符规则（去空白后非空、≤128字符、无控制字符）；重复组ID、
  空标识符、未知键或缺失键均拒绝。JSON 层拒绝重复键与非有限数值；文件≤32MB。
- 每个 query 必须且只能属于一组：组内重复、跨组重复、引用仓中不存在的 query、
  以及仓中存在却未被任何组列出的 query，全部拒绝。上限：组≤10000、单组query≤200000、
  grouped query≤200000；覆盖单元（组×候选系统）≤2000000，超出即拒绝而不是截断。
- 分组文件与仓中 query 集合必须精确相同；缺少一个 query 与多出一个 query 同样失败。

## 分歧单元

在同一只读快照（单个延迟读事务，WAL 快照隔离）中读取 revision、k、判断与显式 run。
候选系统为除 anchor 外的全部已声明系统。只有 anchor 与候选都显式存在 run 的 query 才参与
比较；缺任一侧 run 的 query 不产生该候选样本，也不按空结果合成。每个 run 截取前
`min(k, top)` 位（k 固定建仓时，top 由命令给出），逐位置三态：

- 相关：存在该 doc 判断且 grade>0；
- 不相关：存在该 doc 判断且 grade==0；
- 未判断：该 doc 在 query 判断中不存在（grade缺失与grade0明确不同）。

anchor 与候选的三态序列不同（含长度不同、短run）时，该 query 对该候选构成一个分歧单元。
每个 query 只形成一个候选样本，保留所有分歧系统、双方逐位文档与状态、以及全部差异位置；
同一 query 对同一候选至多一个样本。两侧完全相同的候选不生成样本。

## 样本选择

样本选择职责须在预算内选择至多 B 个不同 query。覆盖单元为 `(查询分组, 候选系统)`，
包含没有任何分歧样本的单元（候选数0）。目标是每个具备至少 F 个候选样本的单元选中 F 个；
候选样本少于 F 的单元不达标，报告不得伪报达标。

每轮从未被选中的 query 中按以下元组降序选择，完全相同时按 query ID 升序：

1. 本次选择可填补的未达标单元数：该 query 当前可贡献的、仍差一个即达标的合格单元数
   （当前选中数为 F-1）；
2. 首次覆盖的合格单元数：该 query 可贡献且当前选中数为 0 的合格单元数；
3. 覆盖单元总数：该 query 有分歧样本的单元数；
4. 三态位置差异总数：该 query 对其全部分歧单元的差异位置之和。

达到预算或没有未选 query 时停止；同一 query 不重复选择。选择是单一确定性贪心，
不做多轮再优化。报告逐单元给出候选数、目标数、选中数、是否合格与是否达标，
并以 `coverage_complete` 明确是否所有合格单元达标；样本不足的单元也列出但不计入达标。

只有由同一仓快照计算出的分歧单元才能参与覆盖选择。若某 query 的 run、判断或所属分组改变
其分歧覆盖，下一轮选择必须据此更新配额缺口，不能沿用旧候选的覆盖状态：本命令每次运行都
从当前快照的 cells 重新推导 units/keys，再执行选择，没有任何跨运行缓存。

## 报告

`{schema_version:1, report:"review-pack", revision, k, anchor, top, budget, cell_floor,
groups:[{id, query_count}], systems:{anchor, candidates:[...]}, summary:{...},
samples:[...], units:[...]}`。

- `groups` 按ID排序；`systems.candidates` 按ID排序（anchor 除外）。
- `samples` 按 query ID 排序，每项 `{query, group, systems:[{system, anchor_docs,
  anchor_states, candidate_docs, candidate_states, diff_positions}]}`；`systems` 按系统ID
  排序，只含与该 query 的 anchor 存在分歧的候选。`*_docs` 为截取的文档序列，
  `*_states` 为 `relevant`/`irrelevant`/`unjudged` 序列，`diff_positions` 为从0开始的位置。
- `units` 按 `(group, system)` 排序，每项 `{group, system, candidate_count, target,
  selected, qualified, met}`；`candidate_count` 是该单元候选样本数，`target` 为 F，
  `selected` 为选中数，`qualified=candidate_count>=F`，`met=qualified and selected>=F`。
- `summary`：`query_count`、`group_count`、`compared_queries`（anchor与至少一个候选都有run
  的query数）、`divergence_queries`（有至少一个分歧单元的query数）、`divergence_cells`
  （query×候选分歧单元总数）、`divergence_units`、`qualified_units`、`met_units`、
  `selected_queries`、`budget`、`coverage_complete`。

输入数组排列不影响输出：分组、系统、query、单元皆有全序；JSON 形状等价于
`json.dump(..., indent=2, sort_keys=True)` 加尾随换行，同一 revision 与参数必须字节相同。

## 设计与资源代价

- 仓以 URI `mode=ro` 打开，绝不创建文件；单个延迟 `BEGIN` 固定快照，扫描、覆盖、选择与
  样本载荷全部读同一快照；提交后写入的 revision 不会与旧 cells 混合。报告在事务提交后
  才原子替换目标文件，因此失败不会留下半份报告，成功也不会发布跨 revision 的混合内容。
- 事实按 query 流式处理：任意时刻 Python 只持有当前 query 的判断和其各系统 run（截取到
  `min(k,top)` 位）；分歧 cell、覆盖单元与选择键都落在 `temp_store=FILE` 的 TEMP 表，
  不把全仓判断/run 或全量 cell 载入 Python 内存。
- 选择器只维护每个未选 query 的一行有序键；单元覆盖从 0→1 或 F-1→F 时才批量更新其余
  query 的对应键，其余迁移不触发全量重排，因此总更新量随 cell 数线性，而不是每轮重算。
- 容量验收（Linux，/proc VmHWM）：5万 query、32 系统、前100位、预算500、cell-floor 20，
  实测峰值 RSS 约 28.9MiB，低于 192MiB 上限。报告包含全部样本、全部单元与完整差异位置。
- 并发模型：单操作者批处理；读者经 WAL 快照读不被写者阻塞；不承诺断电恢复。
