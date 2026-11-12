# 192.168.3.110 HTTP deployment

This guide is for a fresh installation with empty MySQL storage. Changing
`.env` does not rotate a password in an existing MySQL database. If this host
already has an installation, back up its data and migrate credentials first;
do not delete its volumes to apply this guide.

## Transfer the project

Copy the complete current project to the Linux server, for example to
`/opt/astron-rpa`. Include the prepared `docker/.env` (hidden files may be
omitted by upload tools), `docker/docker-compose.local-build.yml`, and
`backend/`. Do not replace the prepared `.env` with `.env.example`.
The build requires the bundled `backend/rpa-auth/lib/rpa-auth-stubs-1.0.0.jar`
and `backend/openapi-service/app/utils/rpawebsocket-1.0.7-py3-none-any.whl`.

Docker Compose 2.24.4 or newer and Python 3 are required. Maven, Java, and
Python service dependencies are installed inside the build containers.
The server must be able to reach the image registries, Maven repositories,
and Python package index used by the Dockerfiles.

## Validate and build

Run as an operator who can use Docker and write to this deployment directory:

```bash
cd /opt/astron-rpa/docker
chmod 600 .env
docker compose version
python3 --version
docker compose config --quiet
docker compose pull mysql redis minio casdoor atlas openresty-nginx
docker compose build
```

Proceed only when each step succeeds. The prepared `.env` selects the HTTP
override and local application builds automatically. It also keeps the Compose
file separator consistent across Windows and Linux.

## Initialize services

```bash
docker compose up -d --wait mysql redis minio casdoor
python3 scripts/sync-casdoor-credentials.py
```

If synchronization reports that initialization is incomplete, wait for Casdoor
initialization and retry the Python command. Do not start the application
services until synchronization succeeds. The script writes this installation's
generated credentials to `.env` without printing them.

Apply the schema in the foreground:

```bash
docker compose run --rm atlas
```

Proceed only after Atlas exits successfully. Start applications and the gateway
with dependencies already initialized:

```bash
docker compose up -d --no-build --no-deps ai-service openapi-service resource-service robot-service rpa-auth openresty-nginx
docker compose ps
```

These commands deploy the current checkout using local application images.
No TLS certificate is needed. The gateway publishes only TCP 32742 and 8000.

## Verify and connect

```bash
curl -f http://192.168.3.110:32742/health
curl -i http://192.168.3.110:32742/api/rpa-auth/user/login-check
```

The first endpoint should return `healthy`; it checks the gateway only. An
unauthenticated request to the second endpoint should return the application's
unauthorized response. Open `http://192.168.3.110:8000` to access Casdoor;
change the default administrator password and secure unused sample accounts.

Configure the Windows client in its installed `resources/conf.yaml`:

```yaml
remote_addr: http://192.168.3.110:32742/
skip_engine_start: false
```

Verify login, client online status, file upload/download, workflow execution,
and results. The gateway root currently returns 404; a browser-hosted Web
console requires separate frontend deployment. Optional AI/OCR services need
their provider credentials in `.env` before use.

## Logs and subsequent changes

```bash
docker compose logs --tail=100 ai-service openapi-service resource-service robot-service rpa-auth openresty-nginx
```

Logs may contain application data; review them before sharing. Avoid printing
the expanded `docker compose config`, which contains credentials.

After changing application source, rebuild the affected image and recreate its
service. After changing deployment environment settings, recreate the affected
containers so the settings take effect. Keep the prepared `.env` and persistent
MySQL and MinIO data backed up. Never run `docker compose down -v` to restart.
