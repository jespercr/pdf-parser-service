FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir -r requirements.txt

RUN playwright install

EXPOSE 8000

CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8000", "--workers", "2"]