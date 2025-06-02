import os
import json
import sys  # для sys.exit
from datetime import datetime
from flask import Flask, request, jsonify
import openai
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv
import httpx
import re  # для очищення рядків із новими рядками
import requests

load_dotenv()

# --- Google Credentials Setup (ENV або локальний файл) ---
# Якщо є змінна GOOGLE_CREDENTIALS_JSON, беремо її. Інакше перевіряємо, чи лежить credentials.json на диску.
google_credentials_json_content = os.getenv("GOOGLE_CREDENTIALS_JSON")
creds_data = None

# --- DEBUG: Print raw ENV content (masked) ---
# Masking more aggressively as full raw content might be very large
print(f"Raw GOOGLE_CREDENTIALS_JSON from ENV (first 200 chars): {google_credentials_json_content[:200] + '...' if google_credentials_json_content else 'Not set'}")
# --- END DEBUG ---

if google_credentials_json_content:
    try:
        # Більш агресивне очищення та нормалізація перед парсингом
        # Замінюємо можливі \r\n на просто \n
        cleaned_json_content = google_credentials_json_content.replace("\r\n", "\n")
        # Замінюємо екрановані \\n на \n (якщо ENV був закодований таким чином)
        cleaned_json_content = cleaned_json_content.replace("\\n", "\n")
        # Видаляємо всі керуючі символи, окрім дозволених JSONом (	, 
, 
)
        # JSON дозволяє , 	, 
, , 
        # Видаляємо символи з діапазону [\x00-\x1F] (контрольні символи ASCII), крім , 	, 
, , 
        # та символ DEL ()
        cleaned_json_content = re.sub(r'[\x00-\x07\x0B\x0E-\x1F\x7F]', '', cleaned_json_content)

        # --- DEBUG: Print cleaned content before parse (masked) ---
        print(f"Cleaned GOOGLE_CREDENTIALS_JSON before parse (first 200 chars): {cleaned_json_content[:200] + '...' if cleaned_json_content else 'Empty'}")
        print(f"Length of cleaned string: {len(cleaned_json_content) if cleaned_json_content else 0}")
        # --- END DEBUG ---

        creds_data = json.loads(cleaned_json_content)
        print("Google credentials JSON успішно розібрано із змінної середовища.")
    except json.JSONDecodeError as e:
        print(f"Fatal Error: Не вдалося розпарсити GOOGLE_CREDENTIALS_JSON як JSON: {e}")
        # Також надрукуємо частину проблемного рядка, якщо це можливо
        if hasattr(e, 'pos') and cleaned_json_content:
            start = max(0, e.pos - 50)
            end = min(len(cleaned_json_content), e.pos + 50)
            problem_snippet = cleaned_json_content[start:end]
            print(f"Problematic snippet around error position ({e.pos}): '{problem_snippet}'")
        print("Перевірте, що змінна містить валідний JSON.")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error while processing GOOGLE_CREDENTIALS_JSON: {e}")
        sys.exit(1)

elif os.path.exists("credentials.json"):
    try:
        creds_data = json.load(open("credentials.json", "r", encoding="utf-8"))
        print("Google credentials JSON завантажено з локального credentials.json.")
    except Exception as e:
        print(f"Fatal Error: Не вдалося прочитати локальний credentials.json: {e}")
        sys.exit(1)

else:
    print("Error: Не знайдено облікових даних Google. Встановіть GOOGLE_CREDENTIALS_JSON або додайте credentials.json.")
    sys.exit(1)

# Авторизація в Google Sheets
SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
try:
    # Спроба авторизації прямим методом через dict
    gc = gspread.service_account_from_dict(creds_data, scopes=SCOPE)
    print("Google Sheets авторизовано з JSON-даних.")
except Exception:
    # Цей блок, ймовірно, не буде досягнуто на Render, оскільки ми виходимо раніше
    # якщо ENV змінна не парситься, або якщо її немає і немає локального файлу.
    # Але залишаємо його для повноти, якщо код використовується локально без ENV.
    try:
        # Якщо раптом версія gspread не підтримує service_account_from_dict,
        # спробуємо звичайний метод, якщо credentials.json на диску
        # Потрібно переконатись, що credentials.json існує, якщо ми тут
        if os.path.exists("credentials.json"):
            gc = gspread.service_account()
            print("Google Sheets авторизовано через локальний credentials.json.")
        else:
            # Якщо service_account_from_dict не спрацював і локального файлу немає
            print("Fatal Error: gspread.service_account_from_dict failed and credentials.json not found for fallback.")
            sys.exit(1)

    except Exception as e:
        print(f"Fatal Error: Не вдалося авторизувати Google Sheets: {e}")
        sys.exit(1)

try:
    sheet = gc.open_by_key(os.getenv("SHEET_ID")).sheet1
    print(f"Успішно відкрито Google Sheet з ID: {os.getenv('SHEET_ID')}")
except Exception as e:
    print(f"Fatal Error: Не вдалося відкрити Google Sheet з ID {os.getenv('SHEET_ID', 'Not set')}: {e}")
    print("Перевірте, чи SHEET_ID правильний і чи сервіс-акаунт має доступ.")
    sys.exit(1)
# --- Кінець секції авторизації Google Sheets ---

# Діагностичний вивід
print("Environment variables:")
print(f"OPENAI_API_KEY: {'*' * len(os.getenv('OPENAI_API_KEY', '')) if os.getenv('OPENAI_API_KEY') else 'Not set'}")
print(f"SHEET_ID: {os.getenv('SHEET_ID', 'Not set')}")
print(f"Google Credentials: {'set' if creds_data else 'not set'}")

# OpenAI конфігурація
http_client = httpx.Client(trust_env=False)
client = openai.OpenAI(http_client=http_client)
openai.api_key = os.getenv("OPENAI_API_KEY")

# Ініціалізація Flask
app = Flask(__name__)

def process_transaction(text: str) -> dict:
    print(f"Received text for processing: {text}")

    # 1) Визначаємо тип транзакції: expense чи income
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
            max_tokens=10
        )
        transaction_type = response_classify.choices[0].message.content.strip().lower()
        if transaction_type not in ['expense', 'income']:
            print(f"Warning: GPT returned unexpected transaction type: {transaction_type}. Defaulting to expense.")
            transaction_type = 'expense'
        print(f"Classified as: {transaction_type}")
    except Exception as e:
        print(f"Error classifying transaction type: {e}. Defaulting to expense.")
        transaction_type = 'expense'

    # 2) Формуємо prompt для деталей
    if transaction_type == 'expense':
        prompt_details = (
            "Ви — інтелектуальний помічник для парсингу фінансових витрат українською. "
            "Поверніть ЛИШЕ JSON (без зайвого тексту).\n\n"
            "Очікуваний JSON з полями:\n"
            "  {\n"
            "    \"amount\": <число (int або float)>,               \n"
            "    \"currency\": \"<трьохлітерний_код_валюти>\",      \n"
            "    \"category\": \"...\",                             \n"
            "    \"description\": \"<короткий опис>\"               \n"
            "  }\n\n"
            "Поле \"category\" може приймати одне з: \"Ресторан\", \"доп їжа\", \"транспорт\", \"покупки\", \"розваги\", \"інше\", \"їжа\".\n"
            "Правила (черговість перевірки):\n"
            "  – Слова «ресторан», «кафе», «столова» → «Ресторан».\n"
            "  – Слова «кава», «хот-дог», «Жабка», «печиво», «чай\" → «доп їжа».\n"
            "  – «продукти», «супермаркет», «магазин\" → «їжа».\n"
            "  – «таксі», «Uber», «Bolt», «метро», «автобус\" → «транспорт».\n"
            "  – «купив», «ремонт», «квитки\" → «покупки».\n"
            "  – «кіно», «театр», «концерт\", «відеогра\", «бар\" → «розваги».\n"
            "  – Інакше → «інше».\n\n"
            "Поле \"description\" — короткий опис: «обід у кафе», «хот-дог біля офісу» і т.д.\n\n"
            f"Phrase: '{text}'"
        )
    else:
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
            response_format={"type": "json_object"}
        )
        content_details = response_details.choices[0].message.content.strip()
        print(f"Raw response from GPT details call: {content_details}")

        try:
            details_data = json.loads(content_details)
            print(f"Parsed details data: {details_data}")
        except json.JSONDecodeError:
            print(f"JSON Decode Error on GPT details call: {content_details}")
            return {
                "type": transaction_type,
                "amount": 0,
                "currency": "",
                "category": "Error",
                "description": f"Parsing Error: {content_details}",
                "error": f"Invalid JSON from GPT: {content_details}"
            }

        amount = details_data.get("amount")
        original_currency = details_data.get("currency", "PLN").upper()
        if transaction_type == 'expense':
            category_or_source = details_data.get("category")
            description = details_data.get("description")
        else:
            category_or_source = details_data.get("source")
            description = None

        print(f"Extracted – Amount: {amount}, Currency: {original_currency}, Category/Source: {category_or_source}, Description: {description}")

        converted_amount = amount
        final_currency = original_currency

        if amount is None or not isinstance(amount, (int, float)) or float(amount) <= 0:
            print(f"Warning: Amount invalid ({amount}). Пропускаємо конвертацію.")
        elif final_currency != "PLN":
            print(f"Запит курсу з {final_currency} → PLN через ExchangeRate-API…")
            api_key = os.getenv("EXCHANGERATE_API_KEY")
            if not api_key:
                print("Error: EXCHANGERATE_API_KEY не встановлено. Пропускаємо конвертацію.")
            else:
                api_url = f"https://v6.exchangerate-api.com/v6/{api_key}/latest/{final_currency}"
                try:
                    response = requests.get(api_url)
                    response.raise_for_status()
                    data = response.json()
                    if data.get("result") == "success":
                        rates = data.get("conversion_rates", {})
                        if "PLN" in rates:
                            rate = rates["PLN"]
                            print(f"Курс {final_currency}→PLN: {rate}")
                            if rate and rate > 0:
                                converted_amount = round(float(amount) * rate, 2)
                                final_currency = "PLN"
                                print(f"Успішно конвертовано: {converted_amount} PLN")
                            else:
                                print(f"Warning: Невірний курс ({rate}). Повертаємо оригінал.")
                        else:
                            print(f"Error: PLN відсутній у {list(rates.keys())}.")
                    else:
                        print(f"Error from ExchangeRate-API: {data.get('error-type', 'Unknown error')} .")
                except requests.exceptions.RequestException as e:
                    print(f"Network/API error: {e}.")
                except Exception as e:
                    print(f"Unexpected error при обробці відповіді ExchangeRate-API: {e}.")
        else:
            print("Валюта вже PLN, конвертація не потрібна.")

        result = {
            "type": transaction_type,
            "amount": converted_amount,
            "currency": final_currency,
            "category": category_or_source if transaction_type == 'expense' else None,
            "source": category_or_source if transaction_type == 'income' else None,
            "description": description
        }
        print(f"Final processed transaction data: {result}")
        return result

    except Exception as e:
        print(f"Error during transaction processing: {e}")
        return {
            "type": "error",
            "amount": 0,
            "currency": "",
            "category": "Error",
            "source": "Error",
            "description": f"Processing Error: {e}",
            "error": str(e)
        }


@app.route("/api/expense", methods=["POST"])
def handle_transaction():
    data = request.get_json(force=True)
    text = data.get("text", "")
    if not text:
        return jsonify({"error": "Empty text"}), 400

    processed_data = process_transaction(text)
    if processed_data.get("type") == "error":
        return jsonify({"error": processed_data.get("error", "Processing error")}), 500

    transaction_type = processed_data.get("type", "expense")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    amount = processed_data.get("amount")
    final_currency = processed_data.get("currency", "PLN")
    category = processed_data.get("category")
    source = processed_data.get("source")
    description = processed_data.get("description")

    if transaction_type == 'expense':
        row_to_append = [timestamp, amount, final_currency, category, description]
        print(f"Appending expense row (A-E): {row_to_append}")
    else:
        income_date = timestamp
        income_amount = amount
        income_source = source
        row_to_append = [''] * 6 + [income_date, income_amount, income_source]
        print(f"Appending income row (G-I): {row_to_append}")

    try:
        if transaction_type == 'expense':
            table_range = 'A1'
            values_to_append = row_to_append
            print(f"Attempting to append expense row to {table_range}: {values_to_append}")
        else:
            table_range = 'G1'
            values_to_append = [income_date, income_amount, income_source]
            print(f"Attempting to append income row to {table_range}: {values_to_append}")

        sheet.append_row(values_to_append, value_input_option='USER_ENTERED', table_range=table_range)
        print("Row successfully appended to Google Sheet.")
    except Exception as e:
        print(f"Error appending row to Google Sheet: {e}")
        return jsonify({"error": f"Failed to write to Google Sheet: {e}"}), 500

    return jsonify({
        "status": "ok",
        "transaction_type": transaction_type,
        "row": row_to_append,
        "message": f"Successfully added {transaction_type}: {text}"
    })


@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Environment PORT: {os.environ.get('PORT')}, Using port: {port}")
    app.run(host="0.0.0.0", port=port) 