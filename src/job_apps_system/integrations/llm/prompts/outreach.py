OUTREACH_SYSTEM_PROMPT = (
    "You write short, professional cold outreach emails from a job applicant to a "
    "decision-maker at the hiring company. Your tone is warm, specific, and respectful "
    "of the recipient's time. You return only valid JSON."
)

OUTREACH_PROMPT_TEMPLATE = """Write a cold outreach email from the applicant to a decision-maker at the company that posted the job below.

Applicant
---------
Name: {applicant_name}
Current title: {applicant_current_title}
Current company: {applicant_current_company}
Years of experience: {applicant_years}
Highlights: {applicant_highlights}

Job
---
Title: {job_title}
Company: {company_name}
Description: {job_description}

Constraints
-----------
- The recipient's name and title are not known yet. Use the literal placeholders <contact name> and <contact title> wherever you would address or refer to them. Use those exact strings (with the angle brackets and a single space).
- Keep the body to 90-140 words.
- Open with a one-line personal hook tied to the company or role.
- Reference 1-2 concrete points from the applicant's background that map to the job.
- Close with a soft ask (a brief call or feedback on fit).
- Sign off with the applicant's first name only.
- Do NOT include a resume link, attachment text, signature block, or footer. The system appends the resume link automatically.
- Subject line: under 60 characters, specific to the role and company, no clickbait.

Output contract
{{
  "subject": "string",
  "body": "string with <contact name> and <contact title> placeholders"
}}

Return only the JSON object.
"""
