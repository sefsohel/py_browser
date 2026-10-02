# PyBrowser Cloud — Real Chromium Browser for Railway

This project is a **real Chromium browser running inside a Linux container** and streamed to your normal web browser using **noVNC**.

It is deliberately different from a URL-fetching proxy:

- Chromium executes JavaScript and renders modern HTML/CSS.
- The browser has a real graphical window, tabs, address bar, extensions/settings, downloads, cookies, local storage, etc.
- Your computer/phone controls the Chromium desktop through noVNC.
- Python 3.11 provides the service health/API layer.
- Nginx provides the public HTTP/WebSocket entry point used by Railway.

## Architecture

```text
Your browser
     │ HTTPS / WebSocket
     ▼
Railway public service
     │
     ├── nginx : $PORT
     │      ├── /vnc.html  → noVNC
     │      ├── /websockify ──WebSocket──┐
     │      └── /healthz → Python 3.11  │
     │                                   ▼
     │                              websockify
     │                                   │ TCP
     │                                   ▼
     │                                x11vnc
     │                                   │
     │                              X display :99
     │                                   │
     │                                   ▼
     │                                Chromium
     └────────────────────────────────────────────
```

## Railway deployment

Railway automatically detects a root-level file named `Dockerfile` and builds the service from it.

### 1. Put this project in GitHub

Create a repository and upload everything in this directory.

### 2. Create a Railway project

In Railway:

1. Create a new project.
2. Deploy the GitHub repository as a service.
3. Railway will detect `Dockerfile` automatically.
4. Open the service's **Variables** tab.
5. Add:

```text
VNC_PASSWORD=Your8CharPassword
```

Use up to 8 characters because classic x11vnc password storage is limited to 8 characters in this configuration.

`PORT` is supplied by Railway automatically; do not hard-code a public port in Railway.

### 3. Generate the public domain

In the service settings, open **Networking** and generate a public domain.

Open that HTTPS domain. You should see noVNC and the Chromium desktop.

Enter the `VNC_PASSWORD` when prompted.

### 4. Optional persistent browser profile

Railway's container filesystem is ephemeral. This project stores the Chromium profile in `/data/chromium` and downloads in `/data/downloads`.

For persistence, attach a Railway Volume to the service with mount path:

```text
/data
```

The Chromium profile, cookies, local storage and downloads can then survive deployment/restart according to Railway's volume behavior.

## Local Docker test

Build:

```bash
docker build -t pybrowser-cloud .
```

Run:

```bash
docker run --rm -it \
  -p 8080:8080 \
  -e PORT=8080 \
  -e VNC_PASSWORD=browser1 \
  pybrowser-cloud
```

Then open:

```text
http://localhost:8080
```

## What makes this a real browser?

The browser engine is **Chromium itself**, not Python's HTML parser and not a server-side `requests`/`BeautifulSoup` page fetch.

The graphical Chromium process renders a normal desktop session inside Xvfb. x11vnc exposes that desktop, websockify converts the VNC TCP connection to WebSockets, and noVNC displays/control it in your existing browser.

## Important production notes

### Security

This exposes a real browser session to the public internet. Keep the Railway URL private or protect the service with an additional authentication layer if other people should not be able to access it.

The VNC password protects the actual VNC connection. Do not commit your password to GitHub.

### One shared browser session

The default project starts **one Chromium session per Railway service/replica**. Multiple simultaneous visitors can therefore interact with the same browser desktop.

For multi-user production use, the architecture should be changed to create an isolated Chromium + X display + VNC session per authenticated user.

### Browser capabilities

Because this is actual Chromium, normal browser technologies can run inside Chromium. However, the browser is still operating inside a remote Linux container, so some capabilities that depend on the physical client device (for example direct access to local hardware) may not behave like a locally installed Chrome application.

## Files

```text
pybrowser-railway/
├── Dockerfile
├── app.py
├── requirements.txt
├── railway.json
├── .dockerignore
├── .env.example
├── nginx/
│   └── default.conf.template
└── scripts/
    └── entrypoint.sh
```

## Railway references

- https://docs.railway.com/builds/dockerfiles
- https://docs.railway.com/deployments/healthchecks
- https://docs.railway.com/volumes
- https://novnc.com/noVNC/docs/EMBEDDING.html
