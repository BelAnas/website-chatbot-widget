FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY chatbot ./chatbot
COPY demo_site ./demo_site
# The embedding model is downloaded on first start into /models, and the leads live in /data:
# mount a volume on both to keep them across restarts.
ENV FASTEMBED_CACHE_PATH=/models INDEX_DIR=/data/index DB_PATH=/data/chat.sqlite3
EXPOSE 8022
CMD ["python", "-m", "chatbot.cli", "serve", "--host", "0.0.0.0", "--port", "8022"]
