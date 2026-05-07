#!/bin/bash

# Создаём папку для данных, если её нет
mkdir -p /data

# Создаём симлинки в /app для совместимости
ln -sf /data/bookings.db /app/bookings.db 2>/dev/null || echo "Symlink failed"
ln -sf /data/backups /app/backups 2>/dev/null || echo "Backups symlink failed"

# Запускаем приложение
export DB_PATH=/data/bookings.db
export BACKUP_DIR=/data/backups

exec gunicorn --bind :8080 --workers 1 --timeout 120 app:app