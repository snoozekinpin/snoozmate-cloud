FROM python:3.12-slim

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=80
ENV HOST=0.0.0.0
ENV SNOOZMATE_SEED_DEMO_DATA=false

EXPOSE 80

CMD ["python", "main.py", "80"]
