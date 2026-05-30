FROM node:20-alpine
WORKDIR /app

# Install deps first for layer caching
COPY package.json package-lock.json* ./
RUN npm install

# Pass at build time via --build-arg VITE_API_URL=... to override the default.
# A .env file in the build context is an alternative (copied by COPY . . below).
ARG VITE_API_URL
ENV VITE_API_URL=$VITE_API_URL

COPY . .
RUN npm run build

# Serve the built bundle. vite preview serves dist/ as static files.
EXPOSE 14173
CMD ["npm", "run", "preview", "--", "--host", "0.0.0.0", "--port", "14173"]
