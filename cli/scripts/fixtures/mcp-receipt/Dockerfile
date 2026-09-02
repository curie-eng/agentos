FROM python:3.12-slim

WORKDIR /srv
COPY server.py /srv/server.py
USER 65534:65534
# The hosted connector's network boundary restricts peers. The fixture itself
# has no credential surface and its stdout receipt is evidence, not authority.
EXPOSE 8000
ENTRYPOINT ["python", "-u", "/srv/server.py"]
