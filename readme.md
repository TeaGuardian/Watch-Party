# Watch Party

Personal project: synchronous video watching service (watch party).

Upload videos, create rooms, synchronized player, text chat, private rooms with "knock-knock" access, Telegram verification and basic admin panel.

## Features

- Rooms (public / private)
- Synchronized player with leader election and heartbeat
- Video upload + HLS transcoding (Celery)
- Content-addressable storage (deduplication by hash)
- Telegram bot for account linking and password reset
- Simple admin panel
- Docker Compose ready

## Tech stack

- Backend: Python, Flask, Flask-SocketIO, Eventlet
- Database: PostgreSQL (Peewee) / SQLite fallback
- Cache / Broker: Redis
- Storage: MinIO (S3) or local
- Workers: Celery + Beat
- Bot: Aiogram
- Frontend: vanilla JS + custom player
- Proxy: Nginx

