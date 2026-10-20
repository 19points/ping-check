FROM python:3.15-slim
RUN apt-get update && apt-get install -y --no-install-recommends iputils-ping && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home app && mkdir /data && chown app:app /data
WORKDIR /app
COPY app.py index.html LICENSE ./
ENV DB_PATH=/data/ping.db PYTHONUNBUFFERED=1 PORT=8080
USER app
EXPOSE 8080
CMD ["python", "app.py"]
