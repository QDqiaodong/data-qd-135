import math

def evaluate(judgments,ranking,k):
    relevant={d for d,g in judgments.items() if g>0};top=ranking[:k];hits=0;ap=0.0;dcg=0.0
    for rank,doc in enumerate(top,1):
        grade=judgments.get(doc,0)
        if grade>0:hits+=1;ap+=hits/rank
        dcg+=(2**grade-1)/math.log2(rank+1)
    ideal=sum((2**g-1)/math.log2(i+2) for i,g in enumerate(sorted(judgments.values(),reverse=True)[:k]))
    return {'precision_at_k':hits/k,'recall_at_k':hits/len(relevant) if relevant else None,
            'average_precision_at_k':ap/min(len(relevant),k) if relevant else None,
            'ndcg_at_k':dcg/ideal if ideal else None,'relevant_total':len(relevant),'hits_at_k':hits,
            'retrieved_at_k':len(top),'judged_at_k':sum(d in judgments for d in top),
            'unjudged_docs_at_k':[d for d in top if d not in judgments]}
