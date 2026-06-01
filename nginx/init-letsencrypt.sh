#!/bin/sh
# One-time TLS bootstrap for the PROD stack. Run ONCE on the VM, AFTER the DNS
# A record for $DOMAIN points at this host's public IP and ports 80/443 are open.
#
#   cd ~/tokentracker
#   sh nginx/init-letsencrypt.sh            # real certificate
#   STAGING=1 sh nginx/init-letsencrypt.sh  # dry-run against LE staging first
#
# What it does: creates a throwaway self-signed cert so nginx can start, brings
# nginx up, then replaces it with a real Let's Encrypt cert (HTTP-01 webroot)
# and reloads nginx. After this, the certbot sidecar auto-renews and nginx
# reloads every 6h — no need to run this again.
set -eu

COMPOSE="docker compose -f docker-compose.prod.yml"

# Pull DOMAIN / LETSENCRYPT_EMAIL from the VM .env.
if [ -f .env ]; then
    set -a
    . ./.env
    set +a
fi

: "${DOMAIN:?Set DOMAIN in .env (e.g. tokentracker.example.com)}"
EMAIL="${LETSENCRYPT_EMAIL:-}"
STAGING="${STAGING:-0}"
cert_path="/etc/letsencrypt/live/$DOMAIN"

echo "### Pulling images ..."
$COMPOSE pull

echo "### Creating a temporary self-signed certificate for $DOMAIN ..."
$COMPOSE run --rm --entrypoint sh certbot -c \
    "mkdir -p '$cert_path' && \
     openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
       -keyout '$cert_path/privkey.pem' \
       -out    '$cert_path/fullchain.pem' \
       -subj '/CN=localhost'"

echo "### Starting nginx with the temporary certificate ..."
$COMPOSE up -d --force-recreate nginx

echo "### Removing the temporary certificate ..."
$COMPOSE run --rm --entrypoint sh certbot -c \
    "rm -rf /etc/letsencrypt/live/$DOMAIN \
            /etc/letsencrypt/archive/$DOMAIN \
            /etc/letsencrypt/renewal/$DOMAIN.conf"

echo "### Requesting the Let's Encrypt certificate for $DOMAIN ..."
if [ -n "$EMAIL" ]; then
    email_arg="--email $EMAIL"
else
    email_arg="--register-unsafely-without-email"
fi
if [ "$STAGING" != "0" ]; then
    staging_arg="--staging"
else
    staging_arg=""
fi

# shellcheck disable=SC2086
$COMPOSE run --rm --entrypoint certbot certbot certonly \
    --webroot -w /var/www/certbot \
    $staging_arg $email_arg \
    -d "$DOMAIN" \
    --rsa-key-size 2048 \
    --agree-tos \
    --non-interactive \
    --force-renewal

echo "### Reloading nginx ..."
$COMPOSE exec nginx nginx -s reload

echo "### Done. https://$DOMAIN should now serve a valid certificate."
