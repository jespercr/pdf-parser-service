FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    wget curl gnupg ca-certificates fonts-liberation \
    libnss3 libxss1 libasound2 libatk-bridge2.0-0 libatk1.0-0 \
    libcups2 libdbus-1-3 libdrm2 libgbm1 libgtk-3-0 libxcomposite1 \
    libxdamage1 libxrandr2 libappindicator3-1 libsecret-1-0 \
    libx11-xcb1 xvfb \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy app files
COPY . .

# Set Playwright path and install Chromium
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install chromium

# Expose port and run
EXPOSE 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "app:app"]