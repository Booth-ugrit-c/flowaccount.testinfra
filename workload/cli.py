import argparse
import sys

from workload import runner, services, spec as spec_mod

EXIT_FAILED = 1
EXIT_INVALID = 2
EXIT_TERMINATED = 143


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def cmd_run(args):
    try:
        text = args.job_json if args.job_json is not None else _read(args.job_file)
        job = spec_mod.parse_spec_json(text)
    except (spec_mod.SpecError, OSError, UnicodeDecodeError) as exc:
        message = str(exc) if isinstance(exc, spec_mod.SpecError) else f"cannot read spec: {exc.__class__.__name__}"
        result = runner.make_result(None, False, None, None, None, None, f"invalid spec: {message}")
        print(runner.format_line(result), flush=True)
        return EXIT_INVALID
    runner.install_signal_handlers()
    result = runner.run_job(job)
    print(runner.format_line(result), flush=True)
    if result["ok"]:
        return 0
    return EXIT_TERMINATED if result["error"] == "terminated by signal" else EXIT_FAILED


def cmd_check(args):
    try:
        problems = spec_mod.check_specs(_read(args.file))
    except (OSError, UnicodeDecodeError) as exc:
        problems = [f"cannot read file: {exc.__class__.__name__}"]
    for line in problems:
        print(line)
    if problems:
        return EXIT_INVALID
    print("all specs valid")
    return 0


def cmd_leftovers(args):
    try:
        names = services.leftovers()
    except (OSError, services.ServiceFailure) as exc:
        print(f"cannot list containers: {exc}", file=sys.stderr)
        return EXIT_INVALID
    for name in names:
        print(name)
    return EXIT_FAILED if names else 0


def build_parser():
    parser = argparse.ArgumentParser(prog="python -m workload")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="burn CPU, hold memory, start services")
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument("--job-json")
    source.add_argument("--job-file")
    run.set_defaults(func=cmd_run)
    check = sub.add_parser("check", help="validate a JSON array of job specs")
    check.add_argument("file")
    check.set_defaults(func=cmd_check)
    left = sub.add_parser("leftovers", help="list containers left by earlier runs")
    left.set_defaults(func=cmd_leftovers)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)
