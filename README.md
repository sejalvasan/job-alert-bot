# Job Alert Bot → WhatsApp

Checks company career boards every 30 minutes and sends a WhatsApp message when a new
PM / APM / TPM / AI PM / FDE **internship** is posted. It runs free on GitHub, so your laptop
can be closed.

```
🚨 New role

*Associate Product Manager Intern*
Coinbase · Remote - USA
Posted 3h ago
https://...
```

**How it works:** GitHub runs `job_alert.py` on a schedule → it reads each company's public
job feed (Greenhouse, Lever, Ashby, Workday, Amazon) → keeps titles that match your filters →
compares against `state.json` (jobs already sent) → messages you only about new ones.

What you'll get:
- **One alert per new role**, or a single digest if more than 5 arrive at once.
- **A catch-up digest** of roles already open whenever you add a company, so the first run doesn't send dozens of messages.
- **A daily "✅ bot is running" message** after 9 AM ET. If it stops arriving, something broke. It also names any board that keeps failing.

---

## Setup (about 15 minutes)

### 1. Get your WhatsApp key (CallMeBot, free for personal use)
1. Save **+34 644 26 33 77** in your phone's contacts (name it "CallMeBot").
   *The number occasionally changes. The current one is always at
   https://www.callmebot.com/blog/free-api-whatsapp-messages/*
2. On WhatsApp, send it: `I allow callmebot to send me messages`
3. It replies with your **API key**. Keep it. If nothing arrives within 2 minutes, CallMeBot
   says to try again after 24 hours.

### 2. Put the code on GitHub
1. Create a new **private** repo at https://github.com/new (e.g. `job-alert-bot`).
2. Upload every file in this folder, **including the hidden `.github` folder**. The easiest way is the terminal:
   ```bash
   cd job-alert-bot
   git init && git add . && git commit -m "job alert bot"
   git branch -M main
   git remote add origin https://github.com/<you>/job-alert-bot.git
   git push -u origin main
   ```

### 3. Add your secrets
Go to the repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Name | Value |
|---|---|
| `WHATSAPP_PHONE` | your number with country code, e.g. `+16035551234` |
| `CALLMEBOT_APIKEY` | the key from step 1 |

### 4. Allow the bot to save its memory
**Settings → Actions → General → Workflow permissions → "Read and write permissions"** → Save.

### 5. First run
**Actions** tab → **Job alert** → **Run workflow**. Within a minute you should get the
catch-up digest on WhatsApp. Then open the run log: every company should show a job count.
If a company shows `!` (an error), fix or remove it in `config.yaml`. The three Workday
companies (NVIDIA, Salesforce, Adobe) are the ones I couldn't test from here.

From then on it runs by itself every 30 minutes.

---

## Customising

Everything is in **`config.yaml`**. Edit it on GitHub (click the file → ✏️) and commit.

**Add a company:** open its careers page, click any job, and look at the URL:

| URL looks like | Add this line |
|---|---|
| `job-boards.greenhouse.io/figma/...` | `- { name: Figma, source: greenhouse, board: figma }` |
| `jobs.lever.co/palantir/...` | `- { name: Palantir, source: lever, board: palantir }` |
| `jobs.ashbyhq.com/openai/...` | `- { name: OpenAI, source: ashby, board: openai }` |
| `nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/...` | `- { name: NVIDIA, source: workday, host: nvidia.wd5.myworkdayjobs.com, tenant: nvidia, site: NVIDIAExternalCareerSite }` |

Many companies with their own careers domain (e.g. stripe.com/jobs) still use one of these
systems behind it. The "Apply" link or the page source usually shows `greenhouse`, `lever` or `ashby`.

**Change what matches:** edit `filters.roles`, `levels`, `exclude_titles`, `exclude_locations`.
To add SWE internships later, uncomment `'software engineer'`.

**Check faster:** in `.github/workflows/job-alert.yml`, change `*/30` to `*/10`. That only
stays free if the repo is **public**; private repos get 2,000 free minutes a month.
GitHub sometimes starts scheduled runs 5–15 minutes late.

## Not covered
- **LinkedIn, Indeed, Handshake:** scraping them is against their terms and gets blocked quickly. Use their built-in alerts instead.
- **Google, Meta, Apple, Microsoft:** no simple public feed. Turn on job alerts on their careers sites.

## Optional: Telegram as a backup channel
CallMeBot is a free shared service and occasionally slow. To also get Telegram messages:
message **@BotFather** → `/newbot` → copy the token. Send your new bot any message, then open
`https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id`. Add both as secrets
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

## Running locally (optional)
```bash
pip install -r requirements.txt
python job_alert.py --check        # does every board respond?
python job_alert.py --dry-run      # show matches, send nothing
WHATSAPP_PHONE=+1... CALLMEBOT_APIKEY=... python job_alert.py --test-notify
python -m pytest -q                # offline tests
```
