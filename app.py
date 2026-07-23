import base64
import gzip
import hashlib
import io
import json
import os
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, flash, redirect, render_template, request, url_for
from flask_apscheduler import APScheduler
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from sqlalchemy import select
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", DATA_DIR / "backups"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.getenv("SECRET_KEY", "change-this-secret-in-production"),
    SQLALCHEMY_DATABASE_URI=f"sqlite:///{DATA_DIR / 'backup_manager.db'}",
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SCHEDULER_API_ENABLED=False,
    SCHEDULER_TIMEZONE=os.getenv("TZ", "Asia/Kolkata"),
)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
db = SQLAlchemy(app)
csrf = CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
scheduler = APScheduler()
scheduler.init_app(app)
backup_lock = threading.Lock()


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
    container_name = db.Column(db.String(150), nullable=False)
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


def cron_kwargs(expression):
    parts = expression.split()
    if len(parts) != 5:
        raise ValueError("Cron expression must contain 5 fields: minute hour day month weekday")
    minute, hour, day, month, weekday = parts
    # APScheduler uses mon-sun/0-6, while standard cron uses sun=0/7.
    weekday = "sun" if weekday in {"0", "7"} else weekday
    return dict(minute=minute, hour=hour, day=day, month=month, day_of_week=weekday)


def sync_jobs():
    for job in scheduler.get_jobs():
        if job.id.startswith("backup_"):
            scheduler.remove_job(job.id)
    with app.app_context():
        targets = db.session.execute(select(BackupTarget).where(BackupTarget.enabled.is_(True))).scalars()
        for target in targets:
            try:
                scheduler.add_job(
                    id=f"backup_{target.id}",
                    func=run_backup_job,
                    args=[target.id],
                    trigger="cron",
                    replace_existing=True,
                    max_instances=1,
                    **cron_kwargs(target.cron_expression),
                )
            except ValueError:
                app.logger.exception("Invalid schedule for target %s", target.name)


def docker_dump_command(target):
    if target.db_type == "postgres":
        return [
            "docker", "exec", "-e", f"PGPASSWORD={target.password}",
            target.container_name, "pg_dump", "-U", target.db_user,
            "-d", target.db_name, "--no-password",
        ], "sql"
    if target.db_type == "mysql":
        return [
            "docker", "exec", "-e", f"MYSQL_PWD={target.password}",
            target.container_name, "mysqldump", "-u", target.db_user,
            "--single-transaction", "--routines", "--events", target.db_name,
        ], "sql"
    if target.db_type == "mssql":
        return [
            "docker", "exec", "-e", f"SQLCMDPASSWORD={target.password}",
            target.container_name, "/opt/mssql-tools18/bin/sqlcmd",
            "-C", "-S", "localhost", "-U", target.db_user,
            "-Q", f"BACKUP DATABASE [{target.db_name}] TO DISK='/var/opt/mssql/backup/{target.name}.bak' WITH INIT",
        ], "bak"
    raise ValueError(f"Unsupported database type: {target.db_type}")


def drive_service():
    raw = setting("google_service_account_json")
    folder_id = setting("google_drive_folder_id")
    if not raw or not folder_id:
        return None, None
    info = json.loads(raw)
    credentials = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=credentials, cache_discovery=False), folder_id


def upload_to_drive(path):
    service, folder_id = drive_service()
    if not service:
        return None
    metadata = {"name": path.name, "parents": [folder_id]}
    media = MediaFileUpload(str(path), mimetype="application/gzip", resumable=True)
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


def run_backup_job(target_id):
    with app.app_context():
        if not backup_lock.acquire(blocking=False):
            app.logger.warning("Another backup is already running")
            return
        target = db.session.get(BackupTarget, target_id)
        if not target:
            backup_lock.release()
            return
        run = BackupRun(target_id=target.id, target_name=target.name)
        db.session.add(run)
        db.session.commit()
        output_path = None
        dump_complete = False
        try:
            command, extension = docker_dump_command(target)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = BACKUP_DIR / f"{target.name}_{stamp}.{extension}.gz"
            if target.db_type == "mssql":
                # Native .bak is created inside the container, then streamed out.
                subprocess.run(command, check=True, capture_output=True, timeout=7200)
                copy_cmd = ["docker", "exec", target.container_name, "cat", f"/var/opt/mssql/backup/{target.name}.bak"]
                process = subprocess.Popen(copy_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            else:
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            with gzip.open(output_path, "wb", compresslevel=6) as compressed:
                while chunk := process.stdout.read(1024 * 1024):
                    compressed.write(chunk)
            stderr = process.stderr.read().decode(errors="replace")
            if process.wait() != 0:
                raise RuntimeError(stderr or "Database dump command failed")
            if output_path.stat().st_size == 0:
                raise RuntimeError("Backup file is empty")
            dump_complete = True
            drive_id = upload_to_drive(output_path)
            cleanup_retention(target)
            run.status = "success"
            run.filename = output_path.name
            run.size_bytes = output_path.stat().st_size
            run.message = f"Completed successfully. Drive file ID: {drive_id}" if drive_id else "Completed locally; Google Drive is not configured."
        except Exception as exc:
            if output_path and output_path.exists() and not dump_complete:
                output_path.unlink(missing_ok=True)
            run.status = "failed"
            run.message = str(exc)[:4000]
            app.logger.exception("Backup failed for %s", target.name)
        finally:
            run.finished_at = datetime.now(timezone.utc)
            db.session.commit()
            backup_lock.release()


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
    jobs = {job.id: job.next_run_time for job in scheduler.get_jobs()}
    return render_template("dashboard.html", targets=targets, runs=runs, jobs=jobs)


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
            cron_kwargs(request.form["cron_expression"])
            target = target or BackupTarget()
            target.name = request.form["name"].strip()
            target.db_type = request.form["db_type"]
            target.container_name = request.form["container_name"].strip()
            target.db_name = request.form["db_name"].strip()
            target.db_user = request.form["db_user"].strip()
            if request.form.get("password"):
                target.password_encrypted = encrypt(request.form["password"])
            target.cron_expression = request.form["cron_expression"].strip()
            target.retention_days = max(1, int(request.form["retention_days"]))
            target.enabled = "enabled" in request.form
            db.session.add(target)
            db.session.commit()
            sync_jobs()
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
        sync_jobs()
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
        folder_id=setting("google_drive_folder_id"),
        service_account_configured=bool(setting("google_service_account_json")),
    )


@app.cli.command("init-db")
def init_db_command():
    initialize()
    print("Database initialized.")


def initialize():
    db.create_all()
    if not db.session.execute(select(User).where(User.username == "admin")).scalar_one_or_none():
        db.session.add(User(username="admin", password_hash=generate_password_hash("Fusil@admin55")))
        db.session.commit()


with app.app_context():
    initialize()

if not scheduler.running:
    scheduler.start()
    sync_jobs()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=os.getenv("FLASK_DEBUG") == "1")
