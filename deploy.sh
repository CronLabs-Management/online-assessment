#!/usr/bin/env bash
# ==============================================================================
# Automated Deployment Script for cPanel (Passenger)
# Domain: scrambler.in
# User: kotturco
# ==============================================================================
set -euo pipefail

VENV_PATH="/home/kotturco/virtualenv/procted_test/3.13"
APP_DIR="/home/kotturco/procted_test"
PRIVATE_MEDIA="/home/kotturco/assessment_private"

echo "==> [1/7] Activating Python Virtual Environment..."
if [ -f "$VENV_PATH/bin/activate" ]; then
    # shellcheck disable=SC1090
    source "$VENV_PATH/bin/activate"
else
    echo "ERROR: Virtual environment not found at $VENV_PATH"
    exit 1
fi

echo "==> [2/7] Navigating to application directory..."
cd "$APP_DIR"

echo "==> [3/7] Pulling latest code from Git..."
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")
    git checkout -- passenger_wsgi.py 2>/dev/null || true
    git pull origin "$CURRENT_BRANCH"
else
    echo "Notice: Not a git repository or git unavailable. Skipping pull."
fi

echo "==> [4/7] Ensuring required directories exist..."
mkdir -p "$PRIVATE_MEDIA"
mkdir -p "$APP_DIR/tmp"
chmod 750 "$PRIVATE_MEDIA" || true

echo "==> [5/7] Installing / upgrading dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo "==> [6/7] Applying database migrations & collecting static files..."
python manage.py migrate --noinput
python manage.py collectstatic --noinput

echo "==> [7/7] Restarting Passenger application..."
touch "$APP_DIR/tmp/restart.txt"

echo "==> Running Django deployment check..."
python manage.py check --deploy || true

echo "=============================================================================="
echo "Deployment completed successfully! Website: https://scrambler.in"
echo ""
echo "Post-deploy commands (run manually if needed):"
echo "  python manage.py create_admin --username admin --email you@example.com"
echo "  python manage.py test_email your-email@example.com"
echo "=============================================================================="
