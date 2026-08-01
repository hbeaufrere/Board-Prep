# Gunicorn configuration file
timeout = 600  # 10 minute timeout for long-running requests
workers = 1
bind = "0.0.0.0:10000"
