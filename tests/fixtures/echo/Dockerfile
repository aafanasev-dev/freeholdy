# Echo project fixture for tests/test_nginx.sh.
# EXPOSE is mandatory — freeholdy rejects a Dockerfile upload (400) without it
# (app/services/scan.py::exposed_port). The container is bound to 127.0.0.1 and
# proxied by nginx; the echo server just reflects the request body back.
FROM python:3.12-alpine
COPY server.py /server.py
EXPOSE 8080
CMD ["python", "/server.py"]
