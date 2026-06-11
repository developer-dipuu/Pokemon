FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gcc \
    libc-dev \
    libpq-dev \
    libjpeg-dev \
    zlib1g-dev \
    libpng-dev \
    libcairo2-dev \
    libpango1.0-dev \
    gir1.2-pango-1.0 \
    gir1.2-gtk-3.0 \
    libgirepository-2.0-dev \
    gobject-introspection \
    libglib2.0-dev \
    libglib2.0-dev-bin \
    libffi-dev \
    pkg-config \
    cmake \
    python3-dev \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2

RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && node -v \
    && npm -v \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "-m", "bot.main"]
