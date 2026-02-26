FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

ENV PYTHONUNBUFFERED=1

CMD ["python", "-c", "from app.server import create_app; app = create_app(); app.run(host='0.0.0.0', port=5000, debug=False)"]
