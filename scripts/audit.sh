#!/usr/bin/env bash
# Команда для запуска Claude Code — аудит booking-сайта

cd /Users/andrzej/Iryna-Master/01-Booking-System

echo "=== Pashynska Booking System — Live Audit ==="

echo ""
echo "📊 Запуск тестов..."
source .venv/bin/activate || python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
pytest tests/ test_admin.py -q --tb=short

echo ""
echo "🔍 Live smoke-test через curl..."
curl -sI https://book.pashynskaphoto.com/ | head -5
echo "---"
curl -s https://book.pashynskaphoto.com/events | python3 -c "import json,sys; data=json.load(sys.stdin); print('Events:', [e['title'] for e in data.get('events',[])])"

echo ""
echo "🔐 Security headers check..."
curl -sI https://book.pashynskaphoto.com/ | grep -iE "strict-transport|content-security|x-content|permissions|x-frame"

echo ""
echo "📧 Himalaya email check..."
himalaya account list

echo ""
echo "🚀 Fly.io status..."
flyctl status --app iryna-booking

echo ""
echo "✅ Готово!"
