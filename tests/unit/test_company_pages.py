from __future__ import annotations

import unittest

from job_apps_system.integrations.company_pages import (
    extract_company_domain,
    extract_company_website_from_ashby_html,
    extract_domain_from_email,
)


class CompanyPagesTests(unittest.TestCase):
    def test_extract_company_website_from_ashby_html_reads_public_website(self) -> None:
        html = '{"organization":{"name":"Kodex","publicWebsite":"https:\\/\\/www.kodexglobal.com\\/","customJobsPageUrl":"https:\\/\\/jobs.ashbyhq.com\\/kodex"}}'
        self.assertEqual(
            extract_company_website_from_ashby_html(html),
            "https://www.kodexglobal.com/",
        )

    def test_extract_company_domain_ignores_linkedin_and_accepts_real_company_host(self) -> None:
        self.assertIsNone(extract_company_domain("https://www.linkedin.com/company/thekodex/life"))
        self.assertEqual(extract_company_domain("https://www.kodexglobal.com/"), "kodexglobal.com")

    def test_extract_domain_from_email_returns_email_host(self) -> None:
        self.assertEqual(extract_domain_from_email("donahue@kodexglobal.com"), "kodexglobal.com")


if __name__ == "__main__":
    unittest.main()
