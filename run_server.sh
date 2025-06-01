#!/bin/bash

# Define the project directory path (handling space)
PROJECT_DIR="/Users/mavlik/Pictures/finance App/expense-tracker"

# Change to the project directory
echo "Changing directory to $PROJECT_DIR..."
cd "$PROJECT_DIR" || { echo "Error: Could not change directory to $PROJECT_DIR"; exit 1; }

# Check if virtual environment exists, create if not
if [ ! -d "venv" ]; then
    echo "Virtual environment 'venv' not found. Creating it..."
    python3 -m venv venv || { echo "Error: Failed to create virtual environment"; exit 1; }
fi

# Activate the virtual environment
echo "Activating virtual environment..."
source venv/bin/activate || { echo "Error: Failed to activate virtual environment"; exit 1; }

# Install dependencies
echo "Installing dependencies from requirements.txt..."
pip install -r requirements.txt || { echo "Error: Failed to install dependencies"; exit 1; }

# Define the port
PORT=5001

# Check if a process is already running on the target port and kill it
echo "Checking for processes on port $PORT..."
PID=$(lsof -t -i :$PORT)

if [ -n "$PID" ]; then
    echo "Process found on port $PORT with PID $PID. Killing process..."
    kill -9 $PID
    sleep 2 # Give it a moment to shut down
    # Optional: Add a check to see if the process is actually gone
    if lsof -t -i :$PORT > /dev/null; then
        echo "Warning: Process $PID might not have terminated."
    else
        echo "Process $PID terminated."
    fi
else
    echo "No process found on port $PORT."
fi

# Export the PORT variable and run the Flask app
echo "Exporting PORT=$PORT and starting Flask server..."
export PORT=$PORT
python app.py 