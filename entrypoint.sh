#!/bin/sh
set -e
python manage.py migrate --noinput
python manage.py seed_rules
python manage.py collectstatic --noinput
exec "$@"
