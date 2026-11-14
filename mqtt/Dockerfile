FROM python:3.12-slim
RUN pip install --no-cache-dir paho-mqtt==1.6.1
COPY publisher.py /app/publisher.py
CMD ["python3", "/app/publisher.py"]
