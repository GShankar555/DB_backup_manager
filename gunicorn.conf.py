import os

bind = os.getenv("GUNICORN_BIND", "127.0.0.1:8000")
workers = 1
threads = int(os.getenv("GUNICORN_THREADS", "4"))
timeout = 7200
graceful_timeout = 30
accesslog = "-"
errorlog = "-"
capture_output = True

