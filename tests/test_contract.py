
import json, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from retrieval_eval import contract

ROOT=Path(__file__).resolve().parents[1]
CLI=[sys.executable,'-m','retrieval_eval.cli']
FIXTURES=ROOT/'fixtures'/'contract'
RSS_LIMIT=192*1024  # KiB on Linux
BLOCKING={'removed_required_read','type_not_accepted','null_not_accepted'}

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
    # Linux: poll the child's own VmHWM (ru_maxrss is inherited across fork/exec).
    proc=subprocess.Popen(CLI+list(args),stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    peak=0
    while proc.poll() is None:
        peak=max(peak,peak_rss(proc.pid))
        time.sleep(0.005)
    peak=max(peak,peak_rss(proc.pid))
    stderr=proc.stderr.read().decode();proc.stderr.close()
    return proc.returncode,peak,stderr

def field(path,typ='integer',required=True,nullable=False):
    return {'path':path,'type':typ,'required':required,'nullable':nullable}

def read_decl(path,accepted=('integer',),required=True,accepts_null=False):
    return {'path':path,'accepted_types':list(accepted),'required':required,'accepts_null':accepts_null}

def contract_doc(dataset,ver,fields):
    return {'dataset':dataset,'version':ver,'fields':fields}

def structures():
    baseline=contract_doc('d','v1',[field('a')])
    candidate=contract_doc('d','v2',[field('a')])
    consumers={'dataset':'d','consumers':[{'id':'c','reads':[read_decl('a')]}]}
    return baseline,candidate,consumers

class Case(unittest.TestCase):
    def setUp(self):
        self.td=tempfile.TemporaryDirectory();self.dir=Path(self.td.name)
    def tearDown(self):
        self.td.cleanup()
    def write(self,name,data,compact=False):
        path=self.dir/name
        if isinstance(data,str):path.write_text(data,encoding='utf-8')
        elif compact:path.write_text(json.dumps(data,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
        else:path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        return path
    def impact(self,baseline,candidate,consumers,output='report.json',expect=0):
        out=self.dir/output
        result=cli('contract-impact','--baseline',str(baseline),'--candidate',str(candidate),
                   '--consumers',str(consumers),'--output',str(out))
        self.assertEqual(result.returncode,expect,result.stderr)
        if expect==0:return json.loads(out.read_text(encoding='utf-8'))
        return result
    def case_files(self,baseline,candidate,consumers):
        return (self.write('baseline.json',baseline),self.write('candidate.json',candidate),
                self.write('consumers.json',consumers))
    def consumer(self,report,cid):
        for c in report['consumers']:
            if c['id']==cid:return c
        self.fail('consumer not in report: '+cid)
    def reasons(self,entry):
        return [i['reason'] for i in entry['issues']]

class Impact(Case):
    def test_required_removal_blocks_and_optional_removal_warns(self):
        baseline=contract_doc('d','v1',[field('gone.req'),field('gone.opt',required=False),field('stay')])
        candidate=contract_doc('d','v2',[field('stay')])
        consumers={'dataset':'d','consumers':[
            {'id':'c1','reads':[read_decl('gone.req')]},
            {'id':'c2','reads':[read_decl('gone.opt',required=False)]}]}
        report=self.impact(*self.case_files(baseline,candidate,consumers))
        self.assertEqual(self.consumer(report,'c1')['status'],'blocked')
        self.assertEqual(self.reasons(self.consumer(report,'c1')),['removed_required_read'])
        self.assertEqual(self.consumer(report,'c2')['status'],'warning')
        self.assertEqual(self.reasons(self.consumer(report,'c2')),['removed_optional_read'])
        kinds={d['path']:d['kind'] for d in report['field_diffs']}
        self.assertEqual(kinds,{'gone.req':'removed','gone.opt':'removed'})
        self.assertEqual(report['summary']['fields_removed'],2)

    def test_type_change_acceptance_is_per_consumer(self):
        baseline=contract_doc('d','v1',[field('a',typ='integer')])
        candidate=contract_doc('d','v2',[field('a',typ='number')])
        consumers={'dataset':'d','consumers':[
            {'id':'narrow','reads':[read_decl('a',('integer',))]},
            {'id':'wide','reads':[read_decl('a',('integer','number'))]}]}
        report=self.impact(*self.case_files(baseline,candidate,consumers))
        narrow=self.consumer(report,'narrow')
        self.assertEqual(narrow['status'],'blocked')
        self.assertEqual(narrow['issues'],[{'candidate_type':'number','path':'a','reason':'type_not_accepted'}])
        self.assertEqual(self.consumer(report,'wide')['status'],'compatible')

    def test_nullable_transition_differentiates_consumers(self):
        # same field, same candidate diff: accepts_null decides the outcome
        baseline=contract_doc('d','v1',[field('a',typ='string',nullable=False)])
        candidate=contract_doc('d','v2',[field('a',typ='string',nullable=True)])
        consumers={'dataset':'d','consumers':[
            {'id':'tolerant','reads':[read_decl('a',('string',),accepts_null=True)]},
            {'id':'strict','reads':[read_decl('a',('string',),accepts_null=False)]}]}
        report=self.impact(*self.case_files(baseline,candidate,consumers))
        self.assertEqual(self.consumer(report,'tolerant')['status'],'compatible')
        strict=self.consumer(report,'strict')
        self.assertEqual(strict['status'],'blocked')
        self.assertEqual(self.reasons(strict),['null_not_accepted'])

    def test_nullable_narrowing_and_added_nullable_are_compatible(self):
        baseline=contract_doc('d','v1',[field('a',typ='string',nullable=True)])
        candidate=contract_doc('d','v2',[field('a',typ='string',nullable=False),
                                         field('new',typ='string',nullable=True)])
        consumers={'dataset':'d','consumers':[{'id':'c','reads':[
            read_decl('a',('string',),accepts_null=False),
            read_decl('new',('string',),accepts_null=False)]}]}
        report=self.impact(*self.case_files(baseline,candidate,consumers))
        self.assertEqual(self.consumer(report,'c')['status'],'compatible')
        self.assertEqual(self.consumer(report,'c')['issues'],[])

    def test_added_field_without_readers_keeps_full_diff(self):
        baseline=contract_doc('d','v1',[field('a')])
        candidate=contract_doc('d','v2',[field('a'),field('brand.new',typ='object',required=False,nullable=True)])
        consumers={'dataset':'d','consumers':[{'id':'c','reads':[read_decl('a')]}]}
        report=self.impact(*self.case_files(baseline,candidate,consumers))
        added=[d for d in report['field_diffs'] if d['kind']=='added']
        self.assertEqual(len(added),1)
        self.assertEqual(added[0]['candidate'],{'type':'object','required':False,'nullable':True})
        self.assertEqual(self.consumer(report,'c')['status'],'compatible')
        self.assertEqual(report['summary']['fields_added'],1)

    def test_added_field_read_by_consumer_is_type_checked(self):
        baseline=contract_doc('d','v1',[field('a')])
        candidate=contract_doc('d','v2',[field('a'),field('new',typ='array')])
        consumers={'dataset':'d','consumers':[
            {'id':'picky','reads':[read_decl('new',('string',))]},
            {'id':'open','reads':[read_decl('new',('array','object'))]}]}
        report=self.impact(*self.case_files(baseline,candidate,consumers))
        self.assertEqual(self.reasons(self.consumer(report,'picky')),['type_not_accepted'])
        self.assertEqual(self.consumer(report,'picky')['status'],'blocked')
        self.assertEqual(self.consumer(report,'open')['status'],'compatible')

    def test_multi_attribute_change_is_one_diff_entry(self):
        baseline=contract_doc('d','v1',[field('a',typ='integer',nullable=False)])
        candidate=contract_doc('d','v2',[field('a',typ='string',nullable=True)])
        consumers={'dataset':'d','consumers':[]}
        report=self.impact(*self.case_files(baseline,candidate,consumers))
        entries=[d for d in report['field_diffs'] if d['path']=='a']
        self.assertEqual(len(entries),1)
        self.assertEqual(entries[0]['kind'],'changed')
        self.assertEqual(entries[0]['changes'],{'type':{'from':'integer','to':'string'},
                                                'nullable':{'from':False,'to':True}})

    def test_unread_changes_produce_no_issues(self):
        baseline=contract_doc('d','v1',[field('watched'),field('quiet',typ='integer',nullable=False)])
        candidate=contract_doc('d','v2',[field('watched'),field('quiet',typ='string',nullable=True)])
        consumers={'dataset':'d','consumers':[{'id':'c','reads':[read_decl('watched')]}]}
        report=self.impact(*self.case_files(baseline,candidate,consumers))
        self.assertEqual(len(report['field_diffs']),1)  # diff kept although nobody reads it
        self.assertEqual(self.consumer(report,'c')['status'],'compatible')
        self.assertEqual(self.consumer(report,'c')['issues'],[])

    def test_reads_of_paths_in_neither_version_are_ignored(self):
        baseline=contract_doc('d','v1',[field('a')])
        candidate=contract_doc('d','v2',[field('a')])
        consumers={'dataset':'d','consumers':[{'id':'c','reads':[read_decl('ghost')]}]}
        report=self.impact(*self.case_files(baseline,candidate,consumers))
        self.assertEqual(self.consumer(report,'c')['status'],'compatible')
        self.assertEqual(report['field_diffs'],[])

    def test_linkage_two_consumers_same_path(self):
        # two consumers read the same path; first the field only turns nullable
        baseline=contract_doc('d','v1',[field('p',typ='string',nullable=False)])
        step1=contract_doc('d','v2',[field('p',typ='string',nullable=True)])
        consumers={'dataset':'d','consumers':[
            {'id':'accepts','reads':[read_decl('p',('string',),accepts_null=True)]},
            {'id':'rejects','reads':[read_decl('p',('string',),accepts_null=False)]}]}
        files=self.case_files(baseline,step1,consumers)
        report=self.impact(*files)
        self.assertEqual(self.consumer(report,'accepts')['status'],'compatible')
        self.assertEqual(self.consumer(report,'rejects')['status'],'blocked')
        self.assertEqual(self.reasons(self.consumer(report,'rejects')),['null_not_accepted'])
        # then the candidate type also changes: the null-accepting consumer blocks too,
        # and both reason kinds survive side by side
        step2=contract_doc('d','v3',[field('p',typ='integer',nullable=True)])
        report=self.impact(files[0],self.write('candidate2.json',step2),files[2],output='report2.json')
        diff=[d for d in report['field_diffs'] if d['path']=='p'][0]
        self.assertEqual(diff['changes'],{'type':{'from':'string','to':'integer'},
                                          'nullable':{'from':False,'to':True}})
        accepts=self.consumer(report,'accepts')
        self.assertEqual(accepts['status'],'blocked')
        self.assertEqual(self.reasons(accepts),['type_not_accepted'])
        rejects=self.consumer(report,'rejects')
        self.assertEqual(rejects['status'],'blocked')
        self.assertEqual(self.reasons(rejects),['null_not_accepted','type_not_accepted'])
        self.assertEqual(rejects['issues'][0],{'path':'p','reason':'null_not_accepted'})
        self.assertEqual(rejects['issues'][1],{'candidate_type':'integer','path':'p','reason':'type_not_accepted'})

    def test_report_ordering_and_summary(self):
        baseline=contract_doc('d','v1',[field('m.b',typ='integer'),field('a.a',typ='string'),
                                        field('z.z',required=False),field('k.k')])
        candidate=contract_doc('d','v2',[field('m.b',typ='number'),field('a.a',typ='string'),
                                         field('k.k'),field('n.n',typ='boolean')])
        consumers={'dataset':'d','consumers':[
            {'id':'zeta','reads':[read_decl('m.b',('integer',))]},
            {'id':'alpha','reads':[read_decl('z.z',required=False)]},
            {'id':'mid','reads':[read_decl('k.k')]}]}
        report=self.impact(*self.case_files(baseline,candidate,consumers))
        self.assertEqual([d['path'] for d in report['field_diffs']],['m.b','n.n','z.z'])
        self.assertEqual([c['id'] for c in report['consumers']],['alpha','mid','zeta'])
        self.assertEqual(report['summary'],{'blocked':1,'compatible':1,'consumers_total':3,
                                            'fields_added':1,'fields_changed':1,'fields_removed':1,
                                            'warning':1})

    def test_empty_consumers_and_identical_versions(self):
        same=contract_doc('d','v1',[field('a')])
        report=self.impact(*self.case_files(same,dict(same,version='v2'),{'dataset':'d','consumers':[]}))
        self.assertEqual(report['field_diffs'],[])
        self.assertEqual(report['consumers'],[])
        self.assertEqual(report['summary']['consumers_total'],0)

class Determinism(Case):
    def structures(self):
        baseline=contract_doc('d','v1',[
            field('p1',typ='integer',nullable=False),field('p2',typ='string'),
            field('p3',typ='boolean',required=False,nullable=True),field('p4',typ='array')])
        candidate=contract_doc('d','v2',[
            field('p1',typ='number',nullable=True),field('p2',typ='string'),
            field('p4',typ='array'),field('p5',typ='object',required=False,nullable=True)])
        consumers={'dataset':'d','consumers':[
            {'id':'c2','reads':[read_decl('p1',('integer','number'),accepts_null=False),
                                read_decl('p3',('boolean',),required=False)]},
            {'id':'c1','reads':[read_decl('p1',('integer',),accepts_null=True),
                                read_decl('p5',('object','string'),accepts_null=True)]}]}
        return baseline,candidate,consumers
    def test_shuffled_input_byte_identical(self):
        baseline,candidate,consumers=self.structures()
        ordered=self.case_files(baseline,candidate,consumers)
        out1=self.dir/'r1.json'
        r=cli('contract-impact','--baseline',str(ordered[0]),'--candidate',str(ordered[1]),
              '--consumers',str(ordered[2]),'--output',str(out1))
        self.assertEqual(r.returncode,0,r.stderr)
        def shuffled(data,key):
            data=json.loads(json.dumps(data))
            data[key].reverse()
            return data
        b2=shuffled(baseline,'fields');c2=shuffled(candidate,'fields');s2=shuffled(consumers,'consumers')
        for consumer in s2['consumers']:
            consumer['reads'].reverse()
            for decl in consumer['reads']:decl['accepted_types'].reverse()
        # different key order in the JSON text as well
        b2={'fields':b2['fields'],'version':b2['version'],'dataset':b2['dataset']}
        files2=(self.write('b2.json',b2,compact=True),self.write('c2.json',c2,compact=True),
                self.write('s2.json',s2,compact=True))
        out2=self.dir/'r2.json'
        r=cli('contract-impact','--baseline',str(files2[0]),'--candidate',str(files2[1]),
              '--consumers',str(files2[2]),'--output',str(out2))
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(out1.read_bytes(),out2.read_bytes())
    def test_rerun_byte_identical(self):
        files=self.case_files(*self.structures())
        first=self.dir/'a.json';second=self.dir/'b.json'
        for out in (first,second):
            r=cli('contract-impact','--baseline',str(files[0]),'--candidate',str(files[1]),
                  '--consumers',str(files[2]),'--output',str(out))
            self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(first.read_bytes(),second.read_bytes())
    def test_fixture_report_matches_committed_expectation(self):
        out=self.dir/'report.json'
        r=cli('contract-impact','--baseline',str(FIXTURES/'baseline.json'),
              '--candidate',str(FIXTURES/'candidate.json'),
              '--consumers',str(FIXTURES/'consumers.json'),'--output',str(out))
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(out.read_bytes(),(FIXTURES/'expected-report.json').read_bytes())

class Counting:
    def __init__(self):
        self.calls=0;self.real=contract.judge
    def __call__(self,entry,candidate,reader):
        self.calls+=1;return self.real(entry,candidate,reader)
    def __enter__(self):
        contract.judge=self;return self
    def __exit__(self,*args):
        contract.judge=self.real

class Scaling(Case):
    def test_no_consumer_field_cartesian_product(self):
        baseline={'f%d'%i:('integer',True,False) for i in range(100)}
        candidate=dict(baseline)
        for i in range(10):candidate['f%d'%i]=('number',True,False)
        consumers={'c%d'%c:{**{'f%d'%i:(('integer','number'),True,False) for i in range(5)},
                            **{'f%d'%i:(('integer',),True,False) for i in range(50,55)}}
                   for c in range(5)}
        with Counting() as counter:
            report=contract.review('d','v1','v2',baseline,candidate,consumers)
        # exactly one judgement per (changed path, its reader): each consumer reads
        # 5 changed paths (f0..f4) and 5 untouched ones (f50..f54)
        self.assertEqual(counter.calls,25)
        self.assertLess(counter.calls,5*100)  # never consumers x all fields
        self.assertEqual(report['summary']['fields_changed'],10)
    def test_scale_capacity(self):
        fields_n,consumers_n,reads_n=20000,2000,40
        baseline_fields,candidate_fields=[],[]
        for i in range(fields_n):
            base={'path':'f%d'%i,'type':'integer','required':i%2==0,'nullable':i%3==0}
            if i%10==1:
                baseline_fields.append(base);continue  # removed in candidate
            cand=dict(base)
            if i%5==0:cand['type']='number'
            if i%7==0 and not base['nullable']:cand['nullable']=True
            baseline_fields.append(base);candidate_fields.append(cand)
        for i in range(2000):
            candidate_fields.append({'path':'new%d'%i,'type':'string','required':False,'nullable':True})
        consumers=[]
        for c in range(consumers_n):
            reads=[]
            for j in range(reads_n):
                i=(c*reads_n+j)%fields_n
                accepted=(['integer','number'],['integer'],
                          ['string','integer','number','boolean','object','array'])[(c+j)%3]
                reads.append({'path':'f%d'%i,'accepted_types':accepted,
                              'required':j%2==0,'accepts_null':(c+j)%2==0})
            consumers.append({'id':'c%d'%c,'reads':reads})
        baseline=contract_doc('events','v1',baseline_fields)
        candidate=contract_doc('events','v2',candidate_fields)
        consumer_doc={'dataset':'events','consumers':consumers}
        files=(self.write('baseline.json',baseline,compact=True),
               self.write('candidate.json',candidate,compact=True),
               self.write('consumers.json',consumer_doc,compact=True))
        out=self.dir/'report.json'
        code,rss,stderr=cli_rss('contract-impact','--baseline',str(files[0]),'--candidate',str(files[1]),
                                '--consumers',str(files[2]),'--output',str(out))
        self.assertEqual(code,0,stderr)
        print('\ncontract capacity: peak RSS %.1f MiB'%(rss/1024))
        self.assertLessEqual(rss,RSS_LIMIT,'contract-impact peak RSS')
        report=json.loads(out.read_text(encoding='utf-8'))
        self.assertEqual(report,self.reference(baseline_fields,candidate_fields,consumers))
        # rerun is byte-identical
        out2=self.dir/'report2.json'
        r=cli('contract-impact','--baseline',str(files[0]),'--candidate',str(files[1]),
              '--consumers',str(files[2]),'--output',str(out2))
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(out.read_bytes(),out2.read_bytes())
    def reference(self,baseline_fields,candidate_fields,consumers):
        bmap={f['path']:f for f in baseline_fields}
        cmap={f['path']:f for f in candidate_fields}
        diffs=[]
        for path in sorted(set(bmap)|set(cmap)):
            b,c=bmap.get(path),cmap.get(path)
            if b is None:
                diffs.append({'path':path,'kind':'added','candidate':
                              {'type':c['type'],'required':c['required'],'nullable':c['nullable']}})
            elif c is None:
                diffs.append({'path':path,'kind':'removed','baseline':
                              {'type':b['type'],'required':b['required'],'nullable':b['nullable']}})
            else:
                changes={}
                if b['type']!=c['type']:changes['type']={'from':b['type'],'to':c['type']}
                if b['nullable']!=c['nullable']:changes['nullable']={'from':b['nullable'],'to':c['nullable']}
                if changes:diffs.append({'path':path,'kind':'changed','changes':changes})
        by_path={d['path']:d for d in diffs}
        reports=[];counts={'blocked':0,'warning':0,'compatible':0}
        for cons in sorted(consumers,key=lambda x:x['id']):
            issues=[]
            for decl in cons['reads']:
                diff=by_path.get(decl['path'])
                if diff is None:continue
                if diff['kind']=='removed':
                    issues.append({'path':decl['path'],'reason':
                                   'removed_required_read' if decl['required'] else 'removed_optional_read'})
                    continue
                cand=cmap[decl['path']]
                if cand['type'] not in decl['accepted_types']:
                    issues.append({'candidate_type':cand['type'],'path':decl['path'],
                                   'reason':'type_not_accepted'})
                if diff['kind']=='changed':
                    nullable=diff['changes'].get('nullable')
                    if nullable and not nullable['from'] and nullable['to'] and not decl['accepts_null']:
                        issues.append({'path':decl['path'],'reason':'null_not_accepted'})
            issues.sort(key=lambda i:(i['path'],i['reason']))
            if any(i['reason'] in BLOCKING for i in issues):status='blocked'
            elif issues:status='warning'
            else:status='compatible'
            counts[status]+=1
            reports.append({'id':cons['id'],'status':status,'issues':issues})
        kinds={'added':0,'removed':0,'changed':0}
        for d in diffs:kinds[d['kind']]+=1
        return {'schema_version':1,'dataset':'events','baseline_version':'v1','candidate_version':'v2',
                'field_diffs':diffs,'consumers':reports,
                'summary':{'consumers_total':len(reports),'blocked':counts['blocked'],
                           'warning':counts['warning'],'compatible':counts['compatible'],
                           'fields_added':kinds['added'],'fields_removed':kinds['removed'],
                           'fields_changed':kinds['changed']}}

class Validation(Case):
    def bad(self,baseline=None,candidate=None,consumers=None):
        b,c,s=structures()
        files=self.case_files(baseline if baseline is not None else b,
                              candidate if candidate is not None else c,
                              consumers if consumers is not None else s)
        out=self.dir/'report.json';out.write_text('prior',encoding='utf-8')
        result=cli('contract-impact','--baseline',str(files[0]),'--candidate',str(files[1]),
                   '--consumers',str(files[2]),'--output',str(out))
        self.assertEqual(result.returncode,2,result.stderr)
        self.assertEqual(out.read_text(encoding='utf-8'),'prior')  # old report preserved
    def test_unknown_and_missing_keys(self):
        for mutate in (
            lambda b,c,s:b.update(extra=1),
            lambda b,c,s:b['fields'][0].update(extra=1),
            lambda b,c,s:b['fields'][0].pop('nullable'),
            lambda b,c,s:s.update(extra=1),
            lambda b,c,s:s['consumers'][0].update(extra=1),
            lambda b,c,s:s['consumers'][0].pop('reads'),
            lambda b,c,s:s['consumers'][0]['reads'][0].update(extra=1),
            lambda b,c,s:c.pop('version')):
            b,c,s=structures();mutate(b,c,s);self.bad(b,c,s)
    def test_identifier_rules(self):
        for bad_id in ('',' x','x ','x'*129,'a\nb','a\tb',5,None,['x']):
            b,c,s=structures();b['dataset']=bad_id;self.bad(b)
            b,c,s=structures();c['version']=bad_id;self.bad(candidate=c)
            b,c,s=structures();b['fields'][0]['path']=bad_id;self.bad(b)
            b,c,s=structures();s['consumers'][0]['id']=bad_id;self.bad(consumers=s)
            b,c,s=structures();s['consumers'][0]['reads'][0]['path']=bad_id;self.bad(consumers=s)
    def test_wrong_types(self):
        b,c,s=structures();b['fields'][0]['required']='true';self.bad(b)
        b,c,s=structures();b['fields'][0]['required']=1;self.bad(b)
        b,c,s=structures();c['fields'][0]['nullable']=None;self.bad(candidate=c)
        b,c,s=structures();s['consumers'][0]['reads'][0]['accepts_null']=0;self.bad(consumers=s)
        b,c,s=structures();s['consumers'][0]['reads'][0]['accepted_types']='integer';self.bad(consumers=s)
        b,c,s=structures();b['fields']={'a':{}};self.bad(b)
        b,c,s=structures();s['consumers'][0]['reads']={};self.bad(consumers=s)
        b,c,s=structures();s['consumers']={};self.bad(consumers=s)
        b,c,s=structures();b['fields'][0]['type']=5;self.bad(b)
        self.bad(baseline=['not','an','object'])
    def test_type_set_rules(self):
        b,c,s=structures();b['fields'][0]['type']='blob';self.bad(b)
        b,c,s=structures();s['consumers'][0]['reads'][0]['accepted_types']=[];self.bad(consumers=s)
        b,c,s=structures();s['consumers'][0]['reads'][0]['accepted_types']=['integer','integer'];self.bad(consumers=s)
        b,c,s=structures();s['consumers'][0]['reads'][0]['accepted_types']=['blob'];self.bad(consumers=s)
        b,c,s=structures()
        s['consumers'][0]['reads'][0]['accepted_types']=['string','integer','number','boolean','object','array']
        b['fields'][0]['type']='boolean';c['fields'][0]['type']='boolean'
        report=self.impact(*self.case_files(b,c,s))
        self.assertEqual(self.consumer(report,'c')['status'],'compatible')
    def test_duplicates_rejected(self):
        b,c,s=structures();b['fields'].append(field('a'));self.bad(b)
        b,c,s=structures();c['fields'].append(field('a',typ='string'));self.bad(candidate=c)
        b,c,s=structures();s['consumers'].append({'id':'c','reads':[]});self.bad(consumers=s)
        b,c,s=structures();s['consumers'][0]['reads'].append(read_decl('a',('string',)));self.bad(consumers=s)
        # the same path read by two different consumers is fine
        b,c,s=structures()
        s['consumers'].append({'id':'other','reads':[read_decl('a')]})
        report=self.impact(*self.case_files(b,c,s))
        self.assertEqual(report['summary']['consumers_total'],2)
    def test_dataset_mismatch_rejected(self):
        b,c,s=structures();c['dataset']='other';self.bad(candidate=c)
        b,c,s=structures();s['dataset']='other';self.bad(consumers=s)
    def test_json_strictness(self):
        b,c,s=self.case_files(*structures())
        out=self.dir/'report.json';out.write_text('prior',encoding='utf-8')
        bodies=[b'{"dataset":"d","dataset":"d","version":"v1","fields":[]}',
                b'{"dataset":"d","version":"v1","fields":[],"x":NaN}',
                b'\xff\xfe',
                b'{"dataset":"d","version":"v1","fields":[]} trailing']
        for body in bodies:
            (self.dir/'baseline.json').write_bytes(body)
            result=cli('contract-impact','--baseline',str(b),'--candidate',str(c),
                       '--consumers',str(s),'--output',str(out))
            self.assertEqual(result.returncode,2,body[:40])
            self.assertEqual(out.read_text(encoding='utf-8'),'prior')
            self.write('baseline.json',structures()[0])
    def test_input_size_limit(self):
        files=self.case_files(*structures())
        original=contract.JSON_LIMIT
        contract.JSON_LIMIT=100
        try:
            with self.assertRaises(ValueError):
                contract.impact(files[0],files[1],files[2],self.dir/'report.json')
        finally:
            contract.JSON_LIMIT=original
        self.assertFalse((self.dir/'report.json').exists())

class CommandSafety(Case):
    def test_input_output_must_differ(self):
        files=[str(p) for p in self.case_files(*structures())]
        for slot in range(3):
            args=['contract-impact','--baseline',files[0],'--candidate',files[1],
                  '--consumers',files[2],'--output',files[slot]]
            before=Path(files[slot]).read_bytes()
            result=cli(*args)
            self.assertEqual(result.returncode,2)
            self.assertEqual(Path(files[slot]).read_bytes(),before)
    def test_output_symlink_rejected(self):
        files=self.case_files(*structures())
        victim=self.dir/'victim.json';victim.write_text('keep',encoding='utf-8')
        link=self.dir/'link.json';link.symlink_to(victim)
        result=cli('contract-impact','--baseline',str(files[0]),'--candidate',str(files[1]),
                   '--consumers',str(files[2]),'--output',str(link))
        self.assertEqual(result.returncode,2)
        self.assertEqual(victim.read_text(encoding='utf-8'),'keep')
    def test_missing_inputs(self):
        files=self.case_files(*structures())
        out=self.dir/'report.json'
        for slot,flag in ((0,'--baseline'),(1,'--candidate'),(2,'--consumers')):
            args=['contract-impact','--baseline',str(files[0]),'--candidate',str(files[1]),
                  '--consumers',str(files[2]),'--output',str(out)]
            args[args.index(flag)+1]=str(self.dir/'missing.json')
            result=cli(*args)
            self.assertEqual(result.returncode,2)
            self.assertFalse(out.exists())
    def test_output_is_directory(self):
        files=self.case_files(*structures())
        result=cli('contract-impact','--baseline',str(files[0]),'--candidate',str(files[1]),
                   '--consumers',str(files[2]),'--output',str(self.dir))
        self.assertEqual(result.returncode,2)

if __name__=='__main__':unittest.main()
