import os
import sys
import json
from datetime import datetime
from flask import Flask, request, jsonify
import openai
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv
import httpx
import requests
import base64

# Завантажуємо локальні .env (якщо є)
load_dotenv()

# -----------------------------------------------------------------------------
# 1) Підготовка Google-сервісних облікових даних
# -----------------------------------------------------------------------------

# Зчитуємо закодовані в Base64 облікові дані з ENV
google_credentials_base64 = os.getenv("GOOGLE_CREDENTIALS_BASE64")

if google_credentials_base64:
    # Якщо дані є в ENV (Base64) – декодуємо та записуємо у файл credentials.json
    try:
        # Декодування Base64
        decoded_json_bytes = base64.b64decode(google_credentials_base64)
        decoded_json_content = decoded_json_bytes.decode('utf-8')

        # Перевірка, чи декодований вміст схожий на JSON
        try:
            json.loads(decoded_json_content) # Спроба розпарсити для валідації
            print("Base64 декодовано успішно, вміст схожий на валідний JSON.")
        except json.JSONDecodeError:
            print("Warning: Base64 декодовано, але вміст не є ідеальним JSON. Спроба зберегти як є.")
            # Продовжуємо запис навіть якщо не ідеальний JSON, можливо, ServiceAccountCredentials впорається

        # Пишемо у credentials.json
        with open("credentials.json", "w", encoding="utf-8") as f:
            f.write(decoded_json_content)
        print("Google credentials JSON записано у credentials.json з декодованого Base64 ENV.")
    except Exception as e:
        print(f"Fatal Error: не вдалося декодувати GOOGLE_CREDENTIALS_BASE64 або записати файл: {e}")
        sys.exit(1)

elif os.path.exists("credentials.json"):
    # Якщо ж локально є credentials.json і ENV не задано – просто повідомляємо
    print("Google credentials JSON завантажено з локального credentials.json.")
else:
    print("Error: не знайдено жодного способу отримати credentials.json.")
    print("Встановіть змінну окруження GOOGLE_CREDENTIALS_BASE64 або додайте локальний файл credentials.json у корінь проєкту.")
    sys.exit(1)

# Тепер, коли credentials.json гарантовано є (він або створений із ENV, або лежав локально),
# авторизуємося через oauth2client
SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
try:
    creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", SCOPE)
    gc = gspread.authorize(creds)
    print("Google Sheets авторизовано через credentials.json.")
except Exception as e:
    print(f"Fatal Error: не вдалося авторизувати Google Sheets: {e}")
    sys.exit(1)

# Далі відкриваємо потрібний лист
sheet_id = os.getenv("SHEET_ID")
if not sheet_id:
    print("Fatal Error: змінна SHEET_ID не встановлена.")
    sys.exit(1)

try:
    sheet = gc.open_by_key(sheet_id).sheet1
    print(f"Успішно відкрито Google Sheet з ID {sheet_id}.")
except Exception as e:
    print(f"Fatal Error: не вдалося відкрити Google Sheet з ID {sheet_id}: {e}")
    sys.exit(1)

# -----------------------------------------------------------------------------
# 2) Налаштування OpenAI
# -----------------------------------------------------------------------------

print("Environment variables:")
print(f"  OPENAI_API_KEY: {'*' * len(os.getenv('OPENAI_API_KEY', '')) if os.getenv('OPENAI_API_KEY') else 'Not set'}")
print(f"  SHEET_ID: {sheet_id}")
print(f"  Google Credentials file: {'exists' if os.path.exists('credentials.json') else 'not found'}")

http_client = httpx.Client(trust_env=False)
client = openai.OpenAI(http_client=http_client)
openai.api_key = os.getenv("OPENAI_API_KEY")

# -----------------------------------------------------------------------------
# 3) Ініціалізація Flask
# -----------------------------------------------------------------------------

app = Flask(__name__)

def process_transaction(text: str) -> dict:
    """
    Визначає "expense" чи "income", парсить JSON через GPT, конвертує валюту,
    і повертає словник із деталями транзакції.
    """
    print(f"Received text for processing: {text}")

    # 1. Класифікація (expense/income)
    prompt_classify = (
        "Analyze the following phrase and determine if it describes an 'expense' or 'income'.\n"
        "Respond with ONLY the word 'expense' or 'income'.\n\n"
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
            print(f"Warning: Unexpected type '{transaction_type}', defaulting to 'expense'.")
            transaction_type = 'expense'
        print(f"Classified as: {transaction_type}")
    except Exception as e:
        print(f"Error classifying transaction type: {e}. Defaulting to 'expense'.")
        transaction_type = 'expense'

    # 2. Парсинг деталей через GPT
    if transaction_type == 'expense':
        prompt_details = (
            "Ви — бот-помічник для українських фінансових витрат. "
            "Поверніть ЛИШЕ JSON за схемою:\n"
            "{\n"
            "  \"amount\": <число (int/float)>,\n"
            "  \"currency\": \"<код_валюти (наприклад, USD, UAH, EUR, PLN)>,\n"
            "  \"category\": \"<категорія>\",\n"
            "  \"description\": \"<короткий опис>\"\n"
            "}\n\n"
            "Категорії можуть бути: \"Ресторан\", \"доп їжа\", \"транспорт\", \"покупки\", \"розваги\", \"інше\", \"їжа\".\n"
            "Правила визначення (перевірити послідовно):\n"
            "  • \"ресторан\", \"кафе\", \"столова\" → \"Ресторан\"\n"
            "  • \"кава\", \"хот-дог\", \"Жабка\", \"печиво\", \"чай\" → \"доп їжа\"\n"
            "  • \"продукти\", \"супермаркет\", \"магазин\" → \"їжа\"\n"
            "  • \"таксі\", \"Uber\", \"Bolt\", \"метро\", \"автобус\" → \"транспорт\"\n"
            "  • \"купив\", \"ремонт\", \"квитки\" → \"покупки\"\n"
            "  • \"кіно\", \"театр\", \"концерт\", \"відеогра\", \"бар\" → \"розваги\"\n"
            "  • Інакше → \"інше\"\n\n"
            f"Phrase: '{text}'"
        )
    else:
        prompt_details = (
            "You are an assistant for parsing financial income in Ukrainian.\n"
            "Return ONLY JSON with fields:\n"
            "{\n"
            "  \"amount\": <number>,\n"
            "  \"currency\": \"<USD, UAH, EUR, PLN>\",\n"
            "  \"source\": \"<source_of_income>\"\n"
            "}\n\n"
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
        print(f"Raw response: {content_details}")

        try:
            details_data = json.loads(content_details)
            print(f"Parsed JSON: {details_data}")
        except json.JSONDecodeError:
            print(f"JSON Decode Error: {content_details}")
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

        print(f"Extracted – amount: {amount}, currency: {original_currency}, category/source: {category_or_source}, description: {description}")

        converted_amount = amount
        final_currency = original_currency

        # 3. Конвертація в PLN через ExchangeRate-API (якщо потрібно)
        if amount is None or not isinstance(amount, (int, float)) or float(amount) <= 0:
            print(f"Warning: Invalid amount ({amount}), skip conversion.")
        elif final_currency != "PLN":
            print(f"Fetching exchange rate {final_currency}→PLN…")
            api_key = os.getenv("EXCHANGERATE_API_KEY")
            if not api_key:
                print("Error: EXCHANGERATE_API_KEY not set, skip conversion.")
            else:
                api_url = f"https://v6.exchangerate-api.com/v6/{api_key}/latest/{final_currency}"
                try:
                    resp = requests.get(api_url)
                    resp.raise_for_status() # Raise an exception for bad status codes
                    rate_data = resp.json()
                    if rate_data.get("result") == "success":
                        rates = rate_data.get("conversion_rates", {})
                        if "PLN" in rates:
                            rate = rates["PLN"]
                            print(f"Rate: 1 {original_currency} = {rate} PLN")
                            if rate and rate > 0:
                                converted_amount = round(float(amount) * rate, 2)
                                final_currency = "PLN"
                                print(f"Converted: {converted_amount} PLN")
                            else:
                                print(f"Warning: Bad rate ({rate}), keep original.")
                        else:
                            print(f"Error: PLN not in rates keys {list(rates.keys())}.")
                    else:
                        print(f"Error from ExchangeRate-API: {rate_data.get('error-type', 'Unknown error')} .")
                except requests.exceptions.RequestException as e:
                    print(f"Network/API error: {e}")
                except Exception as e:
                    print(f"Unexpected error during ExchangeRate-API parse: {e}")
        else:
            print("Currency already PLN, no conversion needed.")

        result = {
            "type": transaction_type,
            "amount": converted_amount,
            "currency": final_currency,
            "category": category_or_source if transaction_type == 'expense' else None,
            "source": category_or_source if transaction_type == 'income' else None,
            "description": description
        }
        print(f"Final transaction data: {result}")
        return result

    except Exception as e:
        print(f"Error in process_transaction: {e}")
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

    processed = process_transaction(text)
    if processed.get("type") == "error":
        return jsonify({"error": processed.get("error", "Processing error")}), 500

    tx_type = processed.get("type", "expense")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    amount = processed.get("amount")
    final_currency = processed.get("currency", "PLN")
    category = processed.get("category")
    source = processed.get("source")
    description = processed.get("description")

    # Визначаємо діапазон та рядок для запису в Google Sheets
    if tx_type == 'expense':
        # Для витрат записуємо в стовпці A-E
        table_range = 'A1'
        # Рядок має бути [timestamp, amount, currency, category, description]
        vals = [timestamp, amount, final_currency, category, description]
        print(f"Appending expense row to range {table_range}: {vals}")
    else:
        # Для доходів записуємо в стовпці G-I
        table_range = 'G1'
        # Рядок має бути [income_date, income_amount, income_source]
        # sheet.append_row вимагає, щоб кількість елементів в списку vals
        # відповідала кількості стовпців від початку table_range (G) до потрібного кінця.
        # Щоб записати лише в G, H, I, але почати з G1, нам потрібно передати список з 3 елементів.
        income_date = timestamp
        income_amount = amount
        income_source = source
        vals = [income_date, income_amount, income_source]
        print(f"Appending income row to range {table_range}: {vals}")

    try:
        sheet.append_row(vals, value_input_option='USER_ENTERED', table_range=table_range)
        print("Row appended successfully.")
    except Exception as e:
        print(f"Error appending to Google Sheet: {e}")
        return jsonify({"error": f"Failed writing to Google Sheet: {e}"}), 500

    return jsonify({
        "status": "ok",
        "transaction_type": tx_type,
        "row": vals, # Повертаємо саме vals, який був записаний
        "message": f"Successfully added {tx_type}: {text}"
    })


@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Environment PORT: {os.environ.get('PORT', 'Not set')}, Using port: {port}") # Виправлено f-рядок
    # Прив'язуємося до 0.0.0.0 для доступності ззовні в контейнері
    app.run(host="0.0.0.0", port=port)