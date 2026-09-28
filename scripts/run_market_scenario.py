"""Local-only deterministic replay. No server, broker client, threads or deployment."""
import argparse
import json
from pathlib import Path
import sys

# Support the requested `python scripts/run_market_scenario.py ...` invocation.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from scenarios.runner import NAMES, human, run


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('scenario', nargs='?', choices=NAMES)
    parser.add_argument('--list', action='store_true', help='List named scenarios')
    parser.add_argument('--all', action='store_true', help='Run every scenario')
    parser.add_argument('--json', action='store_true', help='Print machine-readable results')
    args = parser.parse_args(argv)
    if args.list:
        print('\n'.join(NAMES))
        return 0
    if args.all and args.scenario:
        parser.error('Choose a scenario or --all, not both.')
    if not args.all and not args.scenario:
        parser.error('Choose a scenario, --all, or --list.')
    names = NAMES if args.all else (args.scenario,)
    results = [run(name) for name in names]
    print(json.dumps(results if args.all else results[0], indent=2, allow_nan=False) if args.json
          else '\n\n'.join(human(result) for result in results))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
