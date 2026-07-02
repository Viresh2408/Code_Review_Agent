# Webhook Tunnel Setup

GitHub needs a public URL to deliver webhooks. In local development you need
a tunnel that forwards requests from a public URL to `localhost:8000`.

---

## Option A — smee.io (Recommended, zero install)

**smee.io** is GitHub's own webhook proxy service. No account required.

### Step 1: Get a channel URL

```
https://smee.io/new
```

Click the link → a new channel is created. Copy the URL, e.g.  
`https://smee.io/abc123XYZ`

### Step 2: Install the smee client

```powershell
# Requires Node.js (npx is bundled with npm)
npx smee-client --url https://smee.io/abc123XYZ --target http://localhost:8000/webhooks/github
```

Or install globally for convenience:
```powershell
npm install -g smee-client
smee --url https://smee.io/abc123XYZ --target http://localhost:8000/webhooks/github
```

### Step 3: Set the GitHub App webhook URL

Set the webhook URL on your GitHub App settings page to:
```
https://smee.io/abc123XYZ
```

> ℹ️ smee.io acts as an intermediary: GitHub → smee.io → your local backend.
> The `X-Hub-Signature-256` header is forwarded intact, so HMAC verification works.

---

## Option B — ngrok

**ngrok** gives you a real HTTPS tunnel directly to your machine.
Requires a free account at https://ngrok.com.

### Step 1: Install ngrok

```powershell
# winget
winget install ngrok

# or: Chocolatey
choco install ngrok

# or: download from https://ngrok.com/download
```

### Step 2: Authenticate (one-time)

```powershell
ngrok config add-authtoken YOUR_NGROK_TOKEN
```

### Step 3: Start the tunnel

```powershell
ngrok http 8000
```

Copy the `https://` forwarding URL shown in the terminal, e.g.:  
`https://abc123.ngrok-free.app`

### Step 4: Set the GitHub App webhook URL

```
https://abc123.ngrok-free.app/webhooks/github
```

> ⚠️ On the free tier, the ngrok URL changes every time you restart. 
> You'll need to update the GitHub App webhook URL each session.
> Use a paid ngrok plan or smee.io to avoid this.

---

## Testing the Tunnel

Once the backend is running and the tunnel is up:

```powershell
# Should return {"status": "ok"}
curl http://localhost:8000/health

# Trigger a test payload manually (replace URL and secret)
$body = '{"action":"opened","pull_request":{"number":1,"head":{"sha":"abc123"},"title":"Test","user":{"login":"you"},"changed_files":1,"additions":10,"deletions":0},"repository":{"full_name":"you/test-repo","id":12345},"installation":{"id":99999}}'
$sig = [System.Security.Cryptography.HMACSHA256]::new([System.Text.Encoding]::UTF8.GetBytes("your-webhook-secret")).ComputeHash([System.Text.Encoding]::UTF8.GetBytes($body)) | ForEach-Object { $_.ToString("x2") }
# (Use Postman or httpie for easier manual testing)
```
