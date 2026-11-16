
import json, os, sqlite3, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from retrieval_eval import warehouse
from retrieval_eval.metrics import evaluate
from retrieval_eval.model import analyze

ROOT=Path(__file__).resolve().parents[1]
CLI=[sys.executable,'-m','retrieval_eval.cli']
RSS_LIMIT=192*1024  # KiB on Linux

def jsonl(*records):
    return ''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in records)

def cli(*args):
    return subprocess.run(CLI+list(args),capture_output=True,text=True)

def peak_rss(pid):
    try:
        with open('/proc/%d/status'%pid) as status:
            for line in status:
                if line.startswith('VmHWM'):
                    return int(line.split()[1])  # KiB
    except OSError:
        return 0
    return 0

def cli_rss(*args):
    # Linux: ru_maxrss is inherited across fork/exec, so wait4 cannot measure a child
    # started by an already-large test process; poll the child's own VmHWM instead.
    proc=subprocess.Popen(CLI+list(args),stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    peak=0
    while proc.poll() is None:
        peak=max(peak,peak_rss(proc.pid))
        time.sleep(0.005)
    peak=max(peak,peak_rss(proc.pid))
    stderr=proc.stderr.read().decode();proc.stderr.close()
    return proc.returncode,peak,stderr

class Case(unittest.TestCase):
    def setUp(self):
        self.td=tempfile.TemporaryDirectory();self.dir=Path(self.td.name);self.db=self.dir/'w.db'
    def tearDown(self):
        self.td.cleanup()
    def init(self,k=3):
        warehouse.init(self.db,k)
    def source(self,text,name='batch.jsonl'):
        path=self.dir/name;path.write_text(text,encoding='utf-8');return path
    def apply(self,text,expect=0):
        result=cli('warehouse-apply','--db',str(self.db),'--source',str(self.source(text)))
        self.assertEqual(result.returncode,expect,result.stderr)
        return result
    def report(self,system=None,expect=0,name='report.json'):
        out=self.dir/name
        args=['warehouse-report','--db',str(self.db),'--output',str(out)]
        if system is not None:args+=['--system',system]
        result=cli(*args)
        self.assertEqual(result.returncode,expect,result.stderr)
        return json.loads(out.read_text()) if expect==0 else result
    def entry(self,report,system):
        for s in report['systems']:
            if s['system']==system:return s
        self.fail('system not in report: '+system)
    def close(self,a,b,path=''):
        if a is None or b is None:self.assertEqual(a,b,path);return
        if isinstance(a,float) or isinstance(b,float):
            self.assertAlmostEqual(a,b,delta=1e-12,msg=path);return
        if isinstance(a,dict):
            self.assertEqual(set(a),set(b),path)
            for k in a:self.close(a[k],b[k],path+'/'+str(k))
            return
        if isinstance(a,list):
            self.assertEqual(len(a),len(b),path)
            for i,(x,y) in enumerate(zip(a,b)):self.close(x,y,path+'/'+str(i))
            return
        self.assertEqual(a,b,path)
    def pairs(self):
        con=sqlite3.connect('file:%s?mode=ro'%self.db,uri=True)
        try:
            runs=con.execute('SELECT COUNT(*) FROM runs').fetchone()[0]
            scores=con.execute('SELECT COUNT(*) FROM scores').fetchone()[0]
        finally:con.close()
        return runs,scores

class Counting:
    def __init__(self):
        self.calls=0;self.real=warehouse.evaluate
    def __call__(self,judgments,ranking,k):
        self.calls+=1;return self.real(judgments,ranking,k)
    def __enter__(self):
        warehouse.evaluate=self;return self
    def __exit__(self,*args):
        warehouse.evaluate=self.real

class Facts(Case):
    def test_out_of_order_declarations(self):
        self.init()
        text=jsonl({'kind':'run','system':'s1','query':'q1','docs':['a']},
                   {'kind':'drop_run','system':'s2','query':'q2'},
                   {'kind':'query','id':'q1','judgments':[{'doc':'a','grade':1}]},
                   {'kind':'query','id':'q2','judgments':[]},
                   {'kind':'system','id':'s1'},{'kind':'system','id':'s2'})
        self.apply(text)
        r=self.report()
        self.assertEqual(r['revision'],1)
        self.assertEqual([s['system'] for s in r['systems']],['s1','s2'])
        self.assertEqual(self.entry(r,'s1')['queries_with_runs'],1)
        self.assertEqual(self.pairs(),(1,1))
    def test_duplicate_pair_rejected(self):
        self.init()
        base={'kind':'query','id':'q1','judgments':[]}
        dup=jsonl({'kind':'system','id':'s1'},base,
                  {'kind':'run','system':'s1','query':'q1','docs':[]},
                  {'kind':'drop_run','system':'s1','query':'q1'})
        self.apply(dup,expect=2)
        self.assertEqual(self.report()['revision'],0)
        self.assertEqual(self.pairs(),(0,0))
        dup2=jsonl({'kind':'system','id':'s1'},base,
                   {'kind':'run','system':'s1','query':'q1','docs':['a']},
                   {'kind':'run','system':'s1','query':'q1','docs':['b']})
        self.apply(dup2,expect=2)
        self.assertEqual(self.report()['revision'],0)
    def test_duplicate_query_and_system_rejected(self):
        self.init()
        self.apply(jsonl({'kind':'query','id':'q1','judgments':[]},
                         {'kind':'query','id':'q1','judgments':[]}),expect=2)
        self.apply(jsonl({'kind':'system','id':'s1'},{'kind':'system','id':'s1'}),expect=2)
        r=self.report();self.assertEqual(r['revision'],0);self.assertEqual(r['systems'],[])
    def test_bad_last_line_rolls_back(self):
        self.init()
        text=jsonl({'kind':'system','id':'s1'},{'kind':'query','id':'q1','judgments':[]})+'{"kind":"system",'
        self.apply(text,expect=2)
        r=self.report();self.assertEqual(r['revision'],0);self.assertEqual(r['systems'],[]);self.assertEqual(r['query_count'],0)
    def test_missing_reference_rejected(self):
        self.init()
        self.apply(jsonl({'kind':'system','id':'s1'},{'kind':'run','system':'s1','query':'nope','docs':[]}),expect=2)
        self.apply(jsonl({'kind':'query','id':'q1','judgments':[]},{'kind':'run','system':'nope','query':'q1','docs':[]}),expect=2)
        self.apply(jsonl({'kind':'drop_run','system':'ghost','query':'also_ghost'}),expect=2)
        r=self.report();self.assertEqual(r['revision'],0);self.assertEqual(r['systems'],[])
    def test_empty_batch_keeps_revision(self):
        self.init()
        self.apply('')
        self.assertEqual(self.report()['revision'],0)
        self.apply(jsonl({'kind':'system','id':'s1'}))
        self.assertEqual(self.report()['revision'],1)
        self.apply('')
        self.assertEqual(self.report()['revision'],1)
    def test_idempotent_records_still_count_as_batch(self):
        self.init()
        self.apply(jsonl({'kind':'system','id':'s1'},{'kind':'query','id':'q1','judgments':[]}))
        self.apply(jsonl({'kind':'system','id':'s1'},{'kind':'drop_run','system':'s1','query':'q1'}))
        self.assertEqual(self.report()['revision'],2)
    def test_apply_never_creates_warehouse(self):
        missing=self.dir/'missing.db'
        result=cli('warehouse-apply','--db',str(missing),'--source',str(self.source(jsonl({'kind':'system','id':'s1'}))))
        self.assertEqual(result.returncode,2)
        self.assertFalse(missing.exists())
        result=cli('warehouse-report','--db',str(missing),'--output',str(self.dir/'o.json'))
        self.assertEqual(result.returncode,2)
        self.assertFalse(missing.exists())
    def test_first_failure_leaves_honest_warehouse(self):
        self.init()
        self.apply('{"kind":"system","id":"s1"}\nnot json\n',expect=2)
        r=self.report()
        self.assertEqual((r['revision'],r['query_count'],r['systems']),(0,0,[]))
        self.assertEqual(self.pairs(),(0,0))
    def test_jsonl_strictness(self):
        self.init()
        good={'kind':'system','id':'s1'}
        bad=['\n',  # blank line
             '   \n',  # whitespace-only line
             '{"kind":"system","id":"s1","id":"s2"}\n',  # duplicate key
             '{"kind":"query","id":"q","judgments":[{"doc":"d","grade":NaN}]}\n',
             '{"kind":"query","id":"q","judgments":[{"doc":"d","grade":Infinity}]}\n',
             '{"kind":"query","id":"q","judgments":[{"doc":"d","grade":1e999}]}\n',
             '{"kind":"system","id":"s1","extra":1}\n',  # extra field
             '[1,2]\n',  # not an object
             '{"kind":"mystery","id":"s1"}\n',  # unknown kind
             '{"kind":"system","id":"s1","docs":[]}\n',  # extra field on system
             '\xff\xfe\n',  # invalid UTF-8
             '{"kind":"system","id":"'+'x'*200+'"}\n',  # id too long
             '{"kind":"system","id":"s1"}\n\n',  # trailing blank line
             '{"kind":"system","id":"s1"}\n'+(' '* (1024*1024+1))+'\n']  # line over 1 MiB
        for i,text in enumerate(bad):
            self.apply(text,expect=2)
            self.assertEqual(self.report()['revision'],0,'case %d'%i)
        self.apply(json.dumps(good)+'\n')
        self.assertEqual(self.report()['revision'],1)
    def test_last_line_without_newline(self):
        self.init()
        self.apply('{"kind":"system","id":"s1"}')
        r=self.report();self.assertEqual(r['revision'],1);self.assertEqual(len(r['systems']),1)
    def test_non_ascii_identifiers(self):
        self.init()
        self.apply(jsonl({'kind':'system','id':'系统甲'},
                         {'kind':'query','id':'查询一','judgments':[{'doc':'文档甲','grade':2}]},
                         {'kind':'run','system':'系统甲','query':'查询一','docs':['文档甲','未判文档']}))
        r=self.report(system='系统甲')
        row=r['systems'][0]['queries'][0]
        self.assertEqual(row['query'],'查询一')
        self.assertEqual(row['unjudged_docs_at_k'],['未判文档'])
        self.assertAlmostEqual(row['recall_at_k'],1.0)
        again=self.report(system='系统甲')
        self.assertEqual(json.dumps(r,sort_keys=True),json.dumps(again,sort_keys=True))
    def test_validation_rules(self):
        self.init()
        bad=[{'kind':'query','id':'q','judgments':[{'doc':'d','grade':True}]},
             {'kind':'query','id':'q','judgments':[{'doc':'d','grade':4}]},
             {'kind':'query','id':'q','judgments':[{'doc':'d','grade':-1}]},
             {'kind':'query','id':'q','judgments':[{'doc':'d','grade':1.5}]},
             {'kind':'query','id':'','judgments':[]},
             {'kind':'query','id':' q','judgments':[]},
             {'kind':'query','id':'q','judgments':[{'doc':'d','grade':1},{'doc':'d','grade':2}]},
             {'kind':'query','id':'q','judgments':[{'doc':'d%d'%i,'grade':1} for i in range(1001)]},
             {'kind':'run','system':'s','query':'q','docs':['a','a']},
             {'kind':'run','system':'s','query':'q','docs':['d%d'%i for i in range(1001)]},
             {'kind':'query','id':'q','judgments':[{'doc':'d','grade':1,'x':0}]}]
        for record in bad:
            self.apply(jsonl(record),expect=2)
        self.assertEqual(self.report()['revision'],0)
    def test_init_rules(self):
        for k in (0,1001,-5):
            result=cli('warehouse-init','--db',str(self.db),'--k',str(k))
            self.assertEqual(result.returncode,2)
            self.assertFalse(self.db.exists())
        result=cli('warehouse-init','--db',str(self.db),'--k','nan')
        self.assertEqual(result.returncode,2)
        self.assertFalse(self.db.exists())
        self.init(k=7)
        before=self.db.read_bytes()
        result=cli('warehouse-init','--db',str(self.db),'--k','3')
        self.assertEqual(result.returncode,2)
        self.assertEqual(self.db.read_bytes(),before)
        r=cli('warehouse-report','--db',str(self.db),'--output',str(self.dir/'o.json'))
        self.assertEqual(json.loads((self.dir/'o.json').read_text())['k'],7)

class Metrics(Case):
    def seed(self):
        self.init()
        self.apply(jsonl({'kind':'system','id':'s1'},{'kind':'system','id':'s2'},
                         {'kind':'query','id':'q1','judgments':[{'doc':'H','grade':3},{'doc':'G','grade':1}]},
                         {'kind':'query','id':'q2','judgments':[{'doc':'Z','grade':1}]},
                         {'kind':'run','system':'s1','query':'q1','docs':['G']},
                         {'kind':'run','system':'s1','query':'q2','docs':['Z']}))
    def test_unretrieved_doc_changes_idcg(self):
        self.seed()
        before=self.report(system='s1')
        q1=[q for q in before['systems'][0]['queries'] if q['query']=='q1'][0]
        self.assertAlmostEqual(q1['ndcg_at_k'],1.0/(7+1/2**0.5) if False else 1.0/(7+1/__import__('math').log2(3)))
        # H is never retrieved; lowering its grade must still change IDCG and nDCG.
        self.apply(jsonl({'kind':'query','id':'q1','judgments':[{'doc':'H','grade':1},{'doc':'G','grade':1}]}))
        after=self.report(system='s1')
        q1b=[q for q in after['systems'][0]['queries'] if q['query']=='q1'][0]
        import math
        self.assertAlmostEqual(q1b['ndcg_at_k'],1.0/(1+1/math.log2(3)))
        self.assertNotAlmostEqual(q1['ndcg_at_k'],q1b['ndcg_at_k'])
        # adding a relevant doc no run retrieves grows R and the AP/Recall denominators
        self.apply(jsonl({'kind':'query','id':'q1','judgments':[{'doc':'H','grade':1},{'doc':'G','grade':1},{'doc':'M','grade':2}]}))
        r=self.report(system='s1')
        q1c=[q for q in r['systems'][0]['queries'] if q['query']=='q1'][0]
        self.assertEqual(q1c['relevant_total'],3)
        self.assertAlmostEqual(q1c['recall_at_k'],1/3)
        self.assertAlmostEqual(q1c['average_precision_at_k'],1/3)
    def test_run_replace_and_drop(self):
        self.seed()
        self.apply(jsonl({'kind':'run','system':'s1','query':'q1','docs':['H','G']}))
        r=self.report(system='s1')
        q1=[q for q in r['systems'][0]['queries'] if q['query']=='q1'][0]
        self.assertEqual(q1['hits_at_k'],2)
        self.assertAlmostEqual(q1['recall_at_k'],1.0)
        self.assertEqual(self.entry(self.report(),'s1')['queries_with_runs'],2)
        self.apply(jsonl({'kind':'drop_run','system':'s1','query':'q1'}))
        r=self.report(system='s1')
        q1=[q for q in r['systems'][0]['queries'] if q['query']=='q1'][0]
        self.assertFalse(q1['has_run'])
        self.assertAlmostEqual(q1['recall_at_k'],0.0)
        self.assertEqual(self.entry(self.report(),'s1')['queries_with_runs'],1)
        self.assertEqual(self.pairs(),(1,1))
        self.apply(jsonl({'kind':'drop_run','system':'s1','query':'q1'}))  # absent drop is a no-op
        self.assertEqual(self.pairs(),(1,1))
    def test_explicit_empty_run_counts(self):
        self.init()
        self.apply(jsonl({'kind':'system','id':'s1'},{'kind':'system','id':'s2'},
                         {'kind':'query','id':'q1','judgments':[{'doc':'a','grade':1}]},
                         {'kind':'run','system':'s1','query':'q1','docs':[]}))
        r=self.report()
        self.assertEqual(self.entry(r,'s1')['queries_with_runs'],1)
        self.assertEqual(self.entry(r,'s2')['queries_with_runs'],0)
        self.assertEqual(self.pairs(),(1,1))
        q=self.report(system='s1')['systems'][0]['queries'][0]
        self.assertTrue(q['has_run']);self.assertEqual(q['precision_at_k'],0.0);self.assertEqual(q['recall_at_k'],0.0)
    def test_no_coverage_system_and_zero_denominators(self):
        self.init()
        self.apply(jsonl({'kind':'system','id':'s1'}))
        e=self.entry(self.report(),'s1')
        for key in ('precision_at_k','recall_at_k','average_precision_at_k','ndcg_at_k'):
            self.assertIsNone(e['macro'][key]['value'])
            self.assertEqual(e['macro'][key]['eligible_queries'],0)
        self.apply(jsonl({'kind':'query','id':'q1','judgments':[{'doc':'a','grade':0}]}))
        e=self.entry(self.report(),'s1')
        self.assertEqual(e['macro']['precision_at_k']['value'],0.0)
        self.assertEqual(e['macro']['precision_at_k']['eligible_queries'],1)
        self.assertIsNone(e['macro']['recall_at_k']['value'])
        self.assertEqual(e['macro']['recall_at_k']['eligible_queries'],0)
    def test_unrelated_pairs_not_rescored(self):
        self.init()
        self.apply(jsonl({'kind':'system','id':'s1'},{'kind':'system','id':'s2'},{'kind':'system','id':'s3'},
                         {'kind':'query','id':'q1','judgments':[{'doc':'a','grade':1}]},
                         {'kind':'query','id':'q2','judgments':[{'doc':'b','grade':2}]},
                         {'kind':'query','id':'q3','judgments':[{'doc':'c','grade':3}]},
                         {'kind':'run','system':'s1','query':'q1','docs':['a']},
                         {'kind':'run','system':'s2','query':'q1','docs':['x']},
                         {'kind':'run','system':'s1','query':'q2','docs':['b']},
                         {'kind':'run','system':'s3','query':'q3','docs':['c']}))
        with Counting() as counter:
            warehouse.apply(self.db,self.source(jsonl({'kind':'query','id':'q1','judgments':[{'doc':'a','grade':2},{'doc':'n','grade':1}]})))
        self.assertEqual(counter.calls,2)  # exactly q1's final explicit runs
        with Counting() as counter:
            warehouse.apply(self.db,self.source(jsonl({'kind':'run','system':'s1','query':'q2','docs':['b','y']})))
        self.assertEqual(counter.calls,1)
        with Counting() as counter:
            warehouse.apply(self.db,self.source(jsonl({'kind':'drop_run','system':'s1','query':'q2'})))
        self.assertEqual(counter.calls,0)
        # judgment replacement and a drop of that query's only remaining run in one batch:
        # nothing left to score for it, unrelated q3 still untouched
        with Counting() as counter:
            warehouse.apply(self.db,self.source(jsonl({'kind':'query','id':'q2','judgments':[{'doc':'b','grade':1}]},
                                                      {'kind':'drop_run','system':'s1','query':'q2'})))
        self.assertEqual(counter.calls,0)
        r=self.report(system='s3')  # untouched system keeps its exact scores
        q3=[q for q in r['systems'][0]['queries'] if q['query']=='q3'][0]
        self.assertEqual(q3['ndcg_at_k'],1.0)
    def test_report_never_rescores(self):
        self.seed()
        real=warehouse.evaluate
        def forbidden(*args):raise AssertionError('report must not rescore')
        warehouse.evaluate=forbidden
        try:
            warehouse.report(self.db,self.dir/'r1.json')
            warehouse.report(self.db,self.dir/'r2.json','s1')
        finally:
            warehouse.evaluate=real
    def test_matches_single_file_model(self):
        data=json.loads((ROOT/'fixtures/input.json').read_text())
        self.init(k=data['k'])
        records=[{'kind':'system','id':'solo'}]
        records+=[{'kind':'query','id':q['id'],'judgments':q['judgments']} for q in data['queries']]
        records+=[{'kind':'run','system':'solo','query':r['query'],'docs':r['docs']} for r in data['runs']]
        self.apply(jsonl(*records))
        expected=analyze(data)
        got=self.report(system='solo')
        entry=got['systems'][0]
        self.assertEqual(got['k'],expected['k'])
        self.assertEqual(entry['query_count'],expected['query_count'])
        self.assertEqual(entry['queries_with_runs'],expected['queries_with_runs'])
        self.close(entry['macro'],expected['macro'])
        self.close(entry['queries'],expected['queries'])
    def test_revision_and_determinism(self):
        self.seed()
        first=self.dir/'r1.json';second=self.dir/'r2.json'
        self.report(name='r1.json');self.report(name='r2.json')
        self.assertEqual(first.read_bytes(),second.read_bytes())

class Integration(Case):
    def seed(self):
        self.init()
        self.apply(jsonl({'kind':'system','id':'s1'},{'kind':'system','id':'s2'},{'kind':'system','id':'s3'},
                         {'kind':'query','id':'q0','judgments':[{'doc':'X','grade':0}]},
                         {'kind':'query','id':'q1','judgments':[{'doc':'Y','grade':2},{'doc':'W','grade':1}]},
                         {'kind':'run','system':'s1','query':'q0','docs':['A','B']},
                         {'kind':'run','system':'s2','query':'q1','docs':['Y','Z']}))
    def macro(self,system,key):
        return self.entry(self.report(),system)['macro'][key]
    def query_row(self,system,query):
        rows=self.report(system=system)['systems'][0]['queries']
        return [q for q in rows if q['query']==query][0]
    def test_relevance_crossing(self):
        self.seed()
        base_runs,base_scores=self.pairs()
        self.assertEqual((base_runs,base_scores),(2,2))
        # baseline: q0 has R=0 -> nulls for s1; only q1 eligible -> denominators 1
        row=self.query_row('s1','q0')
        self.assertIsNone(row['recall_at_k']);self.assertIsNone(row['ndcg_at_k'])
        self.assertEqual(self.macro('s2','recall_at_k')['eligible_queries'],1)
        self.assertEqual(self.macro('s3','recall_at_k')['eligible_queries'],1)
        s2_recall_before=self.macro('s2','recall_at_k')['value']
        s2_ndcg_before=self.macro('s2','ndcg_at_k')['value']
        # flip one unretrieved doc of q0 from grade 0 to 3
        self.apply(jsonl({'kind':'query','id':'q0','judgments':[{'doc':'X','grade':3}]}))
        row=self.query_row('s1','q0')
        self.assertEqual(row['recall_at_k'],0.0)
        self.assertEqual(row['average_precision_at_k'],0.0)
        self.assertEqual(row['ndcg_at_k'],0.0)
        self.assertEqual(row['relevant_total'],1)
        # s2's own scores are untouched but its three macro denominators move 1 -> 2
        for key in ('recall_at_k','average_precision_at_k','ndcg_at_k'):
            m=self.macro('s2',key)
            self.assertEqual(m['eligible_queries'],2)
        self.assertAlmostEqual(self.macro('s2','recall_at_k')['value'],s2_recall_before/2,delta=1e-12)
        self.assertAlmostEqual(self.macro('s2','ndcg_at_k')['value'],s2_ndcg_before/2,delta=1e-12)
        q1=self.query_row('s2','q1')
        self.assertAlmostEqual(q1['recall_at_k'],0.5)
        # s3 has no runs at all yet still sees the new eligibility
        for key in ('recall_at_k','average_precision_at_k','ndcg_at_k'):
            m=self.macro('s3',key)
            self.assertEqual(m['eligible_queries'],2)
            self.assertEqual(m['value'],0.0)
        self.assertEqual(self.macro('s3','precision_at_k')['eligible_queries'],2)
        # no placeholder pairs materialized
        self.assertEqual(self.pairs(),(base_runs,base_scores))
        # revert is exact
        self.apply(jsonl({'kind':'query','id':'q0','judgments':[{'doc':'X','grade':0}]}))
        row=self.query_row('s1','q0')
        self.assertIsNone(row['recall_at_k'])
        self.assertEqual(self.macro('s2','recall_at_k')['eligible_queries'],1)
        self.assertAlmostEqual(self.macro('s2','recall_at_k')['value'],s2_recall_before,delta=1e-12)
        self.assertEqual(self.macro('s3','recall_at_k')['eligible_queries'],1)
    def test_same_batch_judgment_and_run_scored_once(self):
        self.seed()
        batch=jsonl({'kind':'query','id':'q1','judgments':[{'doc':'Y','grade':3},{'doc':'Z','grade':2}]},
                    {'kind':'run','system':'s2','query':'q1','docs':['Z','Y']})
        with Counting() as counter:
            warehouse.apply(self.db,self.source(batch))
        self.assertEqual(counter.calls,1)  # q1 finally has one explicit run
        row=self.query_row('s2','q1')
        expected=evaluate({'Y':3,'Z':2},['Z','Y'],3)
        self.assertAlmostEqual(row['ndcg_at_k'],expected['ndcg_at_k'],delta=1e-12)
        self.assertAlmostEqual(row['recall_at_k'],expected['recall_at_k'],delta=1e-12)
        self.assertEqual(self.macro('s2','recall_at_k')['eligible_queries'],1)
    def test_interleaved_processes_and_atomicity(self):
        self.seed()
        first=self.report()
        self.assertEqual(first['revision'],1)
        # an uncommitted write in another connection is invisible to a reporting process
        con=warehouse._connect(self.db,'rw')
        try:
            con.execute('BEGIN IMMEDIATE')
            con.execute("INSERT INTO systems VALUES('ghost')")
            con.execute("UPDATE meta SET value=99 WHERE key='revision'")
            seen=self.report()
            self.assertEqual(seen['revision'],1)
            self.assertEqual([s['system'] for s in seen['systems']],['s1','s2','s3'])
        finally:
            con.execute('ROLLBACK');con.close()
        # a batch whose last line is bad changes nothing at all
        out=self.dir/'keep.json';self.report(name='keep.json');before=out.read_bytes()
        batch=jsonl({'kind':'query','id':'q0','judgments':[{'doc':'X','grade':3}]},
                    {'kind':'run','system':'s3','query':'q1','docs':['W']})+'{"kind":"run",'
        self.apply(batch,expect=2)
        self.report(name='keep.json')
        self.assertEqual(out.read_bytes(),before)
        # a good batch then becomes fully visible at once
        self.apply(jsonl({'kind':'query','id':'q0','judgments':[{'doc':'X','grade':3}]}))
        after=self.report()
        self.assertEqual(after['revision'],2)
        self.assertEqual(self.macro('s3','recall_at_k')['eligible_queries'],2)

class CommandSafety(Case):
    def test_unknown_system_preserves_report(self):
        self.init()
        self.apply(jsonl({'kind':'system','id':'s1'},{'kind':'query','id':'q1','judgments':[]}))
        out=self.dir/'o.json';out.write_text('prior')
        result=cli('warehouse-report','--db',str(self.db),'--output',str(out),'--system','nope')
        self.assertEqual(result.returncode,2)
        self.assertEqual(out.read_text(),'prior')
    def test_output_path_safety(self):
        self.init()
        self.apply(jsonl({'kind':'system','id':'s1'}))
        result=cli('warehouse-report','--db',str(self.db),'--output',str(self.db))
        self.assertEqual(result.returncode,2)
        link=self.dir/'link.json';link.symlink_to(self.dir/'victim.json')
        (self.dir/'victim.json').write_text('keep')
        result=cli('warehouse-report','--db',str(self.db),'--output',str(link))
        self.assertEqual(result.returncode,2)
        self.assertEqual((self.dir/'victim.json').read_text(),'keep')
    def test_apply_source_must_differ_from_db(self):
        self.init()
        result=cli('warehouse-apply','--db',str(self.db),'--source',str(self.db))
        self.assertEqual(result.returncode,2)
    def test_apply_missing_source(self):
        self.init()
        result=cli('warehouse-apply','--db',str(self.db),'--source',str(self.dir/'nope.jsonl'))
        self.assertEqual(result.returncode,2)
        self.assertEqual(self.report()['revision'],0)

class Capacity(Case):
    QUERIES=20000;SYSTEMS=20000;RUNS_PER_SYSTEM=10
    def judgments(self,i):
        grade=0 if i%10==0 else 1+i%3
        return [{'doc':'d%da'%i,'grade':grade},{'doc':'d%db'%i,'grade':0}]
    def run_docs(self,s,q):
        return ['d%da'%q,'u%d_%d'%(s,q),'d%dx'%q]
    def test_sparse_warehouse_capacity(self):
        k=5
        result=cli('warehouse-init','--db',str(self.db),'--k',str(k))
        self.assertEqual(result.returncode,0,result.stderr)
        # batch 1: declarations (systems and queries interleaved, order irrelevant)
        decl=self.source(''.join(
            [jsonl({'kind':'system','id':'s%d'%i}) for i in range(self.SYSTEMS)]+
            [jsonl({'kind':'query','id':'q%d'%i,'judgments':self.judgments(i)}) for i in range(self.QUERIES)]),'decl.jsonl')
        code,rss,err=cli_rss('warehouse-apply','--db',str(self.db),'--source',str(decl))
        self.assertEqual(code,0,err)
        print('\ncapacity: declarations apply peak RSS %.1f MiB'%(rss/1024))
        # batch 2: 200k explicit runs
        runs=self.source(''.join(jsonl({'kind':'run','system':'s%d'%s,'query':'q%d'%((s*self.RUNS_PER_SYSTEM+j)%self.QUERIES),
                                        'docs':self.run_docs(s,(s*self.RUNS_PER_SYSTEM+j)%self.QUERIES)})
                                 for s in range(self.SYSTEMS) for j in range(self.RUNS_PER_SYSTEM)),'runs.jsonl')
        code,rss,err=cli_rss('warehouse-apply','--db',str(self.db),'--source',str(runs))
        self.assertEqual(code,0,err)
        print('capacity: 200k-run apply peak RSS %.1f MiB'%(rss/1024))
        self.assertLessEqual(rss,RSS_LIMIT,'apply peak RSS')
        # full report
        code,rss,err=cli_rss('warehouse-report','--db',str(self.db),'--output',str(self.dir/'full.json'))
        self.assertEqual(code,0,err)
        print('capacity: full report peak RSS %.1f MiB'%(rss/1024))
        self.assertLessEqual(rss,RSS_LIMIT,'report peak RSS')
        # per-system report
        code,rss,err=cli_rss('warehouse-report','--db',str(self.db),'--output',str(self.dir/'s0.json'),'--system','s0')
        self.assertEqual(code,0,err)
        print('capacity: per-system report peak RSS %.1f MiB'%(rss/1024))
        self.assertLessEqual(rss,RSS_LIMIT,'per-system report peak RSS')
        full=json.loads((self.dir/'full.json').read_text())
        self.assertEqual(full['revision'],2)
        self.assertEqual(full['query_count'],self.QUERIES)
        self.assertEqual(len(full['systems']),self.SYSTEMS)
        relevant=sum(1 for i in range(self.QUERIES) if i%10!=0)
        entry=[s for s in full['systems'] if s['system']=='s0'][0]
        self.assertEqual(entry['queries_with_runs'],self.RUNS_PER_SYSTEM)
        self.assertEqual(entry['macro']['recall_at_k']['eligible_queries'],relevant)
        # spot-check s0's per-query rows against direct evaluation
        detail=json.loads((self.dir/'s0.json').read_text())['systems'][0]['queries']
        self.assertEqual(len(detail),self.QUERIES)
        expected={}
        for j in range(self.RUNS_PER_SYSTEM):
            q=(0*self.RUNS_PER_SYSTEM+j)%self.QUERIES
            expected['q%d'%q]=evaluate({j['doc']:j['grade'] for j in self.judgments(q)},self.run_docs(0,q),k)
        for row in detail:
            if row['query'] in expected:
                exp=expected[row['query']]
                self.assertTrue(row['has_run'])
                for key,exp_key in (('precision_at_k','precision_at_k'),('recall_at_k','recall_at_k'),
                                    ('average_precision_at_k','average_precision_at_k'),('ndcg_at_k','ndcg_at_k')):
                    if exp[exp_key] is None:self.assertIsNone(row[key])
                    else:self.assertAlmostEqual(row[key],exp[exp_key],delta=1e-12)
            else:
                self.assertFalse(row['has_run'])
        # single-query replacement on the full warehouse scores exactly its own runs
        qid='q5'
        with Counting() as counter:
            warehouse.apply(self.db,self.source(jsonl({'kind':'query','id':qid,'judgments':[{'doc':'d5a','grade':3}]})))
        runs_on_q5=sum(1 for s in range(self.SYSTEMS) if any((s*self.RUNS_PER_SYSTEM+j)%self.QUERIES==5 for j in range(self.RUNS_PER_SYSTEM)))
        self.assertEqual(counter.calls,runs_on_q5)
        self.assertEqual(self.pairs(),(self.SYSTEMS*self.RUNS_PER_SYSTEM,)*2)
        detail=cli('warehouse-report','--db',str(self.db),'--output',str(self.dir/'after.json'),'--system','s0')
        self.assertEqual(detail.returncode,0,detail.stderr)
        row=[q for q in json.loads((self.dir/'after.json').read_text())['systems'][0]['queries'] if q['query']==qid][0]
        exp=evaluate({'d5a':3},self.run_docs(0,5),k)
        self.assertAlmostEqual(row['ndcg_at_k'],exp['ndcg_at_k'],delta=1e-12)

if __name__=='__main__':unittest.main()
