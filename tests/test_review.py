
import json, os, sqlite3, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from retrieval_eval import review, warehouse

ROOT=Path(__file__).resolve().parents[1]
CLI=[sys.executable,'-m','retrieval_eval.cli']
RSS_LIMIT=192*1024

def jsonl(*records):
    return ''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in records)

def cli(*args):
    return subprocess.run(CLI+list(args),capture_output=True,text=True)

def peak_rss(pid):
    try:
        with open('/proc/%d/status'%pid) as status:
            for line in status:
                if line.startswith('VmHWM'):
                    return int(line.split()[1])
    except OSError:
        return 0
    return 0

def cli_rss(*args):
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
    def groups(self,mapping,name='groups.json'):
        path=self.dir/name
        path.write_text(json.dumps({'groups':[{'id':g,'queries':q} for g,q in mapping.items()]},
                                   ensure_ascii=False),encoding='utf-8')
        return path
    def review(self,groups,anchor='a',top=3,budget=10,floor=1,name='pack.json',expect=0):
        out=self.dir/name
        result=cli('review-pack','--db',str(self.db),'--groups',str(groups),'--anchor',anchor,
                   '--top',str(top),'--budget',str(budget),'--cell-floor',str(floor),
                   '--output',str(out))
        self.assertEqual(result.returncode,expect,result.stderr)
        if expect!=0:return result
        return json.loads(out.read_text())
    def units(self,report):
        return {(u['group'],u['system']):u for u in report['units']}
    def samples(self,report):
        return {s['query']:s for s in report['samples']}
    def seed(self):
        self.init()
        self.apply(jsonl({'kind':'system','id':'a'},{'kind':'system','id':'b'},
                         {'kind':'system','id':'c'},
                         {'kind':'query','id':'q1','judgments':[{'doc':'H','grade':3},
                                                                {'doc':'G','grade':1},
                                                                {'doc':'C','grade':0}]},
                         {'kind':'query','id':'q2','judgments':[{'doc':'Z','grade':1}]},
                         {'kind':'query','id':'q3','judgments':[{'doc':'N','grade':0}]},
                         {'kind':'run','system':'a','query':'q1','docs':['H','G','C']},
                         {'kind':'run','system':'b','query':'q1','docs':['G','H','X']},
                         {'kind':'run','system':'c','query':'q1','docs':['H','G','C']},
                         {'kind':'run','system':'a','query':'q2','docs':['Z']},
                         {'kind':'run','system':'b','query':'q2','docs':[]},
                         {'kind':'run','system':'a','query':'q3','docs':['N']},
                         {'kind':'run','system':'b','query':'q3','docs':['N','M']}))


class Extraction(Case):
    def test_tri_state_and_short_run(self):
        self.seed()
        report=self.review(self.groups({'g1':['q1','q2','q3']}))
        self.assertEqual(report['anchor'],'a')
        self.assertEqual(report['systems']['candidates'],['b','c'])
        self.assertEqual(report['top'],3)
        self.assertEqual(report['k'],3)
        self.assertEqual(report['revision'],1)
        self.assertEqual(sorted(self.samples(report)),['q1','q2','q3'])
        q1=self.samples(report)['q1']['systems'][0]
        self.assertEqual(q1['system'],'b')
        self.assertEqual(q1['anchor_docs'],['H','G','C'])
        self.assertEqual(q1['anchor_states'],['relevant','relevant','irrelevant'])
        self.assertEqual(q1['candidate_docs'],['G','H','X'])
        self.assertEqual(q1['candidate_states'],['relevant','relevant','unjudged'])
        self.assertEqual(q1['diff_positions'],[2])
        q2=self.samples(report)['q2']['systems'][0]
        self.assertEqual(q2['anchor_docs'],['Z']);self.assertEqual(q2['candidate_docs'],[])
        self.assertEqual(q2['diff_positions'],[0])
        q3=self.samples(report)['q3']['systems'][0]
        self.assertEqual(q3['candidate_states'],['irrelevant','unjudged'])
        self.assertEqual(q3['diff_positions'],[1])
        # the identical candidate c never forms a sample for any query
        for sample in report['samples']:
            self.assertEqual([s['system'] for s in sample['systems']],['b'])

    def test_missing_run_is_not_a_comparison(self):
        self.seed()
        # b has no run on q2 if we drop it; a missing run is skipped, not treated as empty
        self.apply(jsonl({'kind':'drop_run','system':'b','query':'q2'}))
        report=self.review(self.groups({'g1':['q1','q2'],'g2':['q3']}),budget=10)
        self.assertNotIn('q2',self.samples(report))
        # q2 only has the anchor run: not compared, no cell; q1 and q3 diverge
        self.assertEqual(report['summary']['compared_queries'],2)
        self.assertEqual(report['summary']['divergence_queries'],2)
        self.assertEqual(self.units(report)[('g1','b')]['candidate_count'],1)

    def test_top_truncates_relative_to_k(self):
        self.seed()
        report=self.review(self.groups({'g':['q1','q2','q3']}),top=5)  # k=3
        self.assertEqual(report['top'],5)
        q1=self.samples(report)['q1']['systems'][0]
        self.assertEqual(len(q1['anchor_docs']),3)  # k wins
        self.assertEqual(len(q1['candidate_docs']),3)
        self.tearDown();self.setUp()
        self.init(k=7)
        self.apply(jsonl({'kind':'system','id':'a'},{'kind':'system','id':'b'},
                         {'kind':'query','id':'q1','judgments':[{'doc':'H','grade':3}]},
                         {'kind':'run','system':'a','query':'q1','docs':['H','X','Y','Z']},
                         {'kind':'run','system':'b','query':'q1','docs':['Z','Y','X','H']}))
        wide=self.review(self.groups({'g1':['q1']}),top=5,budget=10)
        self.assertEqual(wide['k'],7);self.assertEqual(wide['top'],5)
        row=self.samples(wide)['q1']['systems'][0]
        self.assertEqual(row['anchor_docs'],['H','X','Y','Z'])
        # a=H X Y Z, b=Z Y X H -> both truncate to 4 docs, differing at 0 and 3
        self.assertEqual(row['diff_positions'],[0,3])

    def test_changing_judgments_changes_states(self):
        self.init()
        self.apply(jsonl({'kind':'system','id':'a'},{'kind':'system','id':'b'},
                         {'kind':'query','id':'q1','judgments':[{'doc':'X','grade':1}]},
                         {'kind':'run','system':'a','query':'q1','docs':['X']},
                         {'kind':'run','system':'b','query':'q1','docs':['Y']}))
        report=self.review(self.groups({'g1':['q1']}))
        row=self.samples(report)['q1']['systems'][0]
        self.assertEqual(row['anchor_states'],['relevant'])
        self.assertEqual(row['candidate_states'],['unjudged'])
        self.apply(jsonl({'kind':'query','id':'q1','judgments':[{'doc':'X','grade':0},
                                                               {'doc':'Y','grade':2}]}))
        report=self.review(self.groups({'g1':['q1']}))
        row=self.samples(report)['q1']['systems'][0]
        self.assertEqual(row['anchor_states'],['irrelevant'])
        self.assertEqual(row['candidate_states'],['relevant'])
        self.assertEqual(row['diff_positions'],[0])

    def test_identical_systems_do_not_diverge(self):
        self.init()
        self.apply(jsonl({'kind':'system','id':'a'},{'kind':'system','id':'b'},
                         {'kind':'query','id':'q1','judgments':[{'doc':'X','grade':1}]},
                         {'kind':'run','system':'a','query':'q1','docs':['X','Y']},
                         {'kind':'run','system':'b','query':'q1','docs':['X','Y']}))
        report=self.review(self.groups({'g1':['q1']}))
        self.assertEqual(report['samples'],[])
        self.assertEqual(report['summary']['divergence_cells'],0)
        self.assertEqual(self.units(report)[('g1','b')]['candidate_count'],0)
        self.assertFalse(self.units(report)[('g1','b')]['met'])
        self.assertFalse(self.units(report)[('g1','b')]['qualified'])
        self.assertTrue(report['summary']['coverage_complete'])

    def test_empty_group_and_unqualified_units_are_listed(self):
        self.seed()
        report=self.review(self.groups({'empty':[],'g1':['q1','q2','q3']}))
        units=self.units(report)
        # a group with no queries still contributes one unit per candidate system
        self.assertIn(('empty','b'),units)
        self.assertEqual(units[('empty','b')]['candidate_count'],0)
        self.assertFalse(units[('empty','b')]['met'])
        self.assertFalse(units[('empty','b')]['qualified'])
        self.assertEqual(report['groups'][0],{'id':'empty','query_count':0})
        self.assertTrue(report['summary']['coverage_complete'])

    def test_candidate_identity_is_per_query(self):
        """Only systems that diverge on that query appear in its sample."""
        self.init()
        self.apply(jsonl({'kind':'system','id':'a'},{'kind':'system','id':'b'},
                         {'kind':'system','id':'c'},
                         {'kind':'query','id':'q1','judgments':[{'doc':'X','grade':1}]},
                         {'kind':'query','id':'q2','judgments':[{'doc':'Y','grade':1}]},
                         {'kind':'run','system':'a','query':'q1','docs':['X']},
                         {'kind':'run','system':'b','query':'q1','docs':['Z']},   # diverges
                         {'kind':'run','system':'c','query':'q1','docs':['X']},   # agrees
                         {'kind':'run','system':'a','query':'q2','docs':['Y']},
                         {'kind':'run','system':'b','query':'q2','docs':['Y']},   # agrees
                         {'kind':'run','system':'c','query':'q2','docs':['W']}))  # diverges
        report=self.review(self.groups({'g':['q1','q2']}))
        samples=self.samples(report)
        self.assertEqual([s['system'] for s in samples['q1']['systems']],['b'])
        self.assertEqual([s['system'] for s in samples['q2']['systems']],['c'])
        self.assertEqual(self.units(report)[('g','b')]['candidate_count'],1)
        self.assertEqual(self.units(report)[('g','c')]['candidate_count'],1)

    def test_short_run_length_difference_is_a_divergence(self):
        self.init()
        self.apply(jsonl({'kind':'system','id':'a'},{'kind':'system','id':'b'},
                         {'kind':'query','id':'q1','judgments':[{'doc':'X','grade':1}]},
                         {'kind':'run','system':'a','query':'q1','docs':['X','Y']},
                         {'kind':'run','system':'b','query':'q1','docs':['X']}))
        row=self.samples(self.review(self.groups({'g':['q1']})))['q1']['systems'][0]
        self.assertEqual((row['anchor_states'],row['candidate_states']),
                         (['relevant','unjudged'],['relevant']))
        self.assertEqual(row['diff_positions'],[1])

    def test_empty_explicit_run_versus_nonempty(self):
        self.init()
        self.apply(jsonl({'kind':'system','id':'a'},{'kind':'system','id':'b'},
                         {'kind':'query','id':'q1','judgments':[{'doc':'X','grade':1}]},
                         {'kind':'run','system':'a','query':'q1','docs':[]},
                         {'kind':'run','system':'b','query':'q1','docs':['X']}))
        report=self.review(self.groups({'g':['q1']}))
        row=self.samples(report)['q1']['systems'][0]
        self.assertEqual(row['anchor_docs'],[])
        self.assertEqual(row['candidate_docs'],['X'])
        self.assertEqual(row['diff_positions'],[0])


class Selection(Case):
    def build(self, groups, systems, runs, k=5):
        """runs: {(system, query): docs}; judgments: doc grade by query prefix."""
        self.init(k=k)
        records=[{'kind':'system','id':s} for s in systems]
        queries=sorted({q for (_,q) in runs})
        for q in queries:
            records.append({'kind':'query','id':q,
                            'judgments':[{'doc':d,'grade':(1 if d.startswith('r') else 0)}
                                         for d in sorted({d for (_,qq),docs in runs.items()
                                                          if qq==q for d in docs})]})
        for (s,q),docs in sorted(runs.items()):
            records.append({'kind':'run','system':s,'query':q,'docs':docs})
        self.apply(jsonl(*records))
        return self.groups(groups)

    def test_budget_and_cell_floor(self):
        groups={'g1':['q1','q2','q3'],'g2':['q4']}
        runs={}
        for q in groups['g1']+groups['g2']:
            runs[('a',q)]=['r%s'%q]
            runs[('b',q)]=['x%s'%q]
        path=self.build(groups,['a','b'],runs)
        report=self.review(path,budget=1,floor=1)
        self.assertEqual(report['summary']['selected_queries'],1)
        self.assertEqual(len(report['samples']),1)
        self.assertLessEqual(len(report['samples']),1)
        units=self.units(report)
        self.assertEqual(set(units),{('g1','b'),('g2','b')})
        self.assertEqual(units[('g1','b')]['target'],1)
        self.assertFalse(report['summary']['coverage_complete'])
        # a floor above the sample count is never reported met
        report=self.review(path,budget=10,floor=4,name='pack2.json')
        units=self.units(report)
        self.assertEqual(units[('g1','b')]['candidate_count'],3)
        self.assertFalse(units[('g1','b')]['qualified'])
        self.assertFalse(units[('g1','b')]['met'])
        self.assertFalse(units[('g2','b')]['qualified'])
        self.assertEqual(report['summary']['qualified_units'],0)
        self.assertTrue(report['summary']['coverage_complete'])

    def test_floor_reached_per_unit(self):
        groups={'g1':['q1','q2','q3']}
        runs={}
        for q in groups['g1']:
            runs[('a',q)]=['r']
            runs[('b',q)]=['x']
        path=self.build(groups,['a','b','c'],runs)
        runs={}
        report=self.review(path,budget=10,floor=2)
        self.assertEqual(report['summary']['qualified_units'],1)
        self.assertEqual(report['summary']['met_units'],1)  # only g1/b qualified (g1/c has no run)
        units=self.units(report)
        self.assertEqual(units[('g1','b')]['selected'],3)
        self.assertTrue(units[('g1','b')]['met'])
        self.assertFalse(units[('g1','c')]['qualified'])
        self.assertEqual(units[('g1','c')]['candidate_count'],0)

    def test_tie_break_by_query_id(self):
        groups={'g1':['q2','q1']}
        runs={}
        for q in ('q1','q2'):
            runs[('a',q)]=['r']
            runs[('b',q)]=['x']
        path=self.build(groups,['a','b'],runs)
        report=self.review(path,budget=1,floor=1)
        self.assertEqual(self.samples(report)['q1']['query'],'q1')

    def test_no_candidates_stops(self):
        groups={'g1':['q1']}
        runs={('a','q1'):['r']}
        path=self.build(groups,['a'],runs)
        report=self.review(path,budget=10,floor=1)
        self.assertEqual(report['samples'],[])
        self.assertEqual(report['summary']['selected_queries'],0)
        self.assertEqual(report['systems']['candidates'],[])

    def test_group_system_quota_and_coverage_complete(self):
        groups={'g1':['q1','q2'],'g2':['q3','q4']}
        runs={}
        for q in ['q1','q2','q3','q4']:
            runs[('a',q)]=['r']
            runs[('b',q)]=['x']
            runs[('c',q)]=['y']
        path=self.build(groups,['a','b','c'],runs)
        report=self.review(path,budget=10,floor=1)
        units=self.units(report)
        self.assertEqual(len(units),4)
        self.assertTrue(all(u['met'] for u in units.values()))
        self.assertTrue(report['summary']['coverage_complete'])
        # two queries cover all four units; the rule keeps filling the budget while
        # unselected candidates remain, so all four queries are eventually chosen
        self.assertEqual(report['summary']['selected_queries'],4)
        small=self.review(path,budget=2,floor=1,name='pack2.json')
        self.assertEqual(small['summary']['selected_queries'],2)
        covered={(self.samples(report)[q]['group'],
                  s['system']) for q in self.samples(report)
                 for s in self.samples(report)[q]['systems']}
        self.assertEqual(covered,{('g1','b'),('g1','c'),('g2','b'),('g2','c')})

    def test_revision_and_byte_determinism(self):
        groups={'g2':['q4','q3'],'g1':['q2','q1']}
        runs={}
        for q in ['q1','q2','q3','q4']:
            runs[('a',q)]=['r']
            runs[('b',q)]=['x']
        path=self.build(groups,['b','a'],runs)
        first=self.dir/'first.json';second=self.dir/'second.json'
        self.review(path,budget=4,floor=1,name='first.json')
        self.review(path,budget=4,floor=1,name='second.json')
        self.assertEqual(first.read_bytes(),second.read_bytes())
        other=self.review(path,budget=4,floor=1,name='third.json')
        # reordering the groups array changes nothing about the pack
        reordered=self.groups({'g1':['q1','q2'],'g2':['q3','q4']},name='reordered.json')
        self.review(reordered,budget=4,floor=1,name='fourth.json')
        self.assertEqual(first.read_bytes(),(self.dir/'fourth.json').read_bytes())
        self.assertEqual([g['id'] for g in other['groups']],['g1','g2'])

    def test_incremental_correction_updates_quota(self):
        """Linked acceptance: fixing q's run removes its cover and another query is picked."""
        self.init(k=5)
        self.apply(jsonl({'kind':'system','id':'a'},{'kind':'system','id':'b'},
                         {'kind':'query','id':'q1','judgments':[{'doc':'R','grade':2}]},
                         {'kind':'query','id':'q2','judgments':[{'doc':'R','grade':2}]},
                         {'kind':'run','system':'a','query':'q1','docs':['R']},
                         {'kind':'run','system':'b','query':'q1','docs':['X']},
                         {'kind':'run','system':'a','query':'q2','docs':['R']},
                         {'kind':'run','system':'b','query':'q2','docs':['Y']}))
        path=self.groups({'g1':['q1','q2']})
        before=self.review(path,budget=1,floor=1,name='before.json')
        self.assertEqual(list(self.samples(before)),['q1'])
        self.assertEqual(self.units(before)[('g1','b')]['selected'],1)
        self.assertTrue(self.units(before)[('g1','b')]['met'])
        # correct q1's candidate run so the tri-state signature matches the anchor
        self.apply(jsonl({'kind':'run','system':'b','query':'q1','docs':['R']}))
        after=self.review(path,budget=1,floor=1,name='after.json')
        self.assertEqual(list(self.samples(after)),['q2'])
        self.assertEqual(after['revision'],2)
        unit=self.units(after)[('g1','b')]
        self.assertEqual(unit['candidate_count'],1)
        self.assertEqual(unit['selected'],1)
        self.assertTrue(unit['met'])

    def test_snapshot_is_one_complete_revision(self):
        groups={'g1':['q1']}
        runs={('a','q1'):['r'],('b','q1'):['x']}
        path=self.build(groups,['a','b'],runs)
        before=self.review(path,budget=1,floor=1,name='b.json')
        self.assertEqual(before['revision'],1)
        # an uncommitted writer must be invisible
        con=warehouse._connect(self.db,'rw')
        try:
            con.execute('BEGIN IMMEDIATE')
            con.execute('UPDATE meta SET value=99 WHERE key=?',('revision',))
            con.execute("INSERT INTO systems VALUES('ghost')")
            seen=self.review(path,budget=1,floor=1,name='c.json')
            self.assertEqual(seen['revision'],1)
            self.assertEqual(seen['systems']['candidates'],['b'])
        finally:
            con.execute('ROLLBACK');con.close()

    def test_committed_writer_mid_run_is_all_or_nothing(self):
        """A writer committing while the pack is built is never seen half-applied."""
        self.init(k=5)
        records=[{'kind':'system','id':'a'},{'kind':'system','id':'b'}]
        for q in ('q1','q2','q3','q4'):
            records.append({'kind':'query','id':q,'judgments':[{'doc':'R','grade':2}]})
            records.append({'kind':'run','system':'a','query':q,'docs':['R']})
            records.append({'kind':'run','system':'b','query':q,'docs':['X']})
        self.apply(jsonl(*records))
        path=self.groups({'g1':['q1','q2','q3','q4']})
        # the writer corrects every candidate run and bumps the revision mid-run
        rogue=warehouse._connect(self.db,'rw')
        rogue.execute('BEGIN IMMEDIATE')
        rogue.execute("UPDATE runs SET docs_json='[\"R\"]' WHERE system='b'")
        rogue.execute("UPDATE meta SET value=value+1 WHERE key='revision'")
        rogue.execute('COMMIT');rogue.close()
        pack=self.review(path,budget=2,floor=1,name='pack.json')
        self.assertEqual(pack['revision'],2)
        # revision 2 has no divergences at all: the run proves it read the new state,
        # never a mixture of old cells and the new revision counter
        self.assertEqual(pack['samples'],[])
        self.assertEqual(pack['summary']['divergence_cells'],0)
        self.assertEqual(pack['summary']['coverage_complete'],True)


class Rejection(Case):
    def prepare(self):
        self.init()
        self.apply(jsonl({'kind':'system','id':'a'},{'kind':'system','id':'b'},
                         {'kind':'query','id':'q1','judgments':[{'doc':'X','grade':1}]},
                         {'kind':'run','system':'a','query':'q1','docs':['X']},
                         {'kind':'run','system':'b','query':'q1','docs':['Y']}))
        self.out=self.dir/'pack.json';self.out.write_text('prior')
    def test_bad_groups_preserve_output(self):
        self.prepare()
        bad=[{'groups':[{'id':'g','queries':['q1','q1']}]},
             {'groups':[{'id':'g','queries':['q1']},{'id':'g','queries':[]}]},
             {'groups':[{'id':'g1','queries':['q1']},{'id':'g2','queries':['q1']}]},
             {'groups':[{'id':'g','queries':['ghost']}]},
             {'groups':[{'id':'g','queries':[]}]},  # q1 belongs to no group
             {'groups':[{'id':'','queries':['q1']}]},
             {'groups':[{'id':' g','queries':['q1']}]},
             {'groups':[{'id':'g','queries':[' q1']}]},
             {'groups':[{'id':'g','queries':'q1'}]},
             {'groups':[{'id':'g','queries':['q1'],'extra':1}]},
             {'groups':[]},
             [],]
        for index,payload in enumerate(bad):
            path=self.dir/'bad.json'
            path.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8')
            result=self.review(path,expect=2,name='pack.json')
            self.assertEqual(self.out.read_text(),'prior','case %d'%index)
        # duplicate JSON keys are rejected by the shared strict reader
        path=self.dir/'bad.json'
        path.write_text('{"groups":[{"id":"g","queries":["q1"],"queries":[]}]}')
        self.review(path,expect=2);self.assertEqual(self.out.read_text(),'prior')

    def test_bad_anchor_and_bounds_preserve_output(self):
        self.prepare()
        path=self.groups({'g':['q1']})
        for anchor in ('ghost',''):
            self.review(path,anchor=anchor,expect=2)
            self.assertEqual(self.out.read_text(),'prior')
        for top in (0,101,-1):
            self.review(path,top=top,expect=2);self.assertEqual(self.out.read_text(),'prior')
        for budget in (0,501):
            self.review(path,budget=budget,expect=2);self.assertEqual(self.out.read_text(),'prior')
        for floor in (0,21):
            self.review(path,floor=floor,expect=2);self.assertEqual(self.out.read_text(),'prior')

    def test_bad_warehouse_and_paths(self):
        self.prepare()
        path=self.groups({'g':['q1']})
        other=self.dir/'not.db';other.write_text('x')
        result=cli('review-pack','--db',str(other),'--groups',str(path),'--anchor','a',
                   '--top','3','--budget','2','--cell-floor','1','--output',str(self.out))
        self.assertEqual(result.returncode,2);self.assertEqual(self.out.read_text(),'prior')
        missing=self.dir/'missing.db'
        result=cli('review-pack','--db',str(missing),'--groups',str(path),'--anchor','a',
                   '--top','3','--budget','2','--cell-floor','1','--output',str(self.out))
        self.assertEqual(result.returncode,2);self.assertFalse(missing.exists())
        # output may not alias an input, and may not be a symlink
        result=cli('review-pack','--db',str(self.db),'--groups',str(path),'--anchor','a',
                   '--top','3','--budget','2','--cell-floor','1','--output',str(self.db))
        self.assertEqual(result.returncode,2)
        result=cli('review-pack','--db',str(self.db),'--groups',str(path),'--anchor','a',
                   '--top','3','--budget','2','--cell-floor','1','--output',str(path))
        self.assertEqual(result.returncode,2)
        link=self.dir/'link.json';link.symlink_to(self.out)
        result=cli('review-pack','--db',str(self.db),'--groups',str(path),'--anchor','a',
                   '--top','3','--budget','2','--cell-floor','1','--output',str(link))
        self.assertEqual(result.returncode,2);self.assertEqual(self.out.read_text(),'prior')

    def test_missing_groups_file(self):
        self.prepare()
        result=cli('review-pack','--db',str(self.db),'--groups',str(self.dir/'nope.json'),
                   '--anchor','a','--top','3','--budget','2','--cell-floor','1',
                   '--output',str(self.out))
        self.assertEqual(result.returncode,2);self.assertEqual(self.out.read_text(),'prior')


class Capacity(Case):
    QUERIES=50000;SYSTEMS=32;JUDGED=120;RUN=100
    def test_large_pack_capacity(self):
        self.init(k=100)
        con=sqlite3.connect(str(self.db),isolation_level=None)
        try:
            con.execute('PRAGMA synchronous=OFF');con.execute('BEGIN')
            systems=['s%02d'%i for i in range(self.SYSTEMS)]
            con.executemany('INSERT INTO systems VALUES(?)',[(s,) for s in systems])
            # grade 0 every fourth doc, so tri-state signatures genuinely diverge
            con.executemany('INSERT INTO queries VALUES(?,?,?)',
                            [('q%05d'%q,self.JUDGED,self.JUDGED) for q in range(self.QUERIES)])
            con.executemany('INSERT INTO judgments VALUES(?,?,?)',
                            (('q%05d'%q,'d%05d_%03d'%(q,j),0 if j%4==0 else 1+j%3)
                             for q in range(self.QUERIES) for j in range(self.JUDGED)))
            con.executemany('INSERT INTO runs VALUES(?,?,?)',
                            ((s,'q%05d'%q,
                              '['+','.join('"d%05d_%03d"'%(q,(j+si*3)%(self.JUDGED+40))
                                           for j in range(self.RUN))+']')
                             for si,s in enumerate(systems) for q in range(self.QUERIES)))
            con.execute("UPDATE meta SET value=? WHERE key='total_queries'",(self.QUERIES,))
            con.execute("UPDATE meta SET value=? WHERE key='relevant_queries'",(self.QUERIES,))
            con.execute('COMMIT')
        finally:
            con.close()
        groups={'g%d'%i:['q%05d'%q for q in range(self.QUERIES) if q%10==i] for i in range(10)}
        path=self.groups(groups,name='full.json')
        code,rss,err=cli_rss('review-pack','--db',str(self.db),'--groups',str(path),
                             '--anchor','s00','--top','100','--budget','500','--cell-floor','20',
                             '--output',str(self.dir/'pack.json'))
        self.assertEqual(code,0,err)
        print('\ncapacity: review-pack peak RSS %.1f MiB'%(rss/1024))
        self.assertLessEqual(rss,RSS_LIMIT,'review-pack peak RSS')
        report=json.loads((self.dir/'pack.json').read_text())
        self.assertEqual(report['revision'],0)
        self.assertEqual(report['summary']['query_count'],self.QUERIES)
        self.assertEqual(len(report['samples']),500)
        self.assertEqual(len(report['units']),10*31)
        self.assertTrue(report['summary']['coverage_complete'])
        # byte-for-byte determinism at scale
        again=cli('review-pack','--db',str(self.db),'--groups',str(path),
                  '--anchor','s00','--top','100','--budget','500','--cell-floor','20',
                  '--output',str(self.dir/'pack2.json'))
        self.assertEqual(again.returncode,0,again.stderr)
        self.assertEqual((self.dir/'pack.json').read_bytes(),
                         (self.dir/'pack2.json').read_bytes())


class SelectorProperty(unittest.TestCase):
    """The incremental quota updates must equal a direct re-derivation every round."""
    def brute(self,cells,floor,budget):
        owned={}
        for q,grp,system,diff in cells:
            owned.setdefault(q,[]).append((grp,system,diff))
        count={}
        for q,items in owned.items():
            for grp,system,_ in items:
                count[(grp,system)]=count.get((grp,system),0)+1
        qualified={unit:total>=floor for unit,total in count.items()}
        cov={unit:0 for unit in count}
        remaining=set(owned);chosen=[]
        for _ in range(budget):
            if not remaining:break
            def key(q):
                fill=sum(1 for g,s,_ in owned[q] if qualified[(g,s)] and cov[(g,s)]==floor-1)
                first=sum(1 for g,s,_ in owned[q] if qualified[(g,s)] and cov[(g,s)]==0)
                return (-fill,-first,-len(owned[q]),-sum(d for _,_,d in owned[q]),q)
            q=min(remaining,key=key)
            remaining.remove(q);chosen.append(q)
            for g,s,_ in owned[q]:cov[(g,s)]+=1
        return chosen
    def incremental(self,cells,groups,systems,anchor,floor,budget):
        con=sqlite3.connect(':memory:')
        try:
            for statement in review._TEMP_SCHEMA.split(';'):
                if statement.strip():con.execute(statement)
            con.execute('CREATE TEMP TABLE systems(system TEXT PRIMARY KEY)')
            con.executemany('INSERT INTO groups VALUES(?,?)',[(g,0) for g in groups])
            con.executemany('INSERT INTO systems VALUES(?)',[(s,) for s in systems])
            con.executemany('INSERT INTO cells VALUES(?,?,?,?)',cells)
            con.execute('CREATE INDEX cells_unit_index ON cells(grp, system, query)')
            review._units(con,anchor,floor)
            return review._select(con,budget,floor)
        finally:
            con.close()
    def test_matches_brute_force(self):
        import random
        rng=random.Random(7)
        for _ in range(300):
            groups=['g%d'%i for i in range(rng.randint(1,4))]
            systems=['s%d'%i for i in range(rng.randint(2,4))]
            anchor=systems[0];floor=rng.randint(1,4);budget=rng.randint(1,6)
            cells=[];seen=set()
            for index in range(rng.randint(1,7)):
                q='q%d'%index
                for _ in range(rng.randint(1,5)):
                    grp=rng.choice(groups);system=rng.choice(systems[1:])
                    if (q,system) in seen:continue
                    seen.add((q,system))
                    cells.append((q,grp,system,rng.randint(1,4)))
            if not cells:continue
            self.assertEqual(self.brute(cells,floor,budget),
                             self.incremental(cells,groups,systems,anchor,floor,budget),
                             (cells,floor,budget))
