FROM node:20-alpine
WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev
COPY server.js gen-token.sh ./
RUN chmod +x gen-token.sh
EXPOSE 3000
CMD ["node", "server.js"]
