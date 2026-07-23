# Database Backup Manager

A Flask dashboard for scheduling backups of PostgreSQL, MySQL, and Microsoft SQL
Server databases running in Docker containers. Flask runs directly on the Linux
host through Gunicorn and Nginx. SQLite stores configuration and run history.

Database passwords and Google service-account credentials are encrypted before
they are stored.

## Architecture

```text
Browser -> Nginx :80/:443 -> Gunicorn 127.0.0.1:8000 -> Flask
                                                        |
                                                        +-> SQLite
                                                        +-> Docker CLI
                                                              |
                                                              +-> DB containers
```

Only the databases run in Docker. Do not containerize this Flask application.

## 1. Install host packages

The commands below target Ubuntu/Debian:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nginx docker.io
```

If Docker is already installed, keep the existing installation.

## 2. Install the application

```bash
sudo useradd --system --home /var/lib/db-backup-manager \
  --create-home --shell /usr/sbin/nologin backupmgr
sudo usermod -aG docker backupmgr

sudo mkdir -p /opt/db-backup-manager
sudo cp -a . /opt/db-backup-manager/
sudo chown -R backupmgr:backupmgr /opt/db-backup-manager

sudo -u backupmgr python3 -m venv /opt/db-backup-manager/.venv
sudo -u backupmgr /opt/db-backup-manager/.venv/bin/pip install \
  -r /opt/db-backup-manager/requirements.txt

sudo mkdir -p /var/lib/db-backup-manager/backups
sudo chown -R backupmgr:backupmgr /var/lib/db-backup-manager
sudo chmod 700 /var/lib/db-backup-manager
```

The `backupmgr` account needs membership in the `docker` group because the
application runs `docker exec` against database containers. Docker-group access
is effectively privileged; restrict access to this account and dashboard.

## 3. Create secrets and environment configuration

Generate the application secret:

```bash
/opt/db-backup-manager/.venv/bin/python -c \
  "import secrets; print(secrets.token_urlsafe(48))"
```

Generate the encryption key:

```bash
/opt/db-backup-manager/.venv/bin/python -c \
  "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Create `/etc/db-backup-manager.env`:

```ini
SECRET_KEY=put-the-first-generated-value-here
ENCRYPTION_KEY=put-the-second-generated-value-here
TZ=Asia/Kolkata
DATA_DIR=/var/lib/db-backup-manager
BACKUP_DIR=/var/lib/db-backup-manager/backups
GUNICORN_BIND=127.0.0.1:8000
GUNICORN_THREADS=4
```

Protect it:

```bash
sudo chown root:backupmgr /etc/db-backup-manager.env
sudo chmod 640 /etc/db-backup-manager.env
```

Do not change `ENCRYPTION_KEY` after saving database credentials. Changing it
will make existing encrypted values unreadable.

## 4. Configure Gunicorn as a system service

```bash
sudo cp /opt/db-backup-manager/deploy/db-backup-manager.service \
  /etc/systemd/system/db-backup-manager.service
sudo systemctl daemon-reload
sudo systemctl enable --now db-backup-manager
sudo systemctl status db-backup-manager
```

View application and scheduler logs with:

```bash
sudo journalctl -u db-backup-manager -f
```

Gunicorn intentionally uses one worker. The scheduler runs inside the Flask
process, so multiple workers would start duplicate scheduled jobs. Four threads
allow the dashboard to remain responsive during work.

## 5. Configure Nginx

Edit `nginx/default.conf` and replace `server_name _;` with the server's domain
when one is available. Then install it:

```bash
sudo cp /opt/db-backup-manager/nginx/default.conf \
  /etc/nginx/sites-available/db-backup-manager
sudo ln -s /etc/nginx/sites-available/db-backup-manager \
  /etc/nginx/sites-enabled/db-backup-manager
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

Add a TLS certificate before exposing the dashboard publicly.

## 6. Sign in

Open `http://SERVER_IP/`.

- Username: `admin`
- Password: `Fusil@admin55`

The account is created automatically on the first start, and the password is
stored as a hash in SQLite.

## 7. Configure database containers

Add each database from the dashboard using its exact Docker container name. The
application does not use the database container's published port; it invokes the
backup utility inside the container.

Required tools:

- PostgreSQL: `pg_dump`
- MySQL: `mysqldump`
- MSSQL: `/opt/mssql-tools18/bin/sqlcmd` and `cat`

For MSSQL, `/var/opt/mssql/backup` must exist in the database container and be
writable by SQL Server.

Cron expressions have five fields. `0 2 * * *` runs every day at 2 AM using the
timezone in `/etc/db-backup-manager.env`.

## Google Drive

Google Drive is optional. Enter its folder ID and paste the complete service
account JSON through the dashboard. Share that Drive folder with the service
account email address.

Without Drive configuration, backups remain under
`/var/lib/db-backup-manager/backups`. Retention is applied to matching local and
Drive files after successful backups. If an upload fails, the local backup is
preserved.
