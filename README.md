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
warehouse.py 提供批次事实层、指标物化层与稀疏汇总报告，cli.py 衔接数据链路。
fixtures 为明确自制样例，代码与样例采用MIT许可。

## 业务与样例结果

固定k与显式判断政策；q1的P@3=2/3、AP@3=5/6，漏跑query按空结果计入，零相关query指标保留null。