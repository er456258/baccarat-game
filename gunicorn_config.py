bind = "0.0.0.0:10000"
workers = 1  # WebSocket 最好只用一個 worker
worker_class = "eventlet"
timeout = 120
keepalive = 120
worker_connections = 1000
forwarded_allow_ips = '*'
proxy_allow_ips = '*' 