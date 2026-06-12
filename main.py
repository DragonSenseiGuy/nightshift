from emails import fetch_emails_last_x_hours, get_credentials
from summarise import summarise_emails
from send_emails import send_to_self


def main():
    print("NightShift — fetching your recent emails\n")
    credentials = get_credentials()
    detailed = fetch_emails_last_x_hours(credentials, hours=2)

    if not detailed:
        return

    print("\nSummarising...")
    html = summarise_emails(detailed)
    
    send_to_self(
        credentials,
        subject="Your morning digest",
        html_body=html,
    )


if __name__ == "__main__":
    main()
