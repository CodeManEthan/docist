#!/bin/bash

# Docist - Quick Start Script
#
#   ./run.sh          dev server (Flask, http://localhost:5010)
#   ./run.sh prod     production server (gunicorn, binds 127.0.0.1:5010)
#
# Environment variables (see DEPLOYMENT.md):
#   DOCIST_SECRET_KEY   signs sessions, IP hashes and result owner tags; required
#                       in prod (else generated once into instance/secret_key)
#   DATABASE_URL        accounts database (default SQLite in instance/)
#   DOCIST_PORT         port to bind (default 5010)

# Check if virtual environment exists
if [ ! -d ".venv" ]; then
    echo "Error: Virtual environment not found!"
    echo "Please create it first with: python -m venv .venv"
    exit 1
fi

# Activate virtual environment
echo "Activating virtual environment..."
source .venv/bin/activate

# Check if dependencies are installed
if ! python -c "import flask" 2>/dev/null; then
    echo "Installing dependencies..."
    pip install -r requirements.txt
fi

PORT="${DOCIST_PORT:-5010}"

if [ "$1" = "prod" ]; then
    echo "Starting Docist (gunicorn) on http://127.0.0.1:${PORT}"
    # --preload imports the app once in the master, so the secret key is
    # resolved and the database schema created before the workers fork.
    exec gunicorn --preload --workers 2 --timeout 120 --bind "127.0.0.1:${PORT}" app:app
fi

# Run the application (dev)
echo "Starting Docist (dev server)..."
echo "Open your browser to: http://localhost:${PORT}"
echo "Press Ctrl+C to stop the server"
FLASK_DEBUG=1 python app.py
