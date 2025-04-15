FROM mcr.microsoft.com/playwright/python:v1.43.1

WORKDIR /app
COPY . .

RUN pip install -r requirements.txt

CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:5000"]