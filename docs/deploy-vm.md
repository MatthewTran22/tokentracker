# Deploying tokentracker to the Oracle Cloud VM

This describes the **one-time** setup for the Oracle Cloud Always-Free VM
(`VM.Standard.E2.1.Micro`, 1 OCPU / 1 GB RAM). After this is done, every push
to `main` deploys automatically via `.github/workflows/deploy.yml`.

## How the pipeline works

1. `ci.yml` runs `pytest` on every push/PR.
2. On push to `main`, `deploy.yml`:
   - re-runs the tests,
   - builds the Docker image **on the GitHub runner** (never on the VM) and
     pushes it to `ghcr.io/matthewtran22/tokentracker`,
   - SSHes to the VM, which `git pull`s the latest `docker-compose.prod.yml`,
     `nginx/`, and `sql/`, **pulls** the new image, applies idempotent
     migrations, rolls the `app` container, and refreshes nginx + certbot.

The VM only ever pulls a prebuilt image — the 1 GB box never compiles anything.

**Topology / what's exposed:** only **nginx** is published to the host
(ports 80 + 443). It terminates TLS and reverse-proxies to `app:8000` over a
private Docker network. The `app` and `db` have **no host ports**; the `db` is
on an `internal: true` network with no internet access, reachable only by the
app. TLS (443) exists only in `docker-compose.prod.yml` — never in dev.

## 1. GitHub repository secrets

Settings → Secrets and variables → Actions → **New repository secret**:

| Secret | Value |
|--------|-------|
| `SSH_HOST` | The VM's public IP |
| `SSH_USER` | VM login user — `ubuntu` (Ubuntu image) or `opc` (Oracle Linux) |
| `SSH_PRIVATE_KEY` | Private key (full PEM, incl. header/footer) whose public half is in the VM's `~/.ssh/authorized_keys` |

GHCR auth uses the built-in `GITHUB_TOKEN` — no PAT secret required.

## 2. Open the ports (80 + 443)

Open **both** 80 (ACME challenge + HTTP→HTTPS redirect) and 443 (TLS). The app
port 8000 is **not** exposed — it lives behind nginx on the private network.

- **Oracle console:** VCN → the subnet's Security List (or NSG) → add ingress
  rules allowing TCP **80** and TCP **443** from `0.0.0.0/0`.
- **On the VM host firewall:**
  - Ubuntu (Oracle Ubuntu images ship with restrictive iptables):
    ```bash
    sudo iptables -I INPUT 6 -p tcp --dport 80  -j ACCEPT
    sudo iptables -I INPUT 6 -p tcp --dport 443 -j ACCEPT
    sudo netfilter-persistent save
    ```
  - Oracle Linux:
    ```bash
    sudo firewall-cmd --permanent --add-port=80/tcp --add-port=443/tcp
    sudo firewall-cmd --reload
    ```

## 3. Add swap (strongly recommended on 1 GB)

```bash
sudo fallocate -l 1G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 4. Install Docker + Compose

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"   # log out/in afterwards so `docker` works without sudo
```

(`docker compose` v2 ships with the Docker engine above.)

## 5. Clone the repo

The deploy step runs `git pull` on the VM to sync `docker-compose.prod.yml`
and `sql/`. The VM therefore needs **read** access to the repo:

```bash
git clone https://github.com/MatthewTran22/tokentracker.git ~/tokentracker
cd ~/tokentracker
git checkout main
```

- If the repo is **public**, HTTPS pulls work with no credentials.
- If it's **private**, add a read-only deploy key to the repo and clone via
  SSH (`git@github.com:MatthewTran22/tokentracker.git`), or cache an HTTPS PAT.

## 6. Create the VM `.env`

This file lives only on the VM (it's gitignored) and is read by
`docker-compose.prod.yml` for both variable interpolation and the app's
`env_file`:

```bash
cd ~/tokentracker
cat > .env <<'EOF'
AUTH_SECRET=<openssl rand -hex 32>
POSTGRES_PASSWORD=<a strong password>

# TLS / reverse proxy (nginx + Let's Encrypt):
DOMAIN=tokentracker.example.com      # the hostname the cert is issued for
LETSENCRYPT_EMAIL=you@example.com    # renewal/expiry notices (optional)

# Behind nginx terminating TLS:
COOKIE_SECURE=1   # Secure cookies + HSTS + hides /docs (valid only behind HTTPS)
TRUST_PROXY=1     # safe now: the app is only reachable via nginx, which sets X-Real-IP

# Single long-lived VM talking directly to local Postgres (not a pooler):
DB_DISABLE_PREPARE=0
DB_POOL_MAX=10
EOF
chmod 600 .env
```

`DOMAIN` drives both the nginx `server_name`/cert paths and certbot issuance.
`TRUST_PROXY=1` is safe here because the app has no host port — every request
arrives through nginx, which sets `X-Real-IP`.

## 7. First deploy (app + db)

Push to `main` (or re-run the Deploy workflow). The first run creates a fresh
`db_data` volume, so `sql/001..003` run automatically via
`docker-entrypoint-initdb.d`; the migration loop then re-applies them
harmlessly (they're idempotent). `nginx` will start but **won't serve until TLS
is bootstrapped** (next step) because its 443 block needs a certificate.

Sanity-check the app over the private network from the VM:

```bash
cd ~/tokentracker
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml exec app \
  python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/').status)"
```

## 8. Bootstrap TLS (one-time)

Prerequisites: `DOMAIN` is set in `.env`, a **DNS A record for `DOMAIN` points
at the VM's public IP**, and ports 80/443 are open (step 2). Then, on the VM:

```bash
cd ~/tokentracker

# Optional but recommended: dry-run against Let's Encrypt staging first so a
# misconfiguration doesn't burn the real rate limit.
STAGING=1 sh nginx/init-letsencrypt.sh

# Real certificate:
sh nginx/init-letsencrypt.sh
```

The script issues the cert (HTTP-01 webroot) and reloads nginx. From then on
the `certbot` sidecar renews automatically and nginx reloads every 6h — you do
**not** run this again. Verify:

```bash
curl -fsS https://$DOMAIN/        # 200, valid cert
curl -fsS http://$DOMAIN/ -I      # 301 redirect to https
```

## Troubleshooting

- **`denied` / `unauthorized` pulling the image on the VM (first deploy):**
  GHCR may not have linked the new package to the repo yet. In GitHub →
  your profile → Packages → `tokentracker` → Package settings, confirm it's
  linked to this repo (so `GITHUB_TOKEN` can read it), or set the package to
  public.
- **OOM / container killed:** confirm the swapfile is active (`swapon --show`)
  and lower `mem_limit` in `docker-compose.prod.yml` if needed.
- **`git pull` fails on the VM:** the repo is private and the VM lacks
  credentials — see step 5.
- **Migrations fail:** they run with `ON_ERROR_STOP=1`; read the failing
  `psql` output in the Actions deploy log. Because the files are idempotent,
  a fixed file can simply be redeployed.
- **nginx keeps restarting / "cannot load certificate":** TLS hasn't been
  bootstrapped yet (or the cert was removed). Run `sh nginx/init-letsencrypt.sh`
  (see step 8). Check logs with
  `docker compose -f docker-compose.prod.yml logs nginx`.
- **certbot issuance fails:** confirm the DNS A record resolves to this VM
  (`dig +short $DOMAIN`) and port 80 is reachable from the internet. Re-run with
  `STAGING=1` to debug without hitting Let's Encrypt's rate limits.
- **Cert won't renew:** check the sidecar with
  `docker compose -f docker-compose.prod.yml logs certbot`. Manual renew:
  `docker compose -f docker-compose.prod.yml run --rm --entrypoint certbot certbot renew`.
