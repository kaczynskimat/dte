FROM python:3.13-slim

WORKDIR /app
COPY src/state_bridge/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY src/state_bridge /app/src/state_bridge
COPY src/state_machine /app/src/state_machine

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "src.state_bridge.bridge"]
