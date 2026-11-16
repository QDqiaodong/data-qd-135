DESCRIPTION='Judgment-aware ranked retrieval evaluation and query coverage'
import argparse, json, sqlite3, sys
from pathlib import Path
from .common import read, save
from .model import analyze
from . import warehouse
from . import contract
from . import session as session_store

FAILURES=(ValueError,TypeError,KeyError,OSError,OverflowError,RecursionError,sqlite3.Error)

def run(source):
    data,digest=read(source)
    return {'schema_version':1,'source_sha256':digest,'result':analyze(data)}

def warehouse_init(argv):
    parser=argparse.ArgumentParser(prog='retrieval-eval warehouse-init',description='Create a new empty evaluation warehouse (revision 0).')
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--k',type=int,required=True)
    args=parser.parse_args(argv)
    try:
        result=warehouse.init(args.db,args.k)
    except FAILURES as error:
        print(str(error),file=sys.stderr);return 2
    print(json.dumps({'db':str(args.db),**result},sort_keys=True,ensure_ascii=False,allow_nan=False));return 0

def warehouse_apply(argv):
    parser=argparse.ArgumentParser(prog='retrieval-eval warehouse-apply',description='Atomically apply one JSONL fact batch; each successful non-empty batch increments the revision by one.')
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--source',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        if args.source.resolve()==args.db.resolve():raise ValueError('input and output must differ')
        result=warehouse.apply(args.db,args.source)
    except FAILURES as error:
        print(str(error),file=sys.stderr);return 2
    print(json.dumps(result,sort_keys=True,ensure_ascii=False,allow_nan=False));return 0

def warehouse_report(argv):
    parser=argparse.ArgumentParser(prog='retrieval-eval warehouse-report',description='Publish a deterministic report from persisted warehouse state.')
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--system',default=None)
    args=parser.parse_args(argv)
    try:
        if args.db.resolve()==args.output.resolve():raise ValueError('input and output must differ')
        result=warehouse.report(args.db,args.output,args.system)
    except FAILURES as error:
        print(str(error),file=sys.stderr);return 2
    summary={'db':str(args.db),'output':str(args.output),'revision':result['revision'],'systems':len(result['systems'])}
    print(json.dumps(summary,sort_keys=True,ensure_ascii=False,allow_nan=False));return 0

def session_init(argv):
    parser=argparse.ArgumentParser(prog='retrieval-eval session-init',description='Create a new independent session warehouse at revision 0.')
    parser.add_argument('--db',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        result=session_store.init(args.db)
    except FAILURES as error:
        print(str(error),file=sys.stderr);return 2
    print(json.dumps({'db':str(args.db),**result},sort_keys=True,ensure_ascii=False,allow_nan=False));return 0

def session_apply(argv):
    parser=argparse.ArgumentParser(prog='retrieval-eval session-apply',description='Atomically apply one strict session-event JSONL batch.')
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--source',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        result=session_store.apply(args.db,args.source)
    except FAILURES as error:
        print(str(error),file=sys.stderr);return 2
    print(json.dumps(result,sort_keys=True,ensure_ascii=False,allow_nan=False));return 0

def session_report(argv):
    parser=argparse.ArgumentParser(prog='retrieval-eval session-report',description='Publish persisted session and attribution facts for one revision.')
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        result=session_store.report(args.db,args.output)
    except FAILURES as error:
        print(str(error),file=sys.stderr);return 2
    print(json.dumps({'db':str(args.db),'output':str(args.output),**result},sort_keys=True,ensure_ascii=False,allow_nan=False));return 0

def contract_impact(argv):
    parser=argparse.ArgumentParser(prog='retrieval-eval contract-impact',description='Review an offline production contract upgrade: full field diff plus per-consumer read impact.')
    parser.add_argument('--baseline',type=Path,required=True)
    parser.add_argument('--candidate',type=Path,required=True)
    parser.add_argument('--consumers',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        output=args.output.resolve()
        for source in (args.baseline,args.candidate,args.consumers):
            if source.resolve()==output:raise ValueError('input and output must differ')
        report=contract.impact(args.baseline,args.candidate,args.consumers,args.output)
    except FAILURES as error:
        print(str(error),file=sys.stderr);return 2
    summary={'output':str(args.output),'dataset':report['dataset'],
             'baseline_version':report['baseline_version'],'candidate_version':report['candidate_version'],
             'field_diffs':len(report['field_diffs']),'blocked':report['summary']['blocked'],
             'warning':report['summary']['warning'],'compatible':report['summary']['compatible']}
    print(json.dumps(summary,sort_keys=True,ensure_ascii=False,allow_nan=False));return 0

COMMANDS={'warehouse-init':warehouse_init,'warehouse-apply':warehouse_apply,'warehouse-report':warehouse_report,
          'session-init':session_init,'session-apply':session_apply,'session-report':session_report,
          'contract-impact':contract_impact}

def main():
    argv=sys.argv[1:]
    if argv and argv[0] in COMMANDS:return COMMANDS[argv[0]](argv[1:])
    parser=argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        if args.source.resolve()==args.output.resolve():raise ValueError('input and output must differ')
        report=run(args.source);save(args.output,report)
    except FAILURES as error:
        print(str(error),file=sys.stderr);return 2
    print(json.dumps(report,sort_keys=True,ensure_ascii=False,allow_nan=False));return 0

if __name__=='__main__':raise SystemExit(main())
