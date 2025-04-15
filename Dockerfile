FROM python:3.11-slim

# Install system dependencies needed by Playwright
RUN apt-get update && apt-get install -y \
    wget curl gnupg ca-certificates fonts-liberation libnss3 libxss1 \
    libasound2 libatk-bridge2.0-0 libatk1.0-0 libcups2 libdbus-1-3 \
    libdrm2 libgbm1 libgtk-3-0 libxcomposite1 libxdamage1 libxrandr2 \
    libappindicator3-1 libsecret-1-0 libx11-xcb1 xvfb \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy your app files
COPY . .

# Install Python dependencies
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# Install Playwright browsers (Chromium, etc.)
RUN pip install playwright
RUN playwright install --with-deps

# Expose the port your Flask app will run on
EXPOSE 5000

# Start the Flask app using Gunicorn
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:5000"]