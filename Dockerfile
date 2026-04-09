FROM node:24-slim AS assets

WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
RUN mkdir -p vendor \
 && cp node_modules/bootstrap/dist/css/bootstrap.min.css vendor/ \
 && cp node_modules/bootstrap/dist/js/bootstrap.bundle.min.js vendor/ \
 && cp node_modules/htmx.org/dist/htmx.min.js vendor/
COPY static/app.js static/style.css ./
RUN npx --yes esbuild app.js --minify --outfile=app.min.js \
 && npx --yes esbuild style.css --minify --outfile=style.min.css

FROM --platform=$BUILDPLATFORM golang:1.26-alpine AS build

ARG TARGETOS
ARG TARGETARCH

WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
COPY --from=assets /build/vendor/ ./static/vendor/
COPY --from=assets /build/app.min.js ./static/app.js
COPY --from=assets /build/style.min.css ./static/style.css
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -ldflags="-s -w" -o /ollamail .

FROM alpine:3.23

RUN apk add --no-cache tzdata && adduser -D -u 1000 -s /sbin/nologin appuser

COPY --from=build /ollamail /ollamail

USER appuser

EXPOSE 5000

CMD ["/ollamail"]
