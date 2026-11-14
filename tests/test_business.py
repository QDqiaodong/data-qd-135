
import copy,json,unittest,tempfile,subprocess,sys,hashlib
from pathlib import Path
from retrieval_eval.model import analyze
ROOT=Path(__file__).resolve().parents[1]
def sample():return json.loads((ROOT/'fixtures/input.json').read_text())
class Business(unittest.TestCase):
    def bad(self,d):
        with self.assertRaises((ValueError,TypeError)):analyze(d)
    def test_unknown_top_field(self):
        d=sample();d['unexpected']=1;self.bad(d)

    def test_metrics(self):
        r=analyze(sample());q=r['queries'][0];self.assertAlmostEqual(q['precision_at_k'],2/3);self.assertEqual(q['recall_at_k'],1);self.assertAlmostEqual(q['average_precision_at_k'],5/6);self.assertAlmostEqual(q['ndcg_at_k'],7.5/(7+1/__import__('math').log2(3)));self.assertEqual(q['unjudged_docs_at_k'],['X'])
    def test_missing_and_null(self):
        r=analyze(sample());self.assertFalse(r['queries'][1]['has_run']);self.assertEqual(r['queries'][1]['recall_at_k'],0);self.assertIsNone(r['queries'][2]['recall_at_k']);self.assertEqual(r['macro']['recall_at_k']['eligible_queries'],2);self.assertEqual(r['macro']['recall_at_k']['value'],0.5)
    def test_short_list(self):
        d=sample();d['runs'][0]['docs']=['A'];self.assertAlmostEqual(analyze(d)['queries'][0]['precision_at_k'],1/3)
    def test_cutoff(self):
        d=sample();d['k']=1;r=analyze(d)['queries'][0];self.assertEqual(r['average_precision_at_k'],1);self.assertEqual(r['recall_at_k'],0.5)
    def test_duplicate_doc(self):
        d=sample();d['runs'][0]['docs']=['A','A'];self.bad(d)
    def test_unknown_query(self):
        d=sample();d['runs'][0]['query']='unknown';self.bad(d)
    def test_bad_grade(self):
        d=sample();d['queries'][0]['judgments'][0]['grade']=True;self.bad(d)
    def test_order(self):
        d=sample();d['queries'].reverse();d['runs'].reverse()
        for q in d['queries']:q['judgments'].reverse()
        self.assertEqual(analyze(d),analyze(sample()))

class Command(unittest.TestCase):
    def call(self,source,output):
        return subprocess.run([sys.executable,'-m','retrieval_eval.cli','--source',str(source),'--output',str(output)],capture_output=True,text=True)
    def test_installed_cli_replay(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'out.json';src=ROOT/'fixtures/input.json';r=self.call(src,out);self.assertEqual(r.returncode,0,r.stderr);first=out.read_bytes();report=json.loads(first);self.assertEqual(report['source_sha256'],hashlib.sha256(src.read_bytes()).hexdigest());self.assertEqual(self.call(src,out).returncode,0);self.assertEqual(first,out.read_bytes())
    def test_failure_preserves_output(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'bad.json';out=Path(td)/'out.json';out.write_text('prior')
            for body in ['{}','{"x":1,"x":2}','NaN']:
                src.write_text(body);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(out.read_text(),'prior')
    def test_output_safety(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'in.json';src.write_bytes((ROOT/'fixtures/input.json').read_bytes());before=src.read_bytes();self.assertEqual(self.call(src,src).returncode,2);self.assertEqual(src.read_bytes(),before);out=Path(td)/'link.json';out.symlink_to(src);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(src.read_bytes(),before)
