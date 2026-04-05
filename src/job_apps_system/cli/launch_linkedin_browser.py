import argparse

from job_apps_system.integrations.linkedin.browser import launch_linkedin_browser


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile-path", default=None)
    parser.add_argument("--start-url", default="https://www.linkedin.com/feed/")
    args = parser.parse_args()
    launch_linkedin_browser(profile_path=args.profile_path, start_url=args.start_url)


if __name__ == "__main__":
    main()
