# data-qd-135 · 检索排序离线评测

Judgment-aware ranked retrieval evaluation and query coverage。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

## 安装与演示

Python >=3.11，运行和测试仅用标准库；先确认 `python3 --version`。
每仓建立独立环境，构建工具锁版本。首次安装无公共缓存时可能联网；业务运行不访问网络。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m pip check
.venv/bin/retrieval-eval --source fixtures/input.json --output run/report.json
.venv/bin/python -m unittest discover -s tests -v
```

本地已有wheel时，安装构建工具可加 `--no-index --find-links <公共wheel缓存目录>`。
修改源码后重新安装。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

## SQLite 评测仓（多系统共用判断）

同一组判断可持续服务多个检索系统：批次事实（query判断、system声明、run、drop_run）以JSONL
原子写入单文件仓，每个成功的非空批次使revision恰增1；判断修订后下一份报告立即给出一致的
系统分数与覆盖率。指标口径与单文件入口一致（见 [数据契约](docs/contract.md) 的 warehouse v2 节）。

```sh
.venv/bin/retrieval-eval warehouse-init --db run/eval.db --k 3
.venv/bin/retrieval-eval warehouse-apply --db run/eval.db --source run/batch1.jsonl
.venv/bin/retrieval-eval warehouse-report --db run/eval.db --output run/warehouse-report.json
.venv/bin/retrieval-eval warehouse-report --db run/eval.db --output run/s1.json --system s1
```

src/retrieval_eval/model.py 为业务模型，相关算法模块独立组织；common.py 提供受限JSON及报告发布，
warehouse.py 提供批次事实层、指标物化层与稀疏汇总报告，contract.py 提供契约升级审查，cli.py 衔接数据链路。
fixtures 为明确自制样例，代码与样例采用MIT许可。

## 人工仲裁样本包（review-pack）

当锚点系统与候选系统对同一查询的前 k 位相关性判断不同，质量团队需要覆盖查询分组与系统的
可复现复核清单。该命令只读打开现有评测仓，在同一快照内比较三态序列，按预算贪心选择查询，
逐单元报告候选数/目标数/选中数与是否达标，并输出双方逐位文档与状态；缺 run、短 run、
未判断与不同 k/top 都有确定语义。报告绑定仓 revision，同 revision 与参数字节相同。

```sh
.venv/bin/retrieval-eval review-pack --db run/eval.db --groups run/groups.json \
  --anchor s1 --top 10 --budget 100 --cell-floor 3 --output run/review-pack.json
```

容量实测：5万 query、32 系统、前 100 位、预算 500、cell-floor 20 的峰值 RSS 约 28.9MiB，
低于 192MiB 上限。口径、选择规则与报告字段见 [数据契约](docs/contract.md) 的 review pack 节。

## SQLite 会话与归因仓（session commands）

新增三个独立入口，使用与评测仓分离的SQLite数据库，不改变检索评测、warehouse与contract-impact入口：

```sh
.venv/bin/retrieval-eval session-init   --db run/session.db
.venv/bin/retrieval-eval session-apply  --db run/session.db --source run/events.jsonl
.venv/bin/retrieval-eval session-report --db run/session.db --output run/session-report.json
```

严格JSONL逐行（每行≤1MiB，无整文件8MB限制）支持`put`/`delete`事件更正；会话按用户`(at,id)`排序、
相邻差`>1800`秒断开，显式物化成员关系；订单在同会话3600秒窗口内归因到严格在前的最后一个search，
金额按会话开始日入日报。事实、会话、归因、日报与revision在同一事务原子生效，报告只读持久化结果。
100万事件apply峰值约33.7MiB、报告峰值约18.5MiB，详见 `docs/contract.md` 的会话仓章节。

## 离线生产契约升级审查

切换事件数据集版本前，逐消费者判定是否仍能安全读取：完整字段差异（新增/删除/类型/可空性）
结合每个实际读取变更路径的消费者单独判定，输出 blocked/warning/compatible 状态与原因。
判定口径与容量上限见 [数据契约](docs/contract.md) 的 contract impact 节。

```sh
.venv/bin/retrieval-eval contract-impact --baseline fixtures/contract/baseline.json --candidate fixtures/contract/candidate.json --consumers fixtures/contract/consumers.json --output run/contract-report.json
```

样例中 user.age 类型放宽且变为可空：只接受integer且拒绝null的fraud-scorer被阻断并同时保留两类原因，
接受number与null的dashboard在该字段上无问题（因legacy.code可选删除得到warning）；
legacy.code 的删除阻断必需读取它的archiver。

## 业务与样例结果

固定k与显式判断政策；q1的P@3=2/3、AP@3=5/6，漏跑query按空结果计入，零相关query指标保留null。