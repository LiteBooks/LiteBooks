FROM node:22-alpine AS frontend

WORKDIR /build
COPY package.json package-lock.json vite.config.js ./
RUN npm ci
COPY frontend ./frontend
RUN npm run build

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN addgroup --system litebooks && adduser --system --ingroup litebooks litebooks

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY --from=frontend /build/static/frontend /app/static/frontend
RUN python manage.py collectstatic --noinput && \
    mkdir -p /app/media && \
    chown -R litebooks:litebooks /app

USER litebooks

EXPOSE 8000
CMD ["gunicorn", "litebooks.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--access-logfile", "-"]
