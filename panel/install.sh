#!/usr/bin/env bash
# Sets up (or updates) the Plezy panel on this server.
#   curl -fsSL https://raw.githubusercontent.com/elcampione89/plezy/main/panel/install.sh | sudo bash
set -euo pipefail

DIR=/opt/vader-apps/plezy-panel
IMAGE=ghcr.io/elcampione89/plezy-panel:latest
DOMAIN=${DOMAIN:-plezy.daemonhub.tech}
PORT=3020

say() { printf '\n\033[1;33m▶ %s\033[0m\n' "$*"; }
ask() { local v; read -r -p "$1" v </dev/tty; printf '%s' "$v"; }
asks() { local v; read -r -s -p "$1" v </dev/tty; echo >/dev/tty; printf '%s' "$v"; }

command -v docker >/dev/null || { echo "Docker isn't installed on this server."; exit 1; }
mkdir -p "$DIR" && cd "$DIR"

# ---------------------------------------------------------------- settings (.env)
if [ -f .env ]; then
  say "Keeping your existing settings in $DIR/.env"
else
  say "Panel settings"
  pw=""
  while [ ${#pw} -lt 8 ]; do pw=$(asks "Choose a panel password (8+ characters): "); done
  ci=$(ask "Paste PANEL_CI_TOKEN (from github-secrets.txt): ")
  cat > .env <<EOF
PUBLIC_URL=https://$DOMAIN
ADMIN_PASSWORD=$pw
CI_TOKEN=$ci
SECRET_KEY=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
COOKIE_SECURE=true
EOF
  chmod 600 .env
fi

# ---------------------------------------------------------------- find the reverse proxy
proxy=$(docker ps --format '{{.Names}} {{.Image}}' | grep -Ei 'nginx-proxy-manager|jc21|traefik|caddy|swag' | head -1 || true)
proxy_name=${proxy%% *}
net=""
if [ -n "$proxy_name" ]; then
  net=$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$proxy_name" | awk '{print $1}')
  [ "$net" = host ] && net=""   # a proxy on the host network reaches 127.0.0.1:3020 directly
fi

# ---------------------------------------------------------------- docker-compose.yml
{
  cat <<EOF
services:
  plezy-panel:
    image: $IMAGE
    container_name: plezy-panel
    restart: unless-stopped
    env_file: .env
    ports:
      - "127.0.0.1:$PORT:8080"
    volumes:
      - plezy-panel-data:/data
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
EOF
  if [ -n "$net" ]; then
    printf '    networks:\n      - default\n      - proxy\n'
  fi
  if [[ "$proxy" == *traefik* ]]; then
    cat <<EOF
    labels:
      traefik.enable: "true"
      traefik.docker.network: $net
      traefik.http.routers.plezy-panel.rule: Host(\`$DOMAIN\`)
      traefik.http.routers.plezy-panel.entrypoints: websecure
      traefik.http.routers.plezy-panel.tls: "true"
      traefik.http.routers.plezy-panel.tls.certresolver: letsencrypt
      traefik.http.services.plezy-panel.loadbalancer.server.port: "8080"
EOF
  fi
  printf '\nvolumes:\n  plezy-panel-data:\n'
  if [ -n "$net" ]; then
    printf '\nnetworks:\n  proxy:\n    name: %s\n    external: true\n' "$net"
  fi
} > docker-compose.yml

# ---------------------------------------------------------------- start
say "Downloading and starting the panel"
if ! docker compose pull; then
  echo
  echo "Couldn't download the panel. On GitHub: your profile > Packages > plezy-panel >"
  echo "Package settings > Change visibility > Public. Then run this again."
  exit 1
fi
docker compose up -d

for _ in $(seq 1 20); do
  curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 || { echo "The panel didn't start. Logs:"; docker compose logs --tail 30; exit 1; }

say "Panel is running"
case "$proxy" in
  *traefik*)
    echo "Traefik ($proxy_name) will serve it at https://$DOMAIN once DNS points here." ;;
  "")
    echo "No proxy container found. Point your reverse proxy at http://127.0.0.1:$PORT for $DOMAIN." ;;
  *)
    echo "In your proxy ($proxy_name), add a host:"
    echo "  Domain:        $DOMAIN"
    if [ -n "$net" ]; then echo "  Forward to:    plezy-panel   port 8080   (http)"
    else echo "  Forward to:    127.0.0.1   port $PORT   (http)"; fi
    echo "  SSL:           request a certificate, force SSL"
    ;;
esac
echo
echo "DNS: an A record for $DOMAIN pointing at this server's IP ($(curl -fsS -4 https://api.ipify.org 2>/dev/null || echo 'this server'))."
echo "Then open https://$DOMAIN and sign in."
echo
echo "To update the panel later, run the same command again."
