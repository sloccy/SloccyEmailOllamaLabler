FROM node:22-slim AS assets

WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY app/static/ ./app/static/
RUN npm run build

FROM python:3.14-alpine

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && adduser -D -u 1000 -s /sbin/nologin appuser

COPY app/ ./app/
COPY --from=assets /build/app/static/dist/ ./app/static/dist/
COPY --from=assets /build/app/static/vendor/ ./app/static/vendor/

ENV PYTHONUNBUFFERED=1

USER appuser

CMD ["waitress-serve", "--host=0.0.0.0", "--port=5000", "--call", "app.server:create_app"]
