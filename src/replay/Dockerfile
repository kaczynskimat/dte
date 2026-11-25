FROM python:3.13-slim

WORKDIR /app
COPY src/replay/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY src/replay/producer.py /app/src/replay/producer.py
COPY data/test_mission_2021-10-05.csv /app/data/test_mission_2021-10-05.csv

CMD ["python", "-m", "src.replay.producer"]
