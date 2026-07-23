import base64
import fcntl
import gzip
import hashlib
import json
import os
import re
import subprocess
import threading
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import click
from cryptography.fernet import Fernet, InvalidToken
from croniter import croniter
from flask import Flask, flash, redirect, render_template, request, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as OAuthCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from sqlalchemy import inspect, select, text
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", DATA_DIR / "backups"))
BACKUP_TIMEOUT = int(os.getenv("BACKUP_TIMEOUT", "7200"))
GOOGLE_TOKEN_FILE = Path(os.getenv("GOOGLE_TOKEN_FILE", "/opt/creds/token.json"))
GOOGLE_CREDENTIALS_FILE = Path(
    os.getenv("GOOGLE_CREDENTIALS_FILE", "/opt/creds/credentials.json")
)
GOOGLE_AUTH_MODE = os.getenv("GOOGLE_AUTH_MODE", "auto").strip().lower()
DATA_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.getenv("SECRET_KEY", "change-this-secret-in-production"),
    SQLALCHEMY_DATABASE_URI=f"sqlite:///{DATA_DIR / 'backup_manager.db'}",
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
db = SQLAlchemy(app)
csrf = CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"

DATABASE_TYPES = {"postgres", "mysql", "mssql"}
CONNECTION_MODES = {"network", "docker"}
DEFAULT_PORTS = {"postgres": 5432, "mysql": 3306, "mssql": 1433}
SAFE_TARGET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
SAFE_CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,149}$")
GOOGLE_DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]


def _fernet():
    configured = os.getenv("ENCRYPTION_KEY")
    if configured:
        key = configured.encode()
    else:
        digest = hashlib.sha256(app.config["SECRET_KEY"].encode()).digest()
        key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt(value):
    return _fernet().encrypt((value or "").encode()).decode() if value else ""


def decrypt(value):
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        return ""


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)


class BackupTarget(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    db_type = db.Column(db.String(20), nullable=False)
    connection_mode = db.Column(db.String(20), nullable=False, default="network")
    db_host = db.Column(db.String(255), nullable=False, default="")
    db_port = db.Column(db.Integer)
    container_name = db.Column(db.String(150), nullable=False, default="")
    db_name = db.Column(db.String(150), nullable=False)
    db_user = db.Column(db.String(150), nullable=False)
    password_encrypted = db.Column(db.Text, nullable=False, default="")
    cron_expression = db.Column(db.String(100), nullable=False, default="0 2 * * *")
    retention_days = db.Column(db.Integer, nullable=False, default=30)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    @property
    def password(self):
        return decrypt(self.password_encrypted)


class AppSetting(db.Model):
    key = db.Column(db.String(100), primary_key=True)
    value_encrypted = db.Column(db.Text, nullable=False, default="")


class BackupRun(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    target_id = db.Column(db.Integer, db.ForeignKey("backup_target.id"), nullable=True)
    target_name = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="running")
    filename = db.Column(db.String(255))
    size_bytes = db.Column(db.Integer)
    message = db.Column(db.Text)
    started_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    finished_at = db.Column(db.DateTime)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def setting(key, default=""):
    row = db.session.get(AppSetting, key)
    return decrypt(row.value_encrypted) if row else default


def save_setting(key, value):
    row = db.session.get(AppSetting, key) or AppSetting(key=key)
    row.value_encrypted = encrypt(value)
    db.session.add(row)


def validate_cron_expression(expression):
    parts = expression.split()
    if len(parts) != 5:
        raise ValueError("Cron expression must contain 5 fields: minute hour day month weekday")
    if not croniter.is_valid(expression):
        raise ValueError("Cron expression contains an invalid value")
    return parts


def next_run_time(expression):
    try:
        validate_cron_expression(expression)
        now = datetime.now(ZoneInfo(os.getenv("TZ", "Asia/Kolkata")))
        return croniter(expression, now).get_next(datetime)
    except (ValueError, KeyError):
        return None


def validated_target_values(form, existing_target=None):
    name = form.get("name", "").strip()
    db_type = form.get("db_type", "")
    connection_mode = form.get("connection_mode", "")
    db_host = form.get("db_host", "").strip()
    container_name = form.get("container_name", "").strip()

    if not SAFE_TARGET_NAME.fullmatch(name):
        raise ValueError("Display name may contain only letters, numbers, dots, underscores, and hyphens.")
    if db_type not in DATABASE_TYPES:
        raise ValueError("Unsupported database type.")
    if connection_mode not in CONNECTION_MODES:
        raise ValueError("Choose either network host or local Docker container.")

    if connection_mode == "network":
        if not db_host or len(db_host) > 255 or any(character.isspace() for character in db_host):
            raise ValueError("Enter a valid database host name or IP address.")
        try:
            db_port = int(form.get("db_port") or DEFAULT_PORTS[db_type])
        except ValueError as exc:
            raise ValueError("Database port must be a number.") from exc
        if not 1 <= db_port <= 65535:
            raise ValueError("Database port must be between 1 and 65535.")
        container_name = ""
    else:
        if not SAFE_CONTAINER_NAME.fullmatch(container_name):
            raise ValueError("Enter a valid local Docker container name.")
        db_host = ""
        db_port = None

    cron_expression = form.get("cron_expression", "").strip()
    validate_cron_expression(cron_expression)
    try:
        retention_days = max(1, int(form.get("retention_days", "30")))
    except ValueError as exc:
        raise ValueError("Retention must be a whole number of days.") from exc

    password = form.get("password", "")
    if existing_target is None and not password:
        raise ValueError("A database password is required.")

    db_name = form.get("db_name", "").strip()
    db_user = form.get("db_user", "").strip()
    if not db_name or not db_user:
        raise ValueError("Database name and username are required.")

    return {
        "name": name,
        "db_type": db_type,
        "connection_mode": connection_mode,
        "db_host": db_host,
        "db_port": db_port,
        "container_name": container_name,
        "db_name": db_name,
        "db_user": db_user,
        "password": password,
        "cron_expression": cron_expression,
        "retention_days": retention_days,
        "enabled": "enabled" in form,
    }


def command_environment(target):
    environment = os.environ.copy()
    if target.db_type == "postgres":
        environment["PGPASSWORD"] = target.password
    elif target.db_type == "mysql":
        environment["MYSQL_PWD"] = target.password
    elif target.db_type == "mssql":
        environment["SQLCMDPASSWORD"] = target.password
    return environment


def docker_dump_command(target):
    if target.db_type == "postgres":
        return [
            "docker", "exec", "-e", "PGPASSWORD",
            target.container_name, "pg_dump", "-U", target.db_user,
            "-d", target.db_name, "--no-password",
        ], "sql"
    if target.db_type == "mysql":
        return [
            "docker", "exec", "-e", "MYSQL_PWD",
            target.container_name, "mysqldump", "-u", target.db_user,
            "--single-transaction", "--routines", "--events", target.db_name,
        ], "sql"
    if target.db_type == "mssql":
        database_name = target.db_name.replace("]", "]]")
        return [
            "docker", "exec", "-e", "SQLCMDPASSWORD",
            target.container_name, "/opt/mssql-tools18/bin/sqlcmd",
            "-C", "-S", "localhost", "-U", target.db_user,
            "-Q", f"BACKUP DATABASE [{database_name}] TO DISK='/var/opt/mssql/backup/{target.name}.bak' WITH INIT",
        ], "bak"
    raise ValueError(f"Unsupported database type: {target.db_type}")


def network_dump_command(target, output_path=None):
    port = str(target.db_port or DEFAULT_PORTS[target.db_type])
    if target.db_type == "postgres":
        return [
            "pg_dump", "--host", target.db_host, "--port", port,
            "--username", target.db_user, "--dbname", target.db_name,
            "--no-password",
        ], "sql"
    if target.db_type == "mysql":
        return [
            "mysqldump", f"--host={target.db_host}", f"--port={port}",
            f"--user={target.db_user}", "--single-transaction", "--routines",
            "--events", target.db_name,
        ], "sql"
    if target.db_type == "mssql":
        if output_path is None:
            raise ValueError("An output path is required for an MSSQL network export")
        return [
            "sqlpackage", "/Action:Export",
            f"/SourceServerName:{target.db_host},{port}",
            f"/SourceDatabaseName:{target.db_name}",
            f"/SourceUser:{target.db_user}",
            f"/SourcePassword:{target.password}",
            "/SourceEncryptConnection:True",
            "/SourceTrustServerCertificate:True",
            f"/TargetFile:{output_path}",
        ], "bacpac"
    raise ValueError(f"Unsupported database type: {target.db_type}")


def google_credentials_from_files():
    if GOOGLE_AUTH_MODE not in {"auto", "oauth", "service_account"}:
        raise RuntimeError(
            "GOOGLE_AUTH_MODE must be auto, oauth, or service_account."
        )

    oauth_error = None
    if GOOGLE_AUTH_MODE in {"auto", "oauth"} and GOOGLE_TOKEN_FILE.is_file():
        try:
            credentials = OAuthCredentials.from_authorized_user_file(
                GOOGLE_TOKEN_FILE,
                scopes=GOOGLE_DRIVE_SCOPES,
            )
            if credentials.expired and credentials.refresh_token:
                credentials.refresh(GoogleAuthRequest())
            if not credentials.valid:
                raise ValueError("the OAuth token is not valid and cannot be refreshed")
            return credentials, f"OAuth token file {GOOGLE_TOKEN_FILE}"
        except Exception as exc:
            oauth_error = (
                f"Could not load Google OAuth token {GOOGLE_TOKEN_FILE}: {exc}"
            )
            if GOOGLE_AUTH_MODE == "oauth":
                raise RuntimeError(oauth_error) from exc
    elif GOOGLE_AUTH_MODE == "oauth":
        raise RuntimeError(f"Google OAuth token file not found: {GOOGLE_TOKEN_FILE}")

    if (
        GOOGLE_AUTH_MODE in {"auto", "service_account"}
        and GOOGLE_CREDENTIALS_FILE.is_file()
    ):
        try:
            info = json.loads(GOOGLE_CREDENTIALS_FILE.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Could not read Google credentials {GOOGLE_CREDENTIALS_FILE}: {exc}"
            ) from exc
        if info.get("type") == "service_account":
            credentials = service_account.Credentials.from_service_account_info(
                info,
                scopes=GOOGLE_DRIVE_SCOPES,
            )
            if oauth_error:
                app.logger.warning("%s; falling back to the service account.", oauth_error)
            return credentials, f"service-account file {GOOGLE_CREDENTIALS_FILE}"
        if "installed" in info or "web" in info:
            raise RuntimeError(
                f"{GOOGLE_CREDENTIALS_FILE} is an OAuth client-secret file. "
                f"An authorized-user token is also required at {GOOGLE_TOKEN_FILE}."
            )
        raise RuntimeError(
            f"{GOOGLE_CREDENTIALS_FILE} is not a service-account credential."
        )
    if GOOGLE_AUTH_MODE == "service_account":
        raise RuntimeError(
            f"Google service-account file not found: {GOOGLE_CREDENTIALS_FILE}"
        )
    if oauth_error:
        raise RuntimeError(oauth_error)

    return None, None


def drive_service():
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip() or setting(
        "google_drive_folder_id"
    )
    if not folder_id:
        return None, None

    credentials, source = google_credentials_from_files()
    if credentials is None:
        raw = setting("google_service_account_json")
        if not raw:
            return None, None
        info = json.loads(raw)
        credentials = service_account.Credentials.from_service_account_info(
            info,
            scopes=GOOGLE_DRIVE_SCOPES,
        )
        source = "encrypted dashboard service-account credential"

    app.logger.info("Using Google Drive credentials from %s", source)
    return build(
        "drive",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    ), folder_id


def upload_to_drive(path):
    service, folder_id = drive_service()
    if not service:
        return None
    metadata = {"name": path.name, "parents": [folder_id]}
    mimetype = "application/gzip" if path.suffix == ".gz" else "application/octet-stream"
    media = MediaFileUpload(str(path), mimetype=mimetype, resumable=True)
    return service.files().create(body=metadata, media_body=media, fields="id").execute()["id"]


def cleanup_retention(target):
    cutoff = datetime.now(timezone.utc) - timedelta(days=target.retention_days)
    prefix = f"{target.name}_"
    for path in BACKUP_DIR.glob(f"{prefix}*"):
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if modified < cutoff:
            path.unlink(missing_ok=True)

    service, folder_id = drive_service()
    if not service:
        return
    query = f"'{folder_id}' in parents and trashed = false and name contains '{prefix}'"
    token = None
    while True:
        result = service.files().list(
            q=query, fields="nextPageToken, files(id,name,createdTime)", pageToken=token
        ).execute()
        for item in result.get("files", []):
            created = datetime.fromisoformat(item["createdTime"].replace("Z", "+00:00"))
            if item["name"].startswith(prefix) and created < cutoff:
                service.files().delete(fileId=item["id"]).execute()
        token = result.get("nextPageToken")
        if not token:
            break


def acquire_backup_lock(target_id):
    lock_path = DATA_DIR / f"backup-{target_id}.lock"
    lock_file = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_file)
        return None
    return lock_file


def release_backup_lock(lock_file):
    fcntl.flock(lock_file, fcntl.LOCK_UN)
    os.close(lock_file)


def stream_command_to_gzip(command, environment, output_path):
    timed_out = threading.Event()
    with tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            env=environment,
        )

        def stop_process():
            timed_out.set()
            process.kill()

        timer = threading.Timer(BACKUP_TIMEOUT, stop_process)
        timer.daemon = True
        timer.start()
        try:
            with gzip.open(output_path, "wb", compresslevel=6) as compressed:
                while chunk := process.stdout.read(1024 * 1024):
                    compressed.write(chunk)
            return_code = process.wait()
        except Exception:
            process.kill()
            process.wait()
            raise
        finally:
            timer.cancel()

        stderr_file.seek(0)
        stderr = stderr_file.read().decode(errors="replace")
        if timed_out.is_set():
            raise TimeoutError(f"Database backup exceeded {BACKUP_TIMEOUT} seconds")
        if return_code != 0:
            raise RuntimeError(stderr or "Database dump command failed")


def create_backup_file(target, output_path):
    environment = command_environment(target)
    if target.connection_mode == "network" and target.db_type == "mssql":
        command, _ = network_dump_command(target, output_path)
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=BACKUP_TIMEOUT,
            env=environment,
        )
        if completed.returncode != 0:
            error = completed.stderr.decode(errors="replace").strip()
            raise RuntimeError(error or "MSSQL BACPAC export failed")
        return

    if target.connection_mode == "docker":
        command, _ = docker_dump_command(target)
    else:
        command, _ = network_dump_command(target)

    if target.connection_mode == "docker" and target.db_type == "mssql":
        container_backup = f"/var/opt/mssql/backup/{target.name}.bak"
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            timeout=BACKUP_TIMEOUT,
            env=environment,
        )
        copy_command = ["docker", "exec", target.container_name, "cat", container_backup]
        try:
            stream_command_to_gzip(copy_command, environment, output_path)
        finally:
            subprocess.run(
                ["docker", "exec", target.container_name, "rm", "-f", container_backup],
                capture_output=True,
                timeout=60,
            )
    else:
        stream_command_to_gzip(command, environment, output_path)


def run_backup_job(target_id):
    with app.app_context():
        target = db.session.get(BackupTarget, target_id)
        if not target:
            return "failed"
        lock_file = acquire_backup_lock(target.id)
        if lock_file is None:
            message = "Skipped because another backup is already running."
            run = BackupRun(
                target_id=target.id,
                target_name=target.name,
                status="skipped",
                message=message,
                finished_at=datetime.now(timezone.utc),
            )
            db.session.add(run)
            db.session.commit()
            app.logger.warning(message)
            return "skipped"
        run = BackupRun(target_id=target.id, target_name=target.name)
        db.session.add(run)
        db.session.commit()
        output_path = None
        dump_complete = False
        try:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            if target.connection_mode == "network" and target.db_type == "mssql":
                output_path = BACKUP_DIR / f"{target.name}_{stamp}.bacpac"
            else:
                extension = docker_dump_command(target)[1] if target.connection_mode == "docker" else network_dump_command(target)[1]
                output_path = BACKUP_DIR / f"{target.name}_{stamp}.{extension}.gz"
            create_backup_file(target, output_path)
            if output_path.stat().st_size == 0:
                raise RuntimeError("Backup file is empty")
            dump_complete = True
            drive_id = upload_to_drive(output_path)
            cleanup_retention(target)
            run.status = "success"
            run.filename = output_path.name
            run.size_bytes = output_path.stat().st_size
            run.message = f"Completed successfully. Drive file ID: {drive_id}" if drive_id else "Completed locally; Google Drive is not configured."
            result = "success"
        except Exception as exc:
            if output_path and output_path.exists() and not dump_complete:
                output_path.unlink(missing_ok=True)
            run.status = "failed"
            run.message = str(exc)[:4000]
            app.logger.exception("Backup failed for %s", target.name)
            result = "failed"
        finally:
            run.finished_at = datetime.now(timezone.utc)
            db.session.commit()
            release_backup_lock(lock_file)
        return result


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        user = db.session.execute(select(User).where(User.username == request.form["username"])).scalar_one_or_none()
        if user and check_password_hash(user.password_hash, request.form["password"]):
            login_user(user)
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.", "danger")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    targets = db.session.execute(select(BackupTarget).order_by(BackupTarget.name)).scalars().all()
    runs = db.session.execute(select(BackupRun).order_by(BackupRun.started_at.desc()).limit(25)).scalars().all()
    next_runs = {target.id: next_run_time(target.cron_expression) for target in targets if target.enabled}
    return render_template("dashboard.html", targets=targets, runs=runs, next_runs=next_runs)


@app.route("/targets/new", methods=["GET", "POST"])
@app.route("/targets/<int:target_id>/edit", methods=["GET", "POST"])
@login_required
def target_form(target_id=None):
    target = db.session.get(BackupTarget, target_id) if target_id else None
    if target_id and not target:
        flash("Backup target not found.", "danger")
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        try:
            values = validated_target_values(request.form, target)
            target = target or BackupTarget()
            target.name = values["name"]
            target.db_type = values["db_type"]
            target.connection_mode = values["connection_mode"]
            target.db_host = values["db_host"]
            target.db_port = values["db_port"]
            target.container_name = values["container_name"]
            target.db_name = values["db_name"]
            target.db_user = values["db_user"]
            if values["password"]:
                target.password_encrypted = encrypt(values["password"])
            target.cron_expression = values["cron_expression"]
            target.retention_days = values["retention_days"]
            target.enabled = values["enabled"]
            db.session.add(target)
            db.session.commit()
            flash("Backup target saved.", "success")
            return redirect(url_for("dashboard"))
        except Exception as exc:
            db.session.rollback()
            flash(f"Could not save target: {exc}", "danger")
    return render_template("target_form.html", target=target)


@app.post("/targets/<int:target_id>/run")
@login_required
def run_target(target_id):
    target = db.session.get(BackupTarget, target_id)
    if not target:
        flash("Backup target not found.", "danger")
    else:
        threading.Thread(target=run_backup_job, args=(target_id,), daemon=True).start()
        flash(f"Backup for {target.name} started.", "success")
    return redirect(url_for("dashboard"))


@app.post("/targets/<int:target_id>/delete")
@login_required
def delete_target(target_id):
    target = db.session.get(BackupTarget, target_id)
    if target:
        db.session.delete(target)
        db.session.commit()
        flash("Backup target deleted. Existing backup files were preserved.", "success")
    return redirect(url_for("dashboard"))


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        try:
            save_setting("google_drive_folder_id", request.form.get("google_drive_folder_id", "").strip())
            service_json = request.form.get("google_service_account_json", "").strip()
            if service_json:
                parsed = json.loads(service_json)
                if parsed.get("type") != "service_account" or not parsed.get("client_email"):
                    raise ValueError("The JSON is not a Google service-account credential.")
                save_setting("google_service_account_json", service_json)
            db.session.commit()
            flash("Settings saved.", "success")
            return redirect(url_for("settings"))
        except (json.JSONDecodeError, ValueError) as exc:
            db.session.rollback()
            flash(f"Could not save settings: {exc}", "danger")
    return render_template(
        "settings.html",
        folder_id=os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()
        or setting("google_drive_folder_id"),
        folder_id_from_env=bool(os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()),
        google_auth_mode=GOOGLE_AUTH_MODE,
        token_file=str(GOOGLE_TOKEN_FILE),
        token_file_present=GOOGLE_TOKEN_FILE.is_file(),
        credentials_file=str(GOOGLE_CREDENTIALS_FILE),
        credentials_file_present=GOOGLE_CREDENTIALS_FILE.is_file(),
        service_account_configured=bool(setting("google_service_account_json")),
    )


@app.cli.command("init-db")
def init_db_command():
    initialize()
    print("Database initialized.")


@app.cli.command("check-google-drive")
def check_google_drive_command():
    """Verify configured Google credentials and destination-folder access."""
    with app.app_context():
        service, folder_id = drive_service()
        if not service or not folder_id:
            raise click.ClickException(
                "Google Drive is not fully configured. Set credentials and "
                "GOOGLE_DRIVE_FOLDER_ID."
            )
        try:
            folder = service.files().get(
                fileId=folder_id,
                fields="id,name,mimeType",
            ).execute()
        except Exception as exc:
            raise click.ClickException(
                f"Could not access Google Drive folder: {exc}"
            ) from exc
        if folder.get("mimeType") != "application/vnd.google-apps.folder":
            raise click.ClickException(
                "GOOGLE_DRIVE_FOLDER_ID does not identify a Google Drive folder."
            )
        click.echo(
            f"Google Drive access OK: {folder.get('name', folder_id)} ({folder_id})"
        )


@app.cli.command("run-backup")
@click.option("--target-id", type=int, help="ID of the backup target to run.")
@click.option("--target-name", help="Display name of the backup target to run.")
def run_backup_command(target_id, target_name):
    """Run one backup. This command is intended for system cron and diagnostics."""
    if bool(target_id) == bool(target_name):
        raise click.UsageError("Provide exactly one of --target-id or --target-name.")
    with app.app_context():
        if target_id:
            target = db.session.get(BackupTarget, target_id)
        else:
            target = db.session.execute(
                select(BackupTarget).where(BackupTarget.name == target_name)
            ).scalar_one_or_none()
        if not target:
            raise click.ClickException("Backup target not found.")
        result = run_backup_job(target.id)
        click.echo(f"{target.name}: {result}")
        if result == "failed":
            raise click.ClickException("Backup failed. See the dashboard or service logs for details.")


@app.cli.command("run-scheduled-backups")
def run_scheduled_backups_command():
    """Run enabled targets whose cron expression matches the current minute."""
    local_now = datetime.now(ZoneInfo(os.getenv("TZ", "Asia/Kolkata"))).replace(second=0, microsecond=0)
    with app.app_context():
        targets = db.session.execute(
            select(BackupTarget)
            .where(BackupTarget.enabled.is_(True))
            .order_by(BackupTarget.name)
        ).scalars().all()
        due_targets = []
        for target in targets:
            try:
                validate_cron_expression(target.cron_expression)
                if croniter.match(target.cron_expression, local_now):
                    due_targets.append(target)
            except ValueError:
                app.logger.error(
                    "Skipping target %s because its cron expression is invalid: %s",
                    target.name,
                    target.cron_expression,
                )
        if not due_targets:
            return
        failures = []
        for target in due_targets:
            result = run_backup_job(target.id)
            click.echo(f"{target.name}: {result}")
            if result == "failed":
                failures.append(target.name)
        if failures:
            raise click.ClickException(f"Backups failed: {', '.join(failures)}")


def migrate_database():
    columns = {column["name"] for column in inspect(db.engine).get_columns("backup_target")}
    statements = []
    if "connection_mode" not in columns:
        statements.append(
            "ALTER TABLE backup_target ADD COLUMN connection_mode VARCHAR(20) NOT NULL DEFAULT 'docker'"
        )
    if "db_host" not in columns:
        statements.append(
            "ALTER TABLE backup_target ADD COLUMN db_host VARCHAR(255) NOT NULL DEFAULT ''"
        )
    if "db_port" not in columns:
        statements.append("ALTER TABLE backup_target ADD COLUMN db_port INTEGER")
    for statement in statements:
        db.session.execute(text(statement))
    if statements:
        db.session.commit()


def initialize():
    db.create_all()
    migrate_database()
    if not db.session.execute(select(User).where(User.username == "admin")).scalar_one_or_none():
        db.session.add(User(username="admin", password_hash=generate_password_hash("Fusil@admin55")))
        db.session.commit()


with app.app_context():
    initialize()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=os.getenv("FLASK_DEBUG") == "1")
