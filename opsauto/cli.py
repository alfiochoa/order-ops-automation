"""Ops CLI:  python -m opsauto.cli dlq | replay [--id N] | audit [-n 20]"""
import argparse
import json

from .pipeline import from_env


def main(argv=None):
    ap = argparse.ArgumentParser(prog="opsauto")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("dlq", help="list open dead letters")
    r = sub.add_parser("replay", help="retry dead letters")
    r.add_argument("--id", type=int)
    a = sub.add_parser("audit", help="tail audit log")
    a.add_argument("-n", type=int, default=20)
    args = ap.parse_args(argv)
    p = from_env()
    if args.cmd == "dlq":
        for d in p.store.dlq_open():
            print(f"#{d['id']} {d['stage']:8} {d['idem_key']} attempts={d['attempts']} err={d['error']}")
    elif args.cmd == "replay":
        print(json.dumps(p.replay(args.id)))
    else:
        for e in reversed(p.store.audit_tail(args.n)):
            print(e["ts"], e["actor"], e["action"], e["idem_key"], e["detail"])


if __name__ == "__main__":
    main()
