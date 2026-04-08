import argparse
import sys

from job_apps_system.runtime.launcher import launch_backend


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    raise SystemExit(launch_backend(check_only=args.check_only, host=args.host, port=args.port))


if __name__ == "__main__":
    main()
