from __future__ import annotations

import argparse
import json

from job_apps_system.db.session import get_db_session, init_db
from job_apps_system.services.scheduler import run_scheduler_tick


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force-agent", default=None)
    args = parser.parse_args()

    init_db()
    with get_db_session() as session:
        result = run_scheduler_tick(session, force_agent_name=args.force_agent)
    print(json.dumps(result.model_dump(mode="json"), indent=2))


if __name__ == "__main__":
    main()
