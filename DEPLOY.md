# Full VPS Deploy Guide

This guide is the full terminal setup for running this bot on a fresh Ubuntu VPS.

It installs everything needed:

- Git
- Python
- Node.js
- PostgreSQL
- Redis
- project dependencies
- Pokemon Showdown build
- `systemd` service

These steps assume:

- Ubuntu 22.04 or 24.04
- you want the project in `/opt/pokeplaybot`
- you want PostgreSQL as the main database
- you want Redis enabled

## 1. Connect to your VPS

```bash
ssh root@YOUR_SERVER_IP
```

If your VPS provider gave you a different user, use that instead of `root`.

## 2. Update Ubuntu

```bash
apt update
apt upgrade -y
```

## 3. Install base packages

```bash
apt install -y git curl build-essential ca-certificates software-properties-common
```

## 4. Install Python

```bash
apt install -y python3 python3-venv python3-pip
python3 --version
pip3 --version
```

## 5. Install Node.js 20

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt install -y nodejs
node -v
npm -v
```

## 6. Install PostgreSQL and Redis

```bash
apt install -y postgresql postgresql-contrib redis-server
systemctl enable postgresql
systemctl start postgresql
systemctl enable redis-server
systemctl start redis-server
systemctl status postgresql --no-pager
systemctl status redis-server --no-pager
```

## 7. Create the project folder

```bash
mkdir -p /opt/pokeplaybot
cd /opt/pokeplaybot
```

If you are not root, make sure your user owns the folder:

```bash
chown -R $USER:$USER /opt/pokeplaybot
```

## 8. Clone your repo

```bash
git clone YOUR_REPO_URL /opt/pokeplaybot
cd /opt/pokeplaybot
```

Example:

```bash
git clone https://github.com/YOUR_NAME/YOUR_REPO.git /opt/pokeplaybot
cd /opt/pokeplaybot
```

## 9. Create Python virtual environment

```bash
python3 -m venv /opt/pokeplaybot/.venv
source /opt/pokeplaybot/.venv/bin/activate
python -m pip install --upgrade pip
```

## 10. Install Python dependencies

```bash
pip install -r bot/requirements.txt
```

## 11. Install and build Pokemon Showdown

```bash
cd /opt/pokeplaybot/server/pokemon-showdown
npm install
npm run build
cd /opt/pokeplaybot
```

## 12. Create PostgreSQL database and user

Open PostgreSQL:

```bash
sudo -u postgres psql
```

Then run:

```sql
CREATE USER myuser WITH PASSWORD 'strong_password_here';
CREATE DATABASE showdownreal OWNER myuser;
\q
```

If you want different names, that is fine, but then use the same values in `.env`.

## 13. Create the `.env` file

```bash
nano /opt/pokeplaybot/.env
```

Paste this and replace the values:

```env
TELEGRAM_API_ID=YOUR_TELEGRAM_API_ID
TELEGRAM_API_HASH=YOUR_TELEGRAM_API_HASH
TELEGRAM_BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN

DB_HOST=127.0.0.1
DB_NAME=showdownreal
DB_USER=myuser
DB_PASS=strong_password_here
DB_PORT=5432

REDIS_URL=redis://127.0.0.1:6379/0

DB_AUTO_FALLBACK_TO_SQLITE=true
```

Save and exit:

- `Ctrl+O`
- `Enter`
- `Ctrl+X`

## 14. Make sure runtime folders exist

```bash
mkdir -p /opt/pokeplaybot/bot/runtime
mkdir -p /opt/pokeplaybot/bot/game/data/imported
```

## 15. Test the bot manually once

```bash
cd /opt/pokeplaybot
source /opt/pokeplaybot/.venv/bin/activate
python -m bot.main
```

If it starts correctly, stop it with:

```bash
Ctrl+C
```

## 16. Create a Linux user for the bot

```bash
useradd -r -s /bin/bash -d /opt/pokeplaybot pokeplaybot || true
chown -R pokeplaybot:pokeplaybot /opt/pokeplaybot
```

If you prefer to run the service as your own VPS user, you can skip this and adjust the service file later.

## 17. Create the systemd service

```bash
nano /etc/systemd/system/pokeplaybot.service
```

Paste this:

```ini
[Unit]
Description=PokePlay Telegram Bot
After=network.target postgresql.service redis-server.service
Wants=postgresql.service redis-server.service

[Service]
Type=simple
User=pokeplaybot
Group=pokeplaybot
WorkingDirectory=/opt/pokeplaybot
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/pokeplaybot/.venv/bin/python -m bot.main
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Save and exit:

- `Ctrl+O`
- `Enter`
- `Ctrl+X`

## 18. Enable and start the bot service

```bash
systemctl daemon-reload
systemctl enable pokeplaybot
systemctl start pokeplaybot
systemctl status pokeplaybot --no-pager
```

## 19. View live logs

```bash
journalctl -u pokeplaybot -f
```

Press `Ctrl+C` to stop watching logs.

## 20. Useful service commands

Restart:

```bash
systemctl restart pokeplaybot
```

Stop:

```bash
systemctl stop pokeplaybot
```

Start:

```bash
systemctl start pokeplaybot
```

Status:

```bash
systemctl status pokeplaybot --no-pager
```

## 21. Update the bot later

```bash
cd /opt/pokeplaybot
git pull
source /opt/pokeplaybot/.venv/bin/activate
pip install -r bot/requirements.txt
cd /opt/pokeplaybot/server/pokemon-showdown
npm install
npm run build
cd /opt/pokeplaybot
systemctl restart pokeplaybot
systemctl status pokeplaybot --no-pager
```

## 22. Optional: migrate old SQLite data to PostgreSQL

If you already have old bot data in SQLite and want to move it into PostgreSQL:

```bash
cd /opt/pokeplaybot
source /opt/pokeplaybot/.venv/bin/activate
python -m bot.db.migrate_sqlite_to_postgres
```

## 23. Quick install block

If you want the shortest copy-paste flow, this is the compressed version:

```bash
apt update && apt upgrade -y
apt install -y git curl build-essential ca-certificates software-properties-common python3 python3-venv python3-pip postgresql postgresql-contrib redis-server
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt install -y nodejs
systemctl enable postgresql && systemctl start postgresql
systemctl enable redis-server && systemctl start redis-server
git clone YOUR_REPO_URL /opt/pokeplaybot
cd /opt/pokeplaybot
python3 -m venv /opt/pokeplaybot/.venv
source /opt/pokeplaybot/.venv/bin/activate
pip install --upgrade pip
pip install -r bot/requirements.txt
cd /opt/pokeplaybot/server/pokemon-showdown
npm install
npm run build
```

Then do:

1. create PostgreSQL user and database
2. create `/opt/pokeplaybot/.env`
3. test with `python -m bot.main`
4. create the `systemd` service
5. start it with `systemctl start pokeplaybot`

## Notes

- Your code supports PostgreSQL as the main database.
- Your code can fall back to SQLite, but for VPS use PostgreSQL.
- Redis is optional in code, but recommended for production.
- If the bot token or Telegram API secrets were ever shared publicly, rotate them before deploying.
