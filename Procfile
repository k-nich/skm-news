web: python manage.py migrate --noinput && python manage.py collectstatic --noinput && gunicorn skimnews.wsgi --bind 0.0.0.0:$PORT
