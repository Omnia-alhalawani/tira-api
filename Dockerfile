FROM python:3.11-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
# data files are baked into the image; to use other files mount a volume over /srv/data (see README)
COPY data ./data
ENV TIRA_DATA=/srv/data
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
