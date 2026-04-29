from __future__ import annotations

import unittest
from unittest.mock import patch

from job_apps_system.integrations.linkedin.scraper import LinkedInScraper
from job_apps_system.schemas.jobs import ScrapedJob


class LinkedInScraperCompanyIdentityTests(unittest.TestCase):
    def test_refresh_company_identity_uses_ashby_public_website(self) -> None:
        scraper = LinkedInScraper("browser-profiles/test")
        job = ScrapedJob(
            id="job-123",
            company_name="Kodex",
            job_title="Manager, Engineering",
            job_description="",
            company_url="https://www.linkedin.com/company/thekodex/life",
            apply_url="https://jobs.ashbyhq.com/kodex?ashby_jid=123",
            search_url="https://www.linkedin.com/jobs/search/",
        )

        with patch(
            "job_apps_system.integrations.linkedin.scraper.resolve_company_website_from_apply_url",
            return_value=("https://www.kodexglobal.com/", "kodexglobal.com"),
        ):
            scraper._refresh_company_identity(job)

        self.assertEqual(job.company_url, "https://www.kodexglobal.com/")
        self.assertEqual(job.company_domain, "kodexglobal.com")


if __name__ == "__main__":
    unittest.main()
