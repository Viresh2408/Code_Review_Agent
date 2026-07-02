# GitHub App Setup — Step-by-Step

> **Time required:** ~5 minutes. This is a one-time manual step.

---

## 1. Create the GitHub App

1. Go to: **https://github.com/settings/apps/new**
   _(or, for an org: `https://github.com/organizations/YOUR_ORG/settings/apps/new`)_

2. Fill in the form:

   | Field | Value |
   |---|---|
   | **GitHub App name** | `code-review-agent-dev` (must be globally unique) |
   | **Homepage URL** | `http://localhost:8000` |
   | **Webhook URL** | _(leave blank for now — fill after setting up the tunnel in step 2 below)_ |
   | **Webhook secret** | Generate a strong random string: `openssl rand -hex 32` — **save this value** |

---

## 2. Set Permissions

Under **Repository permissions**, set:

| Permission | Access |
|---|---|
| **Contents** | Read-only |
| **Pull requests** | Read & write |
| **Checks** | Read & write |
| **Metadata** | Read-only _(mandatory, auto-granted)_ |

---

## 3. Subscribe to Webhook Events

Under **Subscribe to events**, check:
- ✅ **Pull request**

---

## 4. Finalize Creation

- **Where can this GitHub App be installed?** → Select **Only on this account** (for dev/portfolio)
- Click **Create GitHub App**

---

## 5. Generate & Download the Private Key

On the app's settings page (after creation):

1. Scroll to **Private keys**
2. Click **Generate a private key**
3. A `.pem` file will download automatically
4. Move it to: `secrets/github-app.pem` in this repo root
   ```powershell
   # Windows PowerShell
   Move-Item "$env:USERPROFILE\Downloads\code-review-agent-dev.*.private-key.pem" `
             "c:\Project\Code_Review_Agent\secrets\github-app.pem"
   ```

> ⚠️ `secrets/` is gitignored. Never commit the `.pem` file.

---

## 6. Fill in `.env`

Copy the values from the GitHub App settings page:

```env
GITHUB_APP_ID=<App ID shown at the top of the settings page>
GITHUB_APP_SLUG=code-review-agent-dev
GITHUB_PRIVATE_KEY_PATH=secrets/github-app.pem
GITHUB_WEBHOOK_SECRET=<the secret you generated in step 2>
```

---

## 7. Set the Webhook URL

After setting up the tunnel (see `webhook-tunnel-setup.md`):

1. Go back to the App settings: **https://github.com/settings/apps/YOUR_APP_SLUG**
2. Set **Webhook URL** to your smee.io or ngrok URL + `/webhooks/github`
   - smee: `https://smee.io/YOUR_CHANNEL_ID`
   - ngrok: `https://abc123.ngrok-free.app/webhooks/github`
3. Click **Save changes**

---

## 8. Install the App on a Test Repo

1. Go to: **https://github.com/settings/apps/YOUR_APP_SLUG/installations**
2. Click **Install**
3. Choose the test repository where you'll open PRs

---

## Verify

Open a test PR in the installed repo. You should see in your backend logs:

```
INFO  webhook_received  repo=owner/repo pr_number=1 action=opened ...
INFO  webhook_task_enqueued  task_id=pr-review-owner-repo-1-abc12345
```

**🎉 Phase 0 milestone achieved.**
