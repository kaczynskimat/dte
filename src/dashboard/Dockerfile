FROM python:3.13-slim

WORKDIR /app
COPY src/dashboard/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# The dashboard, the state machine it uses for the sample record, and the producer its Replay page runs.
COPY .streamlit /app/.streamlit
COPY dashboard.py /app/dashboard.py
COPY src/dashboard /app/src/dashboard
COPY src/state_machine /app/src/state_machine
COPY src/replay/producer.py /app/src/replay/producer.py
COPY data/test_mission_2021-10-05.csv data/mock_state.json /app/data/

# The other services run as their own containers, so the Replay page must not try to start them.
# The Fleet page's browser polls the twin feed directly, so the feed listens on every interface.
ENV PYTHONUNBUFFERED=1 \
    DASHBOARD_MANAGES_SERVICES=0 \
    TWIN_FEED_HOST=0.0.0.0

EXPOSE 8501 8503
CMD ["streamlit", "run", "dashboard.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true", "--browser.gatherUsageStats=false"]
