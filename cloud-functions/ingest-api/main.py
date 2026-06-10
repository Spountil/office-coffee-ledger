import functions_framework
import json
import os
import gspread
import datetime
import smtplib
from email.message import EmailMessage
from fpdf import FPDF
from google.cloud import secretmanager
from google.oauth2.service_account import Credentials

PROJECT_ID = "your-project-id-here" # REPLACE THIS

def get_secret(secret_id):
    """Fetches a secret payload from GCP Secret Manager."""
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("UTF-8")

@functions_framework.http
def ingest_coffee(request):
    # 1. Handle CORS (Crucial for GitHub Pages to talk to GCP)
    if request.method == 'OPTIONS':
        headers = {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'POST',
            'Access-Control-Allow-Headers': 'Content-Type',
            'Access-Control-Max-Age': '3600'
        }
        return ('', 204, headers)

    headers = {'Access-Control-Allow-Origin': '*'}

    try:
        # Parse incoming JSON from the phone
        request_json = request.get_json(silent=True)
        email = request_json['email']
        first_name = request_json['firstName']
        last_name = request_json['lastName']
        coffee_type = request_json['coffeeType']
        price = int(request_json['price'])
        timestamp = datetime.datetime.now().isoformat()

        # 2. Authenticate with Google Sheets via Secret Manager
        sa_info = json.loads(get_secret("GCP_SERVICE_ACCOUNT"))
        creds = Credentials.from_service_account_info(
            sa_info, 
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        gc = gspread.authorize(creds)

        # 3. Write to Database
        # Using the exact name of the file in your Google Drive
        sh = gc.open("Office_Coffee_Ledger") 
        consumption_sheet = sh.worksheet("Consumption")
        consumption_sheet.append_row([timestamp, email, first_name, last_name, coffee_type, price])

        # 4. Read Live Balance
        balance_sheet = sh.worksheet("Live_Balances")
        records = balance_sheet.get_all_records()
        current_balance = price # Fallback if user is completely new
        
        for row in records:
            if str(row.get('Email')).strip().lower() == email.strip().lower():
                # gspread pulls formulas as their evaluated values
                current_balance = row.get('Outstanding Balance', price)
                break

        # 5. Generate PDF Receipt in Memory
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("helvetica", size=12)
        pdf.cell(0, 10, txt="Office Coffee Cartel - Receipt", new_x="LMARGIN", new_y="NEXT", align='C')
        pdf.cell(0, 10, txt=f"Date: {timestamp[:10]}", new_x="LMARGIN", new_y="NEXT")
        pdf.cell(0, 10, txt=f"Item: {coffee_type} ({price} XPF)", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("helvetica", style="B", size=12)
        pdf.cell(0, 10, txt=f"New Outstanding Balance: {current_balance} XPF", new_x="LMARGIN", new_y="NEXT")
        
        # Output PDF to a byte array
        pdf_bytes = bytes(pdf.output())

        # 6. Send Email via Native smtplib
        sender_email = os.environ.get("SENDER_EMAIL")
        app_password = get_secret("GMAIL_APP_PASSWORD")

        msg = EmailMessage()
        msg['Subject'] = f'Coffee Receipt: {coffee_type}'
        msg['From'] = sender_email
        msg['To'] = email
        msg.set_content(f"Hey {first_name},\n\nEnjoy the {coffee_type}! Your updated balance is {current_balance} XPF.\n\nAttached is your receipt.")
        
        msg.add_attachment(pdf_bytes, maintype='application', subtype='pdf', filename=f'receipt_{timestamp[:10]}.pdf')

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(sender_email, app_password)
            smtp.send_message(msg)

        # 7. Return Success to Frontend
        return (json.dumps({"status": "success", "balance": current_balance}), 200, headers)

    except Exception as e:
        print(f"Error: {str(e)}")
        return (json.dumps({"error": "Internal Server Error"}), 500, headers)