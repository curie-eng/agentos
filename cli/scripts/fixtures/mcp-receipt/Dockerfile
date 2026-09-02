FROM python:3.12-slim

WORKDIR /srv
COPY server.py /srv/server.py
USER 65534:65534
EXPOSE 8000
ENTRYPOINT ["python", "-u", "/srv/server.py"]
