FROM mcr.microsoft.com/playwright:v1.52.0-jammy
WORKDIR /app
COPY automation-runner/package.json ./package.json
RUN npm install
COPY automation-runner/server.js ./server.js
EXPOSE 3000
CMD ["node", "server.js"]
