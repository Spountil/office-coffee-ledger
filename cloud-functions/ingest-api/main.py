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

# Extract environment variables
PROJECT_ID = os.environ.get("PROJECT_ID")

if not PROJECT_ID:
    raise ValueError("Missing PROJECT_ID environment variable.")

def get_secret(secret_id):
    """Fetches a secret payload from GCP Secret Manager."""
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("UTF-8")

@functions_framework.http
def ingest_coffee(request):
    # 1. CORS Preflight
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
        # 2. Extract Edge Payload
        request_json = request.get_json(silent=True)
        email = request_json['email'].strip().lower()
        first_name = request_json['firstName']
        last_name = request_json['lastName']
        coffee_type = request_json['coffeeType']
        price = int(request_json['price'])
        timestamp = datetime.datetime.now().isoformat()

        # 3. IAM Authentication
        sa_info = json.loads(get_secret("GCP_SERVICE_ACCOUNT"))
        creds = Credentials.from_service_account_info(
            sa_info, 
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        gc = gspread.authorize(creds)

        # 4. Database Write (Append Event)
        sh = gc.open("Office_Coffee_Ledger") 
        consumption_sheet = sh.worksheet("Consumption")
        consumption_sheet.append_row([timestamp, email, first_name, last_name, coffee_type, price])

        # 5. Database Read (Fetch State for FIFO)
        consumption_records = consumption_sheet.get_all_records()
        payments_records = sh.worksheet("Payments").get_all_records()

        # 6. The FIFO Reconciliation Algorithm
        # Calculate total historical payments for this user
        user_payments = [
            int(p['Amount Paid']) for p in payments_records 
            if str(p.get('Email', '')).strip().lower() == email and str(p.get('Amount Paid', '')).isdigit()
        ]
        total_paid_pool = sum(user_payments)

        # Isolate chronological consumption for this user
        user_coffees = [
            c for c in consumption_records 
            if str(c.get('Email', '')).strip().lower() == email
        ]

        unpaid_coffees = []
        for c in user_coffees:
            try:
                c_price = int(c.get('Price', 0))
            except ValueError:
                continue
                
            if total_paid_pool >= c_price:
                # Coffee is fully paid, drain the pool
                total_paid_pool -= c_price
            elif total_paid_pool > 0:
                # Coffee is partially paid
                owed = c_price - total_paid_pool
                unpaid_coffees.append({
                    'Date': str(c.get('Timestamp', ''))[:10], 
                    'Type': c.get('Coffee Type', 'Coffee'), 
                    'Owed': owed
                })
                total_paid_pool = 0
            else:
                # Coffee is completely unpaid
                unpaid_coffees.append({
                    'Date': str(c.get('Timestamp', ''))[:10], 
                    'Type': c.get('Coffee Type', 'Coffee'), 
                    'Owed': c_price
                })
                
        current_balance = sum(item['Owed'] for item in unpaid_coffees)

        # 7. Dynamic PDF Generation
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("helvetica", style="B", size=16)
        pdf.cell(0, 10, txt="Office Coffee Cartel", new_x="LMARGIN", new_y="NEXT", align='C')
        
        pdf.set_font("helvetica", size=12)
        pdf.cell(0, 10, txt=f"Date: {timestamp[:10]}", new_x="LMARGIN", new_y="NEXT")
        pdf.cell(0, 10, txt=f"Transaction: {coffee_type} ({price} XPF) - LOGGED", new_x="LMARGIN", new_y="NEXT")
        
        pdf.ln(5)
        pdf.set_font("helvetica", style="B", size=12)
        pdf.cell(0, 10, txt="--- Itemized Unpaid Tab ---", new_x="LMARGIN", new_y="NEXT")
        
        pdf.set_font("helvetica", size=10)
        if not unpaid_coffees:
             pdf.cell(0, 8, txt="Account fully pre-paid. You have a positive balance!", new_x="LMARGIN", new_y="NEXT")
        else:
            for item in unpaid_coffees:
                pdf.cell(0, 8, txt=f"{item['Date']} | {item['Type']} | Owed: {item['Owed']} XPF", new_x="LMARGIN", new_y="NEXT")

        pdf.ln(5)
        pdf.set_font("helvetica", style="B", size=12)
        pdf.cell(0, 10, txt=f"Total Outstanding Balance: {current_balance} XPF", new_x="LMARGIN", new_y="NEXT")
        
        pdf_bytes = bytes(pdf.output())

        # 8. SMTP Dispatch
        sender_email = os.environ.get("SENDER_EMAIL")
        app_password = get_secret("GMAIL_APP_PASSWORD")

        msg = EmailMessage()
        msg['Subject'] = f'Coffee Receipt: {coffee_type}'
        msg['From'] = sender_email
        msg['To'] = email
        msg.set_content(f"Hey {first_name},\n\nEnjoy the {coffee_type}! Your updated itemized balance is {current_balance} XPF.\n\nAttached is your dynamic receipt.")
        
        msg.add_attachment(pdf_bytes, maintype='application', subtype='pdf', filename=f'receipt_{timestamp[:10]}.pdf')

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(sender_email, app_password)
            smtp.send_message(msg)

        # 9. Return JSON to Edge
        return (json.dumps({"status": "success", "balance": current_balance}), 200, headers)

    except Exception as e:
        print(f"Pipeline Error: {str(e)}")
        return (json.dumps({"error": "Internal Server Error"}), 500, headers)