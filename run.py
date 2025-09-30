import email
import os
import re
import smtplib
import sys
import ssl
import tempfile
from email import encoders, policy
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import markdown2
import requests
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build
from imapclient import IMAPClient
from openai import OpenAI
from xhtml2pdf import pisa

load_dotenv()

HR_KEYWORDS = [
    "hr",
    "human resource",
    "recruiting",
    "recruiter",
    "talent",
    "people",
    "people ops",
    "people operations",
    "staffing",
    "recruitment",
    "resource management",
    "talent acquisition"
]

SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE_PATH")
HUNTER_API_KEY = os.getenv("HUNTER_API_KEY")

client = OpenAI()

def get_default_cv():
    try:
        SCOPES = ["https://www.googleapis.com/auth/documents.readonly"]

        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES
        )

        service = build("docs", "v1", credentials=creds)

        document_id = ""
        if len(sys.argv) == 2:
            document_id = sys.argv[1]
        if not document_id:
            document_id = os.getenv("DEFAULT_RESUME_ID")
        if not document_id:
            print("No default resume id provided")
            sys.exit(1)

        doc = service.documents().get(documentId=document_id).execute()
        text = []
        for element in doc.get("body", {}).get("content", []):
            if "paragraph" in element:
                for el in element["paragraph"]["elements"]:
                    if "textRun" in el:
                        text.append(el["textRun"]["content"])

        return "".join(text)
    except Exception as e:
        print("Failed to get default resume: ", e)
        return ""


def load_job_offers(max_items=100):
    APIFY_API_KEY = os.getenv("APIFY_API_KEY")
    if not APIFY_API_KEY:
        print("No API key provided")
        sys.exit(1)

    url = f"https://api.apify.com/v2/acts/hKByXkMQaC5Qt9UMN/run-sync-get-dataset-items"
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'Authorization': f'Bearer {APIFY_API_KEY}'
    }

    payload = {
        "urls": [
            "https://www.linkedin.com/jobs/search/?currentJobId=4305981446&f_E=2%2C3%2C4&f_T=9%2C25201%2C39%2C25170"
            "%2C25194%2C3172%2C24%2C10738%2C25169%2C30006%2C191%2C17265&f_TPR=r604800&f_WT=2&keywords=remote&origin"
            "=JOB_SEARCH_PAGE_JOB_FILTER&sortBy=R"
        ],
        "count": max_items
    }

    response = requests.request("POST", url, headers=headers, json=payload)
    if response.status_code >= 400:
        print("Failed to get job offers: ", response.text)
        sys.exit(1)

    return response.json()


def is_offer_suitable(job_offer, default_cv_text):
    SYS_MESSAGE = "You're helpful, intelligent AI resume filtering assistant. Your task is to filter out inappropriate job vacancies."
    USER_MESSAGE = (
        "I'm looking for jobs. Your task is to filter them based on the skills and attributes that i have. Some jobs may not be relevant, so that's why i want you to go through each job and tell me if i'm OK fit."
        "Below is a block of context about me and my skills:"
        f"{default_cv_text}\n------"
        f"Here's job description: {job_offer['descriptionText']}"
        'respond in the next text format (without markdown, just text): {"verdict": "true" / "false"}'
        "if i'm a fit return true, if im not return false (both strings)")

    try:
        response = client.chat.completions.create(
            model="gpt-4.1-nano",
            messages=[
                {"role": "system", "content": SYS_MESSAGE},
                {"role": "user", "content": USER_MESSAGE}
            ]
        )
        if not response.choices or len(response.choices) == 0:
            print("No completions returned. ---SKIPPING---")
            result = {"verdict": "false"}
        else:
            result = response.choices[0].message.content
    except Exception as e:
        print("Failed to get job offer. ---SKIPPING---:", str(e))
        result = {"verdict": "false"}

    return result


def rebuild_default_cv(job_offer, default_cv_text):
    SYS_MESSAGE = "You're helpful, intelligent AI job resume customisation assistant."
    USER_MESSAGE = ("I'm looking for jobs. Your task is to customise the provided resume using a job description to fit to that job."
                    "-----"
                    f"Here's a job description: {job_offer['descriptionText']}"
                    "-----"
                    f"Here's a CV that you need to rebuild: {default_cv_text}"
                    "----"
                    "Respond with only the customised resume with great formatting and styling (try to fit it on the one page), nothing else"
                    "Write it in Markdown (atx) format. Do not output any backticks (no ```). keep links clickable ")
    try:
        response = client.chat.completions.create(
            model="gpt-5-nano",
            messages=[
                {"role": "system", "content": SYS_MESSAGE},
                {"role": "user", "content": USER_MESSAGE}
            ]
        )
        if len(response.choices) == 0:
            print("Failed to get job offer. ---SKIPPING---: ", response.text)
            return ""
    except Exception as e:
        print("Failed to rebuild default cv: ---SKIPPING---", str(e))
        return ""

    return response.choices[0].message.content

def find_hr_emails(company_domain):
    response = requests.get(f"https://api.hunter.io/v2/domain-search?domain={company_domain}&api_key={HUNTER_API_KEY}")
    if response.status_code >= 400:
        print("Failed to get HR emails: ", response.text)
        return []

    results = []
    emails_data = response.json()['data']['emails']
    for data in emails_data:
        position = (data.get("position") or "").lower()
        if any(keyword in position for keyword in HR_KEYWORDS):
            full_name = f'{data.get("first_name", "")} {data.get("last_name", "")}'.strip()
            results.append({
                "full_name": full_name,
                "email": data["value"],
            })

    return results

def filter_out_used_hrs(hr_emails):
    context = ssl._create_unverified_context()
    imap_conn = IMAPClient('imap.gmail.com', use_uid=True, ssl=True, ssl_context=context)
    imap_conn.login(os.getenv("SENDER_EMAIL"), os.getenv("SENDER_PASSWORD"))

    imap_conn.select_folder("[Gmail]/Sent Mail", readonly=True)
    uids = imap_conn.search(["ALL"])
    fetch_data = imap_conn.fetch(uids, ['BODY.PEEK[HEADER.FIELDS (TO CC FROM SUBJECT DATE)]'])

    emails = []
    for uids, data in fetch_data.items():
        raw_header = data.get(b'BODY[HEADER.FIELDS (TO CC FROM SUBJECT DATE)]')
        if not raw_header:
            continue
        msg = email.message_from_bytes(raw_header, policy=policy.default)
        emails.append(msg.get("To"))

    input_emails = set(map(lambda em: em["email"], hr_emails))
    diff = input_emails.difference(emails)

    filtered = [e for e in hr_emails if e.get("email", "").lower() not in diff]

    return filtered


def clean_markdown(md_text):
    md_text = md_text.replace('```', '')

    md_text = re.sub(r'^(#{1,6})\s*(.+)$', r'\1 \2\n', md_text, flags=re.MULTILINE)

    md_text = re.sub(r'\n{3,}', '\n\n', md_text)

    md_text = re.sub(r'\[([^\]]+)\]\s*\(([^\)]+)\)', r'[\1](\2)', md_text)

    return md_text.strip()


def markdown_to_pdf(markdown_content, output_path):
    """
    Convert markdown to PDF using markdown2 + xhtml2pdf
    Returns True if successful, False otherwise
    """
    try:
        # Clean the markdown first
        cleaned_md = clean_markdown(markdown_content)

        # Convert markdown to HTML
        html_content = markdown2.markdown(
            cleaned_md,
            extras=[
                'fenced-code-blocks',
                'tables',
                'break-on-newline',
                'cuddled-lists',
                'header-ids'
            ]
        )

        css_path = os.path.join(os.path.dirname(__file__), 'cv_styles.css')
        try:
            with open(css_path, 'r', encoding='utf-8') as css_file:
                css_content = css_file.read()
        except FileNotFoundError:
            print(f"Error: CSS file not found at {css_path}")
            sys.exit(1)

        full_html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                {css_content}
            </style>
        </head>
            <body>
                {html_content}
            </body>
        </html>
        """

        # Convert HTML to PDF
        with open(output_path, "wb") as pdf_file:
            pisa_status = pisa.CreatePDF(
                full_html.encode('utf-8'),
                dest=pdf_file,
                encoding='utf-8'
            )

        return not pisa_status.err

    except Exception as e:
        print(f"PDF conversion failed: {e}")
        return False


def send_email_with_cv(job_title, company_name, updated_cv_md, receiver_data):
    sender_email = os.getenv("SENDER_EMAIL")
    sender_password = os.getenv("SENDER_PASSWORD")

    if not sender_email or not sender_password:
        raise ValueError("Missing email credentials. Set SENDER_EMAIL and SENDER_PASSWORD env vars.")

    # Create PDF from markdown
    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp_pdf:
        pdf_path = tmp_pdf.name

    success = markdown_to_pdf(updated_cv_md, pdf_path)

    if not success:
        print(f"Failed to generate PDF for {company_name} - {job_title}")
        if os.path.exists(pdf_path):
            os.unlink(pdf_path)
        return

    msg = MIMEMultipart()
    msg["From"] = sender_email
    msg["To"] = receiver_data["email"]
    msg["Subject"] = f"Application for {job_title} at {company_name}"

    body = f"""Dear {receiver_data['full_name']},

I hope this message finds you well. I am very interested in the {job_title} position at {company_name} and believe my background and skills could be a strong fit for your team.

I would greatly appreciate the opportunity to contribute and grow within your organization.

Please find my resume attached for your review.

Best regards,  
Dmytro"""

    msg.attach(MIMEText(body, "plain"))

    try:
        with open(pdf_path, "rb") as attachment:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(attachment.read())

        encoders.encode_base64(part)

        safe_company = re.sub(r'[^\w\s-]', '', company_name).strip().replace(' ', '_')
        filename = f"Resume_{safe_company}.pdf"

        part.add_header(
            "Content-Disposition",
            f"attachment; filename= {filename}",
        )
        msg.attach(part)

    finally:
        if os.path.exists(pdf_path):
            os.unlink(pdf_path)

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(sender_email, sender_password)
            server.send_message(msg)

        print(f"CV sent to {receiver_data['email']} ({company_name})")
    except Exception as e:
        print(f"Failed to send email to {receiver_data['email']}: {e}")

def main():
    default_cv_content = get_default_cv()
    job_offers = load_job_offers()

    for offer in job_offers:
        if not is_offer_suitable(offer, default_cv_content) or not offer.get("companyWebsite"):
            continue
        hr_emails = find_hr_emails(offer.get('companyWebsite'))
        filtered_hr_emails = filter_out_used_hrs(hr_emails)
        if filtered_hr_emails:
            updated_cv = rebuild_default_cv(offer, default_cv_content)
            for email in filtered_hr_emails:
                send_email_with_cv(offer.get('title'), offer.get('companyName'), updated_cv, email)
                break

main()
