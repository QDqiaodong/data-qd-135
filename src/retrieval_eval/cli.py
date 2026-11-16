DESCRIPTION='Judgment-aware ranked retrieval evaluation and query coverage'
import argparse, json, sqlite3, sys
from pathlib import Path
from .common import read, save
from .model import analyze
from . import warehouse

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

COMMANDS={'warehouse-init':warehouse_init,'warehouse-apply':warehouse_apply,'warehouse-report':warehouse_report}

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
