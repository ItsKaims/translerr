FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg tesseract-ocr tesseract-ocr-eng libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pgsrip==0.3.0
WORKDIR /app
COPY app.py index.html admin.html login.html ./
ENV PYTHONUNBUFFERED=1
CMD ["python", "app.py"]
