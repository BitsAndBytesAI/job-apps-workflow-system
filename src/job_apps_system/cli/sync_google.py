from __future__ import annotations

import json

from job_apps_system.db.session import get_db_session
from job_apps_system.services.sheet_sync import SheetSyncService


def main() -> None:
    with get_db_session() as session:
        service = SheetSyncService(session)
        results = {
            "headers": service.ensure_configured_headers(),
            "em_jobs_sync": service.sync_em_jobs_to_db(),
        }
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
