from .common import require,ident,number,rows
from .metrics import evaluate

def analyze(data):
    require(data,'k queries runs');k=number(data['k'],1,1000);queries={};runs={}
    for q in rows(data['queries'],1000):
        require(q,'id judgments');qid=ident(q['id']);judgments={}
        if qid in queries:raise ValueError('duplicate query')
        for j in rows(q['judgments'],1000):
            require(j,'doc grade');doc=ident(j['doc']);grade=number(j['grade'],0,3)
            if doc in judgments:raise ValueError('duplicate document judgment')
            judgments[doc]=grade
        queries[qid]=judgments
    if not queries:raise ValueError('queries cannot be empty')
    for r in rows(data['runs'],1000):
        require(r,'query docs');qid=ident(r['query']);docs=[ident(d) for d in rows(r['docs'],1000)]
        if qid in runs or qid not in queries or len(set(docs))!=len(docs):raise ValueError('duplicate/unknown run or duplicate ranked document')
        runs[qid]=docs
    result=[{'query':q,'has_run':q in runs,**evaluate(j,runs.get(q,[]),k)} for q,j in sorted(queries.items())]
    macro={}
    for key in ['precision_at_k','recall_at_k','average_precision_at_k','ndcg_at_k']:
        values=[r[key] for r in result if r[key] is not None]
        macro[key]={'value':sum(values)/len(values) if values else None,'eligible_queries':len(values)}
    return {'k':k,'query_count':len(queries),'queries_with_runs':len(runs),'queries':result,'macro':macro,
            'policy':'Unjudged grade=0; precision denominator k; AP denominator min(known relevant,k); absent runs are empty.'}
