import imaplib
import email
from email.header import decode_header
import os
from dotenv import load_dotenv

load_dotenv()

IMAP_SERVER = os.getenv("IMAP_SERVER", "imap.gmail.com")
EMAIL_ACCOUNT = os.getenv("EMAIL_ACCOUNT")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")


def fetch_latest_emails(limit: int = 5):
    if not EMAIL_ACCOUNT or not EMAIL_PASSWORD:
        raise ValueError("Email credentials are not set in the .env file.")

    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
    mail.select("inbox")

    status, messages = mail.search(None, "ALL")
    if status != "OK" or not messages[0]:
        return []

    email_ids = messages[0].split()
    latest_email_ids = email_ids[-limit:]

    extracted_emails = []

    for e_id in reversed(latest_email_ids):
        _, msg_data = mail.fetch(e_id, "(RFC822)")

        for response_part in msg_data:
            if isinstance(response_part, tuple):
                msg = email.message_from_bytes(response_part[1])

                subject, encoding = decode_header(msg["Subject"])[0]
                if isinstance(subject, bytes):
                    subject = subject.decode(encoding if encoding else "utf-8")

                from_ = msg.get("From")

                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        content_type = part.get_content_type()
                        content_disposition = str(part.get("Content-Disposition"))
                        try:
                            if content_type == "text/plain" and "attachment" not in content_disposition:
                                body = part.get_payload(decode=True).decode()
                                break
                        except Exception:
                            pass
                else:
                    content_type = msg.get_content_type()
                    if content_type == "text/plain":
                        body = msg.get_payload(decode=True).decode()

                if body:
                    extracted_emails.append({
                        "id": e_id.decode(),
                        "from": from_,
                        "subject": subject,
                        "body": body.strip()
                    })

    mail.close()
    mail.logout()
    return extracted_emails


if __name__ == "__main__":
    try:
        emails = fetch_latest_emails(limit=2)
        print(f"Fetched {len(emails)} emails.")
        for i, eml in enumerate(emails, 1):
            print(f"\n--- Email {i} ---")
            print(f"Subject: {eml['subject']}")
            print(f"Body: {eml['body'][:200]}...")
    except Exception as e:
        print(f"Error fetching emails: {e}")
