#!/bin/bash
# Install Playwright chromium and dependencies
playwright install --with-deps chromium

# Start the dashboard application
uvicorn dashboard.app:app --host 0.0.0.0 --port ${PORT:-8000}
