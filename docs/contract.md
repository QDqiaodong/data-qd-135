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
