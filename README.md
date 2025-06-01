# Expense Tracker with Voice Input

This application allows you to track expenses using voice input through Apple Watch. It uses OpenAI GPT to parse voice input and stores the data in Google Sheets.

## Setup

1. Create a virtual environment and install dependencies:
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

2. Set up environment variables:
- Create a `.env` file based on `.env.example`
- Add your OpenAI API key
- Add your Google Sheets credentials and Sheet ID

3. Google Sheets Setup:
- Create a new Google Sheet
- Enable Google Sheets API in Google Cloud Console
- Create service account credentials and download as `credentials.json`
- Share your Google Sheet with the service account email

4. Run the application:
```bash
python app.py
```

## API Endpoints

- `POST /api/expense`: Add a new expense
  - Request body: `{"text": "я витратив 120 гривень на каву"}`
  - Response: `{"status": "ok", "row": [...], "message": "..."}`

- `GET /health`: Health check endpoint

## Apple Watch Shortcut Setup

1. Create a new Shortcut
2. Add "Dictate Text" action (Ukrainian language)
3. Add "Get Contents of URL" action:
   - URL: `https://YOUR_DOMAIN/api/expense`
   - Method: POST
   - Headers: `Content-Type: application/json`
   - Body: `{"text":"[Dictated Text]"}`
4. Add "Show Notification" action (optional)

## Google Sheets Structure

The spreadsheet will have the following columns:
- Timestamp
- Amount
- Currency
- Category
- Description

## Categories

Available expense categories:
- food
- transport
- shopping
- entertainment
- other 