# VPS Deployment

This project runs as:

- Python bot process
- local Node-based Pokemon Showdown worker
- PostgreSQL
- Redis

These instructions assume Ubuntu 22.04/24.04.

## 1. Copy the repo

```bash
sudo mkdir -p /opt/pokeplaybot
sudo chown -R $USER:$USER /opt/pokeplaybot
git clone <your-repo-url> /opt/pokeplaybot
cd /opt/pokeplaybot
```

## 2. Install system dependencies

```bash
bash deploy/linux/setup.sh /opt/pokeplaybot
```

If the repo was cloned after running the script, run these manually:

```bash
/opt/pokeplaybot/.venv/bin/pip install -r bot/requirements.txt
cd /opt/pokeplaybot/server/pokemon-showdown
npm install
node build
```

## 3. Configure environment

Create `/opt/pokeplaybot/.env`:

```env
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_BOT_TOKEN=your_bot_token

DB_HOST=127.0.0.1
DB_NAME=showdownreal
DB_USER=myuser
DB_PASS=strong_password_here
DB_PORT=5432

REDIS_URL=redis://127.0.0.1:6379/0
```

## 4. Create PostgreSQL database

```bash
sudo -u postgres psql
```

Inside `psql`:

```sql
CREATE USER myuser WITH PASSWORD 'strong_password_here';
CREATE DATABASE showdownreal OWNER myuser;
\q
```

## 5. Install the systemd service

```bash
sudo cp deploy/linux/pokeplaybot.service /etc/systemd/system/pokeplaybot.service
sudo systemctl daemon-reload
sudo systemctl enable pokeplaybot
sudo systemctl start pokeplaybot
```

## 6. Check logs

```bash
sudo journalctl -u pokeplaybot -f
```

## 7. Updating later

```bash
cd /opt/pokeplaybot
git pull
/opt/pokeplaybot/.venv/bin/pip install -r bot/requirements.txt
cd /opt/pokeplaybot/server/pokemon-showdown && npm install && node build
sudo systemctl restart pokeplaybot
```

## Notes

- Redis is strongly recommended in VPS/prod.
- PostgreSQL is recommended over SQLite on a VPS.
- The bot currently runs as a long-lived polling/client process, so `systemd` is the right fit.
- If you move from local SQLite data, use the migration command documented in `bot/README.md`.
