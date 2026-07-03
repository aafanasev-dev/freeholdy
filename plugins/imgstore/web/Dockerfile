# Static page served by nginx. No build step — the whole app is one HTML file that
# derives the storage API base from its own hostname at runtime.
FROM nginx:alpine
COPY html/ /usr/share/nginx/html
EXPOSE 80
