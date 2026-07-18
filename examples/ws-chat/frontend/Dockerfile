# Build the Vite/React app, then serve the static bundle with nginx on :80.
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json ./
RUN npm install
COPY . .
# Chat name chosen during the interactive install; baked into the bundle by Vite.
ARG VITE_CHAT_NAME=ws-chat
ENV VITE_CHAT_NAME=$VITE_CHAT_NAME
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
