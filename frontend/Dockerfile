# The UI's dev server (profile app, `make app-up`). Only the Node runtime lives here: compose
# bind-mounts the source and keeps node_modules in a named volume, so nothing is installed at build.
# Pin >= the package.json engines floor (24.15).
FROM node:24.21.0-slim
WORKDIR /app
# A new named volume copies the owner of its mount point, so npm (user node) can write to it.
RUN mkdir node_modules && chown node:node /app node_modules
USER node
EXPOSE 5173
