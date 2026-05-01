# MHDE Dashboard — VPS Deployment

The MHDE dashboard is a Streamlit app backed by DuckDB. This guide covers deployment
to a VPS accessible via a DuckDNS domain with HTTPS.

## Architecture

```
DuckDNS domain
  → HTTPS (Caddy or Nginx + Certbot)
    → localhost:8501 (Streamlit)
      → data/mhde.duckdb (DuckDB file)
```

Streamlit is never exposed directly to the public internet. All traffic goes through
the reverse proxy which handles TLS termination.

---

## 1. Install project on VPS

```bash
cd /opt
git clone <your-repo-url> mhde
cd mhde
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install streamlit altair
```

---

## 2. Create mhde system user

```bash
sudo useradd -r -s /bin/false mhde
sudo chown -R mhde:mhde /opt/mhde
```

---

## 3. Create .env from template

```bash
cp deploy/dashboard/.env.example deploy/dashboard/.env
# Edit with your values:
nano deploy/dashboard/.env
```

Generate a password hash:
```bash
python3 -c "import hashlib; print(hashlib.sha256(b'your-password').hexdigest())"
```

---

## 4. Configure DuckDNS

1. Go to https://www.duckdns.org and create a subdomain.
2. Copy your token to `.env` as `DUCKDNS_TOKEN`.
3. Set `DUCKDNS_DOMAIN` to your subdomain name (without `.duckdns.org`).
4. Test the update script:
   ```bash
   source deploy/dashboard/.env
   bash deploy/dashboard/duckdns-update.sh
   ```
5. Set up automatic updates every 5 minutes:
   ```bash
   (crontab -l 2>/dev/null; echo "*/5 * * * * /opt/mhde/deploy/dashboard/duckdns-update.sh") | crontab -
   ```

---

## 5. Start the systemd service

```bash
# Update WorkingDirectory and ExecStart paths in the service file if needed
sudo cp deploy/dashboard/mhde-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable mhde-dashboard
sudo systemctl start mhde-dashboard
sudo systemctl status mhde-dashboard
```

---

## 6. Configure reverse proxy

### Option A: Caddy (recommended — automatic HTTPS)

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install caddy

sudo nano /etc/caddy/Caddyfile
# Paste contents of deploy/dashboard/Caddyfile.example with your domain
sudo systemctl reload caddy
```

### Option B: Nginx + Certbot

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo cp deploy/dashboard/nginx.example.conf /etc/nginx/sites-available/mhde
# Edit /etc/nginx/sites-available/mhde: replace your-subdomain.duckdns.org
sudo ln -s /etc/nginx/sites-available/mhde /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d your-subdomain.duckdns.org
```

---

## 7. Confirm dashboard is accessible

```bash
# Test locally
curl http://127.0.0.1:8501

# Test via domain (after DNS propagation)
curl https://your-subdomain.duckdns.org
```

Open https://your-subdomain.duckdns.org in your browser. You should see the login page.

---

## 8. Run the pipeline

```bash
cd /opt/mhde
source .venv/bin/activate
source deploy/dashboard/.env
python main.py run daily-radar
```

---

## Troubleshooting

```bash
# Service status
sudo systemctl status mhde-dashboard
sudo journalctl -u mhde-dashboard -f

# Direct Streamlit test
curl http://127.0.0.1:8501

# Caddy logs
sudo journalctl -u caddy -f

# Nginx logs
sudo tail -f /var/log/nginx/error.log

# Verify DuckDNS resolves to your VPS IP
dig your-subdomain.duckdns.org

# Check firewall
sudo ufw status
# Allow HTTP/HTTPS if needed:
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
```

---

## Security Notes

- Dashboard auth is enabled by default (`MHDE_DASHBOARD_AUTH_ENABLED=true`).
- Never disable auth unless running locally only.
- The dashboard may contain sensitive watchlists, LLM outputs, and internal scoring logic.
- Do not commit `.env` to git. It contains secrets.
- Use strong passwords. The hash must be `sha256(password)`.
