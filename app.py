import os
import json
from datetime import datetime
from flask import Flask, request, jsonify
import openai
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv
import httpx
import re # Import the regular expression module
import requests # Add import for making HTTP requests
# from forex_python.converter import CurrencyRates # Removed
# from forex_python.exceptions import RateNotFoundError # Keep this commented for now
# from currency_converter import CurrencyConverter # Removed
# from currency_converter.errors import CurrencyDoesNotExist, MissingRateError # Removed

load_dotenv()

# --- Google Credentials Setup ---
# This section handles credentials.json for deployment environments
# On platforms like Render, we pass the JSON content via an environment variable
# and write it to a file during startup.
google_credentials_json_content = os.getenv("GOOGLE_CREDENTIALS_JSON")
credentials_file_path = "credentials.json"

if google_credentials_json_content:
    print(f"GOOGLE_CREDENTIALS_JSON environment variable found. Writing to {credentials_file_path}")
    try:
        with open(credentials_file_path, "w") as f:
            json.dump(json.loads(google_credentials_json_content), f, indent=None) # Use indent=None to save space
        print(f"{credentials_file_path} created successfully.")
    except Exception as e:
        print(f"Error writing {credentials_file_path}: {e}")
        # Depending on strictness, you might want to sys.exit(1) here
else:
    print("GOOGLE_CREDENTIALS_JSON not found. Assuming credentials.json exists locally or using other method.")
# --- End Google Credentials Setup ---

# Debug prints
print("Environment variables:")
print(f"OPENAI_API_KEY: {'*' * len(os.getenv('OPENAI_API_KEY', '')) if os.getenv('OPENAI_API_KEY') else 'Not set'}")
print(f"SHEET_ID: {os.getenv('SHEET_ID', 'Not set')}")

# OpenAI Configuration
# Initialize the OpenAI client with the new API interface
# Explicitly disable trusting environment variables for proxies
http_client = httpx.Client(trust_env=False)
client = openai.OpenAI(http_client=http_client)
openai.api_key = os.getenv("OPENAI_API_KEY") # Keep for backwards compatibility if needed, though client handles it

# Google Sheets Configuration
SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
creds = ServiceAccountCredentials.from_json_keyfile_name(
    "credentials.json", SCOPE
)
gc = gspread.authorize(creds)
sheet = gc.open_by_key(os.getenv("SHEET_ID")).sheet1

# Flask Initialization
app = Flask(__name__)

def process_transaction(text: str) -> dict:
    """
    Uses OpenAI ChatCompletion to first classify text as expense or income,
    then parses details (amount, currency, category/source, description).
    Converts amount to PLN if needed using ExchangeRate-API.
    Returns a dictionary including transaction type and parsed/converted data.
    """
    print(f"Received text for processing: {text}")

    # Step 1: Classify transaction type (expense or income)
    prompt_classify = (
        "Analyze the following phrase and determine if it describes an 'expense' or 'income'. "
        "Respond with ONLY the word 'expense' or 'income'. "
        f"Phrase: '{text}'"
    )
    try:
        response_classify = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt_classify}],
            temperature=0,
            max_tokens=10 # Keep response short
        )
        transaction_type = response_classify.choices[0].message.content.strip().lower()
        if transaction_type not in ['expense', 'income']:
            print(f"Warning: GPT returned unexpected transaction type: {transaction_type}. Defaulting to expense.")
            transaction_type = 'expense' # Default if classification fails
        print(f"Classified as: {transaction_type}")

    except Exception as e:
        print(f"Error classifying transaction type: {e}. Defaulting to expense.")
        transaction_type = 'expense' # Default on error

    # Step 2: Parse transaction details based on type
    if transaction_type == 'expense':
        # Updated prompt for expense details with strict categories including 'їжа'
        prompt_details = (
            "Ви — інтелектуальний помічник для парсингу фінансових витрат українською. "
            "Поверніть ЛИШЕ JSON (без зайвого тексту).\n\n"
            "Очікуваний JSON має містити поля:\n"
            "  {\n"
            "    \"amount\": <число (int або float)>,               \n"
            "    \"currency\": \"<трьохлітерний_код_валюти>\",      \n"
            "    \"category\": \"...\",                             \n"
            "    \"description\": \"<короткий опис>\"               \n"
            "  }\n\n"
            "Поле \"category\" може приймати одне з наступних значень:\n"
            "  1) \"Ресторан\"\n"
            "  2) \"доп їжа\"\n"
            "  3) \"транспорт\"\n"
            "  4) \"покупки\"\n"
            "  5) \"розваги\"\n"
            "  6) \"інше\"\n"
            "  7) \"їжа\"\n\n" # Added 'їжа' category
            "Правила присвоєння (вибирайте ПЕРШЕ правило, яке відповідає фразі):\n" # Clarified priority
            "  – Якщо фраза містить слова «ресторан», «кафе», «столова» (будь-яке з них) → категорія: \"Ресторан\".\n"
            "  – Інакше, якщо фраза містить слова, що позначають дрібний перекус чи «хотілку»: «кава», «хот-дог», «Жабка», «печиво\", «чай\" тощо → категорія: \"доп їжа\".\n"
            "  – Інакше, якщо фраза стосується загальних покупок продуктів, закупівлі в супермаркетах/магазинах, або щомісячних витрат на харчування: «продукти», «закупка», «супермаркет», «магазин», «щомісячні витрати на їжу», «бідронка» тощо → категорія: \"їжа\".\n" # Added rule for 'їжа'
            "  – Інакше, якщо фраза стосується пересування: «таксі», «Uber», «Bolt», «метро», «автобус» тощо → категорія: \"транспорт\".\n"
            "  – Інакше, якщо фраза про придбання речей або послуг у магазинах (окрім харчових): «купив футболку», «ремонт техніки», «квитки», «пакет» тощо → категорія: \"покупки\".\n"
            "  – Інакше, якщо фраза стосується розваг: «кіно», «театр», «концерт», «відеогра», «бар з друзями\" тощо → категорія: \"розваги\".\n"
            "  – В усіх інших випадках → категорія: \"інше\".\n\n"
            "Поле \"description\" має бути коротким поясненням суті витрати (наприклад, «обід у кафе», «хот-дог біля офісу», «квиток у кіно» чи «ремонт друкарки»).  \n\n"
            f"Фраза: '{text}'"
        )
    else: # income
        prompt_details = (
            "You are an assistant for parsing financial income. "
            "You receive a phrase in Ukrainian. For example: 'I earned 500 dollars from freelancing'. "
            "Return ONLY JSON with fields: amount (integer or float), currency (string, currency code, e.g. USD, UAH, EUR, PLN), "
            "source (string, source of income, e.g. salary, freelancing, gift). "
            f"Phrase: '{text}'"
        )

    try:
        response_details = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt_details}],
            temperature=0,
            response_format={ "type": "json_object" }
        )
        content_details = response_details.choices[0].message.content.strip()
        print(f"Raw response from GPT details call: {content_details}")

        try:
            details_data = json.loads(content_details)
            print(f"Parsed details data: {details_data}")
        except json.JSONDecodeError:
            print(f"JSON Decode Error on GPT details call: {content_details}")
            # If parsing fails, return minimal data and indicate error
            return {
                "type": transaction_type, # Return classified type even if parsing failed
                "amount": 0,
                "currency": "",
                "category": "Error", # Or Source: "Error"
                "description": f"Parsing Error: {content_details}", # Use description for error details
                "error": f"Invalid JSON from GPT: {content_details}"
            }

        amount = details_data.get("amount")
        original_currency = details_data.get("currency", "PLN").upper() # Default to PLN and make uppercase

        # Get category or source based on type
        if transaction_type == 'expense':
             category_or_source = details_data.get("category")
             description = details_data.get("description")
        else: # income
             category_or_source = details_data.get("source")
             description = None # No description for income in prompt/sheet

        print(f"Extracted - Amount: {amount}, Original Currency: {original_currency}, Category/Source: {category_or_source}, Description: {description}")

        # Step 3: Convert amount to PLN using ExchangeRate-API if needed
        converted_amount = amount # Start with original amount
        final_currency = original_currency # Start with original currency

        if amount is None or not isinstance(amount, (int, float)) or float(amount) <= 0:
             print(f"Warning: Amount not found, invalid, or non-positive ({amount}). Skipping currency conversion.")
             # converted_amount and final_currency remain original or default
        elif final_currency != "PLN":
            print(f"Attempting to get exchange rate for {final_currency} to PLN using ExchangeRate-API...")
            api_key = os.getenv("EXCHANGERATE_API_KEY")
            if not api_key:
                print("Error: EXCHANGERATE_API_KEY not set in .env file. Cannot perform currency conversion.")
                # Keep original amount and currency
            else:
                # Construct the API URL
                api_url = f"https://v6.exchangerate-api.com/v6/{api_key}/latest/{final_currency}"
                try:
                    response = requests.get(api_url)
                    response.raise_for_status() # Raise an exception for bad status codes (4xx or 5xx)
                    data = response.json()

                    if data["result"] == "success":
                        rates = data["conversion_rates"]
                        if "PLN" in rates:
                            rate = rates["PLN"]
                            print(f"Rate from ExchangeRate-API ({final_currency} to PLN): {rate}")

                            if rate is not None and rate > 0:
                                 converted_amount = round(float(amount) * rate, 2) # Convert and round
                                 final_currency = "PLN" # Set currency to PLN after successful conversion
                                 print(f"Successfully converted to {converted_amount} PLN using ExchangeRate-API rate.")
                            else:
                                 print(f"Warning: Received non-positive or None rate {rate} from ExchangeRate-API for {final_currency}. Keeping original amount and currency.")
                        else:
                            print(f"Error: PLN not found in conversion rates from ExchangeRate-API for {final_currency}. Keeping original amount and currency.")
                            print(f"Available rates: {list(rates.keys())}") # Log available currencies for debugging

                    else:
                        # Use single quotes for the outer f-string
                        print(f'Error from ExchangeRate-API: {data.get("error-type", "Unknown error")}. Keeping original amount and currency.')
                        # Keep original amount and currency

                except requests.exceptions.RequestException as e:
                    print(f"Network or API request error getting exchange rate from ExchangeRate-API for {final_currency}: {e}. Keeping original amount and currency.")
                    # Keep original amount and currency
                except Exception as e: # Catch any other unexpected error during API call or processing
                    print(f"Unexpected error processing ExchangeRate-API response for {final_currency}: {e}. Keeping original amount and currency.")
                    # Keep original amount and currency
        else:
             print("Currency is already PLN, no conversion needed.")
             # converted_amount and final_currency remain as parsed

        # Prepare final result dictionary
        result = {
            "type": transaction_type,
            "amount": converted_amount,
            "currency": final_currency,
            # Include category for expense, source for income
            "category": category_or_source if transaction_type == 'expense' else None,
            "source": category_or_source if transaction_type == 'income' else None,
            "description": description # Only for expense
        }

        print(f"Final processed transaction data: {result}")
        return result

    except Exception as e:
        # This catches errors from the initial GPT calls themselves or other unexpected issues
        print(f"Error during transaction processing: {str(e)}")
        # Return error state
        return {
             "type": "error",
             "amount": 0,
             "currency": "",
             "category": "Error",
             "source": "Error",
             "description": f"Processing Error: {str(e)}",
             "error": str(e)
        }


@app.route("/api/expense", methods=["POST"])
def handle_transaction(): # Renamed endpoint function
    """
    Expects JSON: { "text": "<dictated text>" }
    Processes text as either expense or income and records in Google Sheet.
    """
    data = request.get_json(force=True)
    text = data.get("text", "")

    if not text:
        return jsonify({"error": "Empty text"}), 400

    # Process the text to get transaction details
    processed_data = process_transaction(text)

    # Check if processing resulted in a critical error
    if processed_data.get("type") == "error":
         return jsonify({"error": processed_data.get("error", "An unknown processing error occurred.")}), 500

    # Determine which columns to write to based on transaction type
    transaction_type = processed_data.get("type", "expense") # Default to expense if type is missing

    # Prepare data parts that are common or conditional
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    amount = processed_data.get("amount")
    final_currency = processed_data.get("currency", "PLN")
    category = processed_data.get("category") # Used for expense
    source = processed_data.get("source") # Used for income
    description = processed_data.get("description") # Used for expense

    # Construct the row based on transaction type and target columns
    if transaction_type == 'expense':
        # Data for columns A-E:
        # A: Дата витрати
        # B: Сума витрати (PLN)
        # C: Валюта (PLN)
        # D: Категорія
        # E: Додаткова інформація
        row_to_append = [
            timestamp,
            amount,
            final_currency, # Should be PLN after successful conversion
            category,
            description
        ]
        print(f"Appending expense row (A-E): {row_to_append}")
    else: # income
        # Data for columns G-I, with empty leading columns (A-F):
        # A-F: Пусто
        # G: Дата прибутку
        # H: Сума прибутку (PLN)
        # I: Джерело прибутку
        income_date = timestamp # Use the same timestamp for income date
        income_amount = amount
        income_source = source

        # Construct the row with empty leading columns
        row_to_append = [''] * 6 + [income_date, income_amount, income_source]
        print(f"Appending income row (G-I): {row_to_append}")

    # Add to Google Sheet using append_row with table_range
    try:
        # Determine the table_range based on transaction type
        if transaction_type == 'expense':
            # Append to columns A-E, table starts at A1
            table_range = 'A1'
            values_to_append = row_to_append # The row_to_append already contains data for A-E (5 elements)
            print(f"Attempting to append expense row to table_range {table_range}: {values_to_append}")
        else: # income
            # Append to columns G-I, table starts at G1
            table_range = 'G1'
            # For income, row_to_append was [''] * 6 + [date, amount, source]
            # We only need the last 3 elements for G-I when using table_range='G1'
            values_to_append = [income_date, income_amount, income_source] # Prepare list for G-I (3 elements)
            print(f"Attempting to append income row to table_range {table_range}: {values_to_append}")

        # Use append_row method with table_range
        # The 'sheet' object is already a Worksheet object based on its initialization sheet = gc.open_by_key(...).sheet1
        sheet.append_row(
            values_to_append,
            value_input_option='USER_ENTERED',
            table_range=table_range
        )

        print("Row successfully appended to Google Sheet using append_row with table_range.")
    except Exception as e:
        print(f"Error appending row to Google Sheet: {e}")
        return jsonify({"error": f"Failed to write to Google Sheet: {str(e)}"}), 500


    return jsonify({
        "status": "ok",
        "transaction_type": transaction_type,
        "row": row_to_append,
        "message": f"Successfully added {transaction_type}: {text}"
    })

@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint"""
    return jsonify({"status": "healthy"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Environment PORT: {os.environ.get('PORT')}, Using port: {port}")
    app.run(host="0.0.0.0", port=port) 