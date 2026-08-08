# The aspy21 gateway: the dashboard plus the /api/v1/series ingest endpoint.
#
# It ships in mock mode, so `docker run -p 8000:8000 <image>` gives a working
# service with no historian, no credentials and no network access. Point it at a
# real IP.21 by setting ASPY21_MODE=live and the ASPY21_* variables (see
# config.py); the password belongs in the environment or a secret, never baked
# into the image.
FROM python:3.12-slim

# Bytecode files and stdout buffering both make container logs worse.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, so editing source does not reinstall them.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run unprivileged. The app writes nothing except logs; aspy21.local.json is for
# the setup wizard, which is not how a container is configured.
RUN useradd --create-home --uid 10001 aspy21 && chown -R aspy21:aspy21 /app
USER aspy21

EXPOSE 8000

# Confirms the whole chain, not just the process: in mock mode a real read
# through respx, in live mode a one-result browse against the historian.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys,json; \
r=json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=8)); \
sys.exit(0 if r.get('status')=='ok' else 1)"

# No --reload: that is a development convenience and it doubles the process count.
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
