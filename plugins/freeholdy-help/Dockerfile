FROM python:3.11-alpine
COPY index.html /www/index.html
EXPOSE 80
CMD ["python3", "-m", "http.server", "80", "--directory", "/www"]
