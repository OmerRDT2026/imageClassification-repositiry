# Deploying to a VPS — Step by Step

This assumes a fresh Ubuntu 22.04+ VPS (DigitalOcean, Hetzner, and Linode
all offer this). Recommended starting size: 4 vCPU / 8GB RAM / 100GB SSD —
this app is memory-bound (classification has hit 700MB-1GB+ single
allocations), so RAM is the number to bump up first if you outgrow this.

## 1. Provision the VPS and connect

Create the droplet/server through your provider's dashboard, then:

```bash
ssh root@YOUR_SERVER_IP
```

## 2. Install Docker, Docker Compose, git, and Git LFS

```bash
apt update && apt upgrade -y
curl -fsSL https://get.docker.com | sh
apt install -y docker-compose-plugin git git-lfs nginx
git lfs install
```

## 3. Clone the repo (Git LFS pulls the seed images automatically)

```bash
cd /opt
git clone https://github.com/OmerRDT2026/imageClassification-repositiry.git multispectral-app
cd multispectral-app
```

Copy `Dockerfile` and `docker-compose.yml` (provided alongside this guide)
into this same folder, at the project root — next to `backend/`,
`frontend/`, `index.html`.

## 4. Create the persisted upload/output folders

```bash
mkdir -p backend/uploads backend/converted
```

These get volume-mounted (see `docker-compose.yml`) so they survive
container restarts and rebuilds.

## 5. Build and start the app container

```bash
docker compose up -d --build
```

Check it's actually running:

```bash
docker compose logs -f
```

You should see the same `Starting Flask server...` output you're used to
seeing locally, just running inside the container now. `Ctrl+C` exits the
log view without stopping the container.

At this point the app is running on `127.0.0.1:5000` *inside the server*,
not yet reachable from the internet — that's what nginx is for next.

## 6. Configure nginx as the reverse proxy

Copy `nginx_multispectral-app.conf` (provided) to the server:

```bash
cp nginx_multispectral-app.conf /etc/nginx/sites-available/multispectral-app
```

Edit it and replace `YOUR_DOMAIN_OR_IP` with your actual domain (if you
have one pointed at this server) or just the server's IP address if not.

```bash
nano /etc/nginx/sites-available/multispectral-app
```

## 7. Set up basic auth (recommended for the POC/testing phase)

This puts a username/password prompt in front of the whole app, so it's
not sitting wide open to anyone on the internet while you're just letting
specific test clients try it:

```bash
apt install -y apache2-utils
htpasswd -c /etc/nginx/.htpasswd testclient
```

You'll be prompted to set a password. Add more users the same way, without
`-c` (which would overwrite the file):

```bash
htpasswd /etc/nginx/.htpasswd anotheruser
```

## 8. Enable the nginx site and reload

```bash
ln -s /etc/nginx/sites-available/multispectral-app /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx
```

`nginx -t` checks the config for errors before reloading — if it reports a
problem, fix it before running `systemctl reload nginx`.

At this point, visiting `http://YOUR_SERVER_IP` (or your domain) in a
browser should prompt for the basic-auth login, then show the app.

## 9. (Recommended) Add free HTTPS with Let's Encrypt

Only possible if you have a real domain name pointed at this server (an
A record → your server's IP) — Let's Encrypt can't issue a certificate for
a bare IP address.

```bash
apt install -y certbot python3-certbot-nginx
certbot --nginx -d yourdomain.com
```

Follow the prompts. Certbot edits the nginx config automatically to
redirect HTTP to HTTPS and sets up auto-renewal.

## Updating the app after future code changes

```bash
cd /opt/multispectral-app
git pull
docker compose up -d --build
```

This rebuilds the image with whatever's newest on GitHub and restarts the
container. Uploaded files and results in `backend/uploads`/`backend/converted`
are untouched, since they're volume-mounted, not baked into the image.

## Known limitation for this POC setup

The cached `iap_model_*.pkl` files are **not** persisted across container
rebuilds (only `uploads/` and `converted/` are) — this was a deliberate
simplicity tradeoff to avoid touching `app.py`'s path handling further.
Practical effect: after every `docker compose up -d --build`, the *first*
classification of each previously-tested image will retrain from scratch
(slower, not broken) before caching again. If this becomes annoying, the
fix is to also volume-mount a dedicated model-cache directory — worth
doing once this moves past POC.

## Checking logs / debugging

```bash
docker compose logs -f          # live tail
docker compose logs --tail 200  # last 200 lines
docker compose restart          # restart without rebuilding
docker compose down && docker compose up -d --build   # full rebuild
```
