FROM node:22-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    NAVER_HEADLESS=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt package.json ./
RUN python3 -m pip install --break-system-packages --no-cache-dir -r requirements.txt \
    && npm install --omit=dev \
    && npx playwright install --with-deps chromium \
    && npm cache clean --force

COPY . .

CMD ["python3", "dashboard_server.py"]
