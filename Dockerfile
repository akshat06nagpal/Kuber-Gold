FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY scripts ./scripts
COPY tests ./tests
COPY pytest.ini .
# single worker on purpose: SQLite + in-process scheduler (idempotency is enforced in the DB regardless)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
