# Database Backup Manager

A Flask dashboard for local and remote PostgreSQL, MySQL, and Microsoft SQL
Server backups. Configuration and run history are stored in SQLite. Database
passwords and Google credentials entered through the dashboard are encrypted
before they are stored. File-based Google credentials are read from paths
configured in the environment.

The application has no in-process scheduler. Linux cron starts a dispatcher
once per minute, and the dispatcher runs enabled targets whose five-field cron
expression matches that minute.

## Connection modes

Each backup target has one of two connection modes:

### Network host or IP

Use this for:

- A database on another VPS, cloud server, private network, or VPN.
- A non-container database on the application VPS.
- A local container whose database port has been published to the host.

Enter the host/IP and TCP port. The required database client runs on the
application VPS and connects directly to that address.

### Local Docker container

Use this only when the database container and this application run on the same
VPS. Enter the exact container name. A published database port is not required;
the application runs the dump utility inside the container with `docker exec`.

The `backupmgr` Linux account needs Docker access for this mode. Docker-group
access is effectively root-equivalent, so do not enable it when Docker mode is
not needed.

## Architecture

```text
Browser -> Nginx -> Gunicorn -> Flask -> SQLite
                                  |
Linux cron -> Flask CLI ----------+
                                  |
                                  +-> pg_dump/mysqldump/sqlpackage -> network DB
                                  |
                                  +-> Docker CLI -> local DB container
                                  |
                                  +-> local backup files -> optional Google Drive
```

## Software required on the VPS

Python alone is not enough. Install the client for each database type you want
to back up:

| Target | Network mode tool on this VPS | Local Docker mode |
|---|---|---|
| PostgreSQL | `pg_dump` from `postgresql-client` | Docker CLI; `pg_dump` inside the container |
| MySQL | `mysqldump` from `default-mysql-client` or MySQL client | Docker CLI; `mysqldump` inside the container |
| MSSQL | Microsoft `sqlpackage` | Docker CLI; `sqlcmd`, `cat`, and `rm` inside the container |

Also install:

- `cron` to trigger scheduled backups.
- Nginx for the documented reverse-proxy deployment.
- Docker CLI only when using local Docker targets.

On Ubuntu/Debian:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nginx cron
sudo apt install -y postgresql-client default-mysql-client
```

Use the official installation references for
[PostgreSQL client packages](https://www.postgresql.org/download/linux/ubuntu/),
[MySQL client packages](https://dev.mysql.com/doc/refman/8.0/en/linux-installation-native.html),
and [SqlPackage on Linux](https://learn.microsoft.com/en-us/sql/tools/sqlpackage/sqlpackage-download).
Microsoft recommends installing SqlPackage as a global .NET tool; its
self-contained Linux archive is also supported. Ensure the resulting
`sqlpackage` executable is in the `PATH` configured below. Install Docker from
its official repository when using local Docker targets.

Use a PostgreSQL client version at least as new as the PostgreSQL server.

### Remote database requirements

The application can back up a database anywhere only when the VPS can reach its
host and port and the database permits the supplied account:

- Open the port only to this VPS, preferably over a private network or VPN.
- Configure PostgreSQL `listen_addresses` and `pg_hba.conf` as needed.
- Configure a MySQL user grant for the VPS source address.
- Enable SQL Server TCP connections and permit the login.
- Give the account enough permission to dump/export the selected database.

Do not expose database ports publicly to all addresses.

PostgreSQL and MySQL produce compressed SQL dumps. Network MSSQL uses
`sqlpackage` and produces a BACPAC logical export. A BACPAC does not contain
server-level objects or transaction-log history. Local Docker MSSQL uses a
native `.bak`, streams it out of the container, and compresses it.

## Install the application

```bash
sudo useradd --system --home /var/lib/db-backup-manager \
  --create-home --shell /usr/sbin/nologin backupmgr

# Only if local Docker mode will be used:
sudo usermod -aG docker backupmgr

sudo mkdir -p /var/www/DB_backup_manager
sudo cp -a . /var/www/DB_backup_manager/
sudo chown -R backupmgr:backupmgr /var/www/DB_backup_manager

sudo -u backupmgr python3 -m venv /var/www/DB_backup_manager/.venv
sudo -u backupmgr /var/www/DB_backup_manager/.venv/bin/pip install \
  -r /var/www/DB_backup_manager/requirements.txt

sudo mkdir -p /var/lib/db-backup-manager/backups
sudo chown -R backupmgr:backupmgr /var/lib/db-backup-manager
sudo chmod 700 /var/lib/db-backup-manager
```

## Configure secrets

Generate independent application and encryption secrets:

```bash
/var/www/DB_backup_manager/.venv/bin/python -c \
  "import secrets; print(secrets.token_urlsafe(48))"

/var/www/DB_backup_manager/.venv/bin/python -c \
  "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Create `/etc/db-backup-manager.env`:

```ini
SECRET_KEY=put-the-first-generated-value-here
ENCRYPTION_KEY=put-the-second-generated-value-here
TZ=Asia/Kolkata
DATA_DIR=/var/lib/db-backup-manager
BACKUP_DIR=/var/lib/db-backup-manager/backups
BACKUP_TIMEOUT=7200
GOOGLE_AUTH_MODE=auto
GOOGLE_TOKEN_FILE=/opt/creds/token.json
GOOGLE_CREDENTIALS_FILE=/opt/creds/credentials.json
GOOGLE_DRIVE_FOLDER_ID=put-the-google-drive-folder-id-here
GUNICORN_BIND=127.0.0.1:8000
GUNICORN_THREADS=4
PATH=/var/lib/db-backup-manager/.dotnet/tools:/opt/sqlpackage:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin
```

Protect it:

```bash
sudo chown root:backupmgr /etc/db-backup-manager.env
sudo chmod 640 /etc/db-backup-manager.env
```

Do not change `ENCRYPTION_KEY` after storing credentials. Existing encrypted
values cannot be read with a different key.

`GOOGLE_AUTH_MODE` accepts:

- `oauth`: require the authorized-user token at `GOOGLE_TOKEN_FILE`.
- `service_account`: require a service-account JSON file at
  `GOOGLE_CREDENTIALS_FILE`.
- `auto`: try the OAuth token first, then use the service-account file.

Protect the credential directory and files:

```bash
sudo chown root:backupmgr /opt/creds
sudo chmod 750 /opt/creds
sudo chown root:backupmgr /opt/creds/token.json /opt/creds/credentials.json
sudo chmod 640 /opt/creds/token.json /opt/creds/credentials.json
```

## Install the web service

```bash
sudo cp /var/www/DB_backup_manager/deploy/db-backup-manager.service \
  /etc/systemd/system/db-backup-manager.service
sudo systemctl daemon-reload
sudo systemctl enable --now db-backup-manager
sudo systemctl status db-backup-manager
```

The web process does not run scheduled jobs. Gunicorn serves only dashboard and
manual-run requests.

## Install the external cron dispatcher

```bash
sudo cp /var/www/DB_backup_manager/deploy/db-backup-manager.cron \
  /etc/cron.d/db-backup-manager
sudo chown root:root /etc/cron.d/db-backup-manager
sudo chmod 644 /etc/cron.d/db-backup-manager
sudo systemctl enable --now cron
```

Cron invokes `flask run-scheduled-backups` once per minute. The target's cron
expression remains the source of its schedule. Per-target file locks prevent
the same target from running twice at the same time, while different databases
can still be backed up independently.

Useful manual checks:

```bash
sudo -u backupmgr sh -c \
  '. /etc/db-backup-manager.env && /var/www/DB_backup_manager/.venv/bin/flask \
  --app /var/www/DB_backup_manager/app.py run-scheduled-backups'

sudo -u backupmgr sh -c \
  '. /etc/db-backup-manager.env && /var/www/DB_backup_manager/.venv/bin/flask \
  --app /var/www/DB_backup_manager/app.py run-backup --target-name production-postgres'
```

Cron output is sent to the system journal:

```bash
sudo journalctl -t db-backup-manager-cron
```

## Configure Nginx

Edit `nginx/default.conf` and replace `server_name _;` with the server's domain,
then install it:

```bash
sudo cp /var/www/DB_backup_manager/nginx/default.conf \
  /etc/nginx/sites-available/db-backup-manager
sudo ln -s /etc/nginx/sites-available/db-backup-manager \
  /etc/nginx/sites-enabled/db-backup-manager
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

Add TLS before exposing the dashboard publicly.

## Sign in and add a database

Open `http://SERVER_IP/`.

- Username: `admin`
- Initial password: `Fusil@admin55`

For a remote database choose **Network host or IP** and enter the reachable
address and port. For a database container on this same VPS choose **Local
Docker container**.

For local Docker MSSQL, `/var/opt/mssql/backup` must exist in the container and
must be writable by SQL Server. The temporary native backup is removed from the
container after it is streamed to local backup storage.

Cron expressions use five standard fields. `0 2 * * *` runs daily at 02:00 in
the timezone configured by `TZ`.

## Google Drive

Google Drive is optional. File locations and the folder ID are taken from
`/etc/db-backup-manager.env`.

For OAuth, `token.json` must be an authorized-user token with Google Drive
access and the Drive folder must be accessible to that Google user. A client
secret file alone cannot upload files; the authorized token is required.

For a service account, `credentials.json` must contain
`"type": "service_account"`. Share the destination Drive folder with the
credential's `client_email`.

`auto` mode prefers a valid OAuth token and falls back to the service account.
The older dashboard service-account setting remains as a fallback only when
neither configured credential file is present.

Test credentials and folder access on the VPS with:

```bash
sudo -u backupmgr sh -c \
  '. /etc/db-backup-manager.env && /var/www/DB_backup_manager/.venv/bin/flask \
  --app /var/www/DB_backup_manager/app.py check-google-drive'
```

Without Drive configuration, backups stay in `BACKUP_DIR`. Retention applies to
matching local and Drive files after a successful backup. If Drive upload fails,
the local backup is preserved and the run is marked failed.

## Updating an existing installation

Restarting the application automatically adds the connection-mode, host, and
port columns to an older SQLite database. Existing targets are migrated to
local-Docker mode because that was the only mode supported previously.

```bash
sudo systemctl restart db-backup-manager
```
