FROM node:22-alpine
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-fund --no-audit
COPY server.js ./
COPY public ./public
# Prebuilt agent apps (optional) go in ./downloads, settings/secrets in ./data
RUN mkdir -p downloads data
ENV PORT=8080 DATA_DIR=/app/data
EXPOSE 8080
CMD ["node", "server.js"]
