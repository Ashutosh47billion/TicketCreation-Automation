FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py app.py ./
COPY templates/ templates/
COPY config/ config/
COPY skills/ skills/

# Required for the container's port mapping to reach the app at all — binding
# 127.0.0.1 inside a container isn't reachable from the host even with -p.
ENV FLASK_HOST=0.0.0.0
ENV FLASK_PORT=5000
# Off by default in the image: Werkzeug's debugger is a real RCE risk on any
# port actually reachable from outside the process, which a mapped container
# port is. Set FLASK_DEBUG=1 at `docker run` time if you specifically want
# auto-reload for local development inside the container.
ENV FLASK_DEBUG=0

EXPOSE 5000

# GITHUB_TOKEN/OWNER/REPO, AI_API_KEY, GEMINI_API_KEY etc. are NOT baked into
# the image — pass them at runtime (--env-file .env, or -e per variable, or via
# docker-compose.yml's env_file:). ticket_log.json should be a mounted volume
# so history survives a container restart — see docker-compose.yml.
CMD ["python", "app.py"]
