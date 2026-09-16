# Krishi Sakhi: WhatsApp AI helper for women farmers

A WhatsApp chatbot that understands **text, voice notes and crop photos** in **Gujarati, Hindi and English**, and replies in the farmer's own language (voice questions get voice answers).

This README covers:

1. [How it works](#1-how-it-works)
2. [Prerequisites: accounts, numbers and API keys (and where to get each one)](#2-prerequisites)
3. [The `.env` file: what to put in it](#3-the-env-file)
4. [Build the backend with Claude Code](#4-build-the-backend-with-claude-code)
5. [Run on your laptop and connect WhatsApp](#5-run-locally-and-connect-whatsapp)
6. [Deploy online](#6-deploy-online)
7. [Go-live checklist](#7-go-live-checklist)
8. [Costs](#8-costs-approximate)
9. [Troubleshooting](#9-troubleshooting)
10. [Project structure](#10-project-structure)

---

## 1. How it works

```
Farmer's WhatsApp
      │  (text / voice note / photo)
      ▼
Meta WhatsApp Cloud API ──POST──► YOUR BACKEND  /webhook   (FastAPI, always online)
                                     │
                                     ├─ download voice/photo from Meta
                                     ├─ voice → text ............ Google Gemini (audio understanding)
                                     ├─ think + answer .......... Google Gemini
                                     ├─ text → voice ............ Google Gemini TTS + ffmpeg
                                     ├─ memory, limits, logs .... Redis
                                     │
Meta WhatsApp Cloud API ◄──POST── send reply (text or voice note)
      │
      ▼
Farmer's WhatsApp
```

- **Meta** forwards every farmer message to your **webhook URL**.
- Your **backend** does all the work and sends the reply back through Meta's **send API**.
- No n8n or other workflow tool is needed: the backend code connects everything.

---

## 2. Prerequisites

### 2.1 Things to install on your computer

| Tool | Why | How to get it |
|---|---|---|
| Python 3.11+ | runs the backend | python.org/downloads |
| ffmpeg | converts voice notes | Windows: `winget install ffmpeg` · Mac: `brew install ffmpeg` · Linux: `sudo apt install ffmpeg` |
| Git + GitHub account | store the code, deploy | git-scm.com, github.com |
| Claude Code | builds the backend for you | docs.claude.com → Claude Code (needs a Claude Pro/Max plan or an Anthropic API account) |
| ngrok | gives your laptop a public HTTPS link for testing | ngrok.com → sign up → install → `ngrok config add-authtoken <token>` |
| Docker Desktop (optional) | test the production image locally | docker.com |

### 2.2 Meta (WhatsApp) setup

You need 5 values from Meta: **Access token, Phone number ID, WhatsApp Business Account ID, App secret**, and a **Verify token** you invent yourself.

**Step A: Meta Business account (business portfolio)**
1. Go to **business.facebook.com** and log in with a Facebook account (use an organisation admin's account, not a random personal one).
2. Create a business portfolio with your organisation's legal name, address and website.
3. Go to **Business Settings → Security Centre → Start verification**. Upload documents such as the GST certificate, registration certificate / trust deed / NGO certificate, or a utility bill in the organisation's name.
   - Verification can take from 1 day to 2 weeks. **Start this first.** You can build and test while waiting.

**Step B: Developer app**
1. Go to **developers.facebook.com → My Apps → Create App**.
2. Choose use case **"Other" → app type "Business"** (or the WhatsApp use case if shown), give it a name, and link your business portfolio.
3. In the app dashboard, click **Add product → WhatsApp → Set up**.

**Step C: Test with Meta's free test number (right away)**
1. Open **WhatsApp → API Setup**. Meta gives you a **test phone number** and a **temporary access token** (valid 24 hours).
2. In "To", add up to 5 of your own phone numbers and verify them with the OTP.
3. Note the **Phone number ID** and **WhatsApp Business Account ID** shown on this page.

**Step D: Your real business phone number**
- Requirements:
  - A new SIM or a landline that can receive an SMS or voice call for the OTP.
  - It must **not** be registered on the normal WhatsApp or WhatsApp Business app. If it is, delete that WhatsApp account first.
  - Keep the SIM active and safe; you may need it again for verification.
- In **WhatsApp → API Setup → Add phone number**:
  - Enter the **display name** (e.g. "Krishi Sakhi"). It should relate to your organisation's name or brand, or it can be rejected.
  - Choose a category (e.g. Non-profit / Education), verify the OTP, and set a **6-digit two-step PIN** (save it).
- Copy the new number's **Phone number ID**. This is different from the test number's ID.
- In **WhatsApp Manager → Payment settings**, add a payment method (card or credit line), because replies are charged.

**Step E: Permanent access token (the 24-hour token is only for testing)**
1. Go to **business.facebook.com → Business Settings → Users → System users → Add**. Name it `krishi-bot` and set the role to **Admin**.
2. Click **Assign assets**:
   - Apps → your app → Full control.
   - WhatsApp accounts → your account → Full control.
3. Click **Generate token**, then:
   - Select your app and set expiry to **Never**.
   - Tick the permissions `whatsapp_business_messaging` and `whatsapp_business_management`.
4. Copy the token immediately; it is shown only once. This is `WA_ACCESS_TOKEN`.

**Step F: App secret and verify token**
- **App secret**: go to developers.facebook.com → your app → **App settings → Basic → App secret → Show**. This is `WA_APP_SECRET`.
- **Verify token**: invent any long random string (e.g. `ks-verify-8f3a91c2d7`). This is `WA_VERIFY_TOKEN`. You will type the same value into Meta later.
- **Graph API version**: use the latest version shown in your app dashboard (e.g. `v23.0`). This is `GRAPH_API_VERSION`.

**Step G: Privacy policy and going Live**
- In **App settings → Basic**, add a **Privacy Policy URL** (a simple page on your website explaining what farmer data you store and why).
- Switch the app mode from **Development** to **Live** once business verification is approved.

### 2.3 Google Gemini API key (the AI brain)

1. Go to **aistudio.google.com** and sign in with a Google account (ideally an organisation account).
2. Click **Get API key → Create API key**, and choose or create a Google Cloud project.
3. Copy the key. This is `GEMINI_API_KEY`.
4. **Set up billing (recommended before real farmers use it):**
   - In AI Studio, click **Set up billing** next to the key, or add a billing account to that project in console.cloud.google.com.
   - The free tier has low rate limits, and Google's terms treat free-tier and paid-tier data differently. Read the current Gemini API terms and use the paid tier for real farmer conversations.
5. **Choose the model:**
   - Check **ai.google.dev/gemini-api/docs/models** and use the current stable Flash model (at the time of writing, `gemini-3.8-flash`). This is `GEMINI_MODEL`.
   - For lower cost, use a Flash-Lite model.
   - Avoid Gemini 2.x models; they are being shut down.
6. Set a budget alert in **console.cloud.google.com → Billing → Budgets & alerts**.

### 2.4 Voice (Google Gemini, same key)

Voice uses the **same `GEMINI_API_KEY`** as the answers. There is no separate voice account.
1. **Voice → text**: a Gemini model listens to the voice note. This is `GEMINI_STT_MODEL` (the Flash model by default).
2. **Text → voice**: a Gemini text-to-speech model. Check **ai.google.dev/gemini-api/docs/models** for the current TTS model (at the time of writing, `gemini-3.1-flash-tts-preview`). This is `GEMINI_TTS_MODEL`.
3. In **aistudio.google.com → Generate speech**, try a Gujarati and a Hindi sentence and pick a **female voice** you like (e.g. `Sulafat`, `Kore`, `Aoede`, `Leda`). This is `GEMINI_TTS_VOICE`.
4. Check that the voice sounds natural in Gujarati. If a voice reply fails, the bot automatically sends the answer as text instead.

### 2.5 Redis (memory, daily limits, logs)

Choose one:
- **Upstash (easiest, has a free tier):**
  1. Go to upstash.com → Create database → Redis.
  2. Pick the region closest to India (e.g. Mumbai if listed).
  3. Copy the URL that starts with `rediss://`.
- **Railway:** in your Railway project, click **+ New → Database → Redis** and copy `REDIS_URL` from its Variables tab.
- **Local testing:** run `docker run -p 6379:6379 redis`, then use `redis://localhost:6379/0`.

This value is `REDIS_URL`.

### 2.6 Hosting account

- **Railway** (easiest): railway.app, sign in with GitHub, add a payment method (Hobby plan).
- **or Google Cloud Run** (keeps data in India, Mumbai region `asia-south1`): console.cloud.google.com, the same project as Gemini, with billing on. Install the `gcloud` CLI.

### 2.7 Optional (later)

| Service | Use | Where |
|---|---|---|
| Supabase (Postgres) | farmer profiles, knowledge base | supabase.com → New project → Settings → Database → connection string |
| Sentry | error alerts | sentry.io → New project (Python/FastAPI) → copy DSN |

---

## 3. The `.env` file

Create a file named `.env` in the project folder. **Never** upload it to GitHub (it is listed in `.gitignore`).

```env
# ───────── App ─────────
APP_ENV=development              # development | production
LOG_LEVEL=INFO
ADMIN_API_KEY=change-me-long-random-string   # protects /admin/chats

# ───────── WhatsApp (Meta) ─────────
WA_ACCESS_TOKEN=EAAG...          # System User permanent token (Step E)
WA_PHONE_NUMBER_ID=1234567890    # API Setup → Phone number ID (NOT the phone number)
WA_BUSINESS_ACCOUNT_ID=987654321 # API Setup → WhatsApp Business Account ID
WA_APP_SECRET=abc123...          # App settings → Basic → App secret
WA_VERIFY_TOKEN=ks-verify-8f3a91c2d7   # any string you invent; same value goes into Meta
GRAPH_API_VERSION=v23.0          # latest version shown in your Meta app

# ───────── Google Gemini (AI brain + voice) ─────────
GEMINI_API_KEY=AIza...           # aistudio.google.com → Get API key
GEMINI_MODEL=gemini-3.8-flash    # check ai.google.dev/gemini-api/docs/models
GEMINI_THINKING_LEVEL=low        # low = faster & cheaper answers
GEMINI_STT_MODEL=gemini-3.8-flash                # voice note → text
GEMINI_TTS_MODEL=gemini-3.1-flash-tts-preview    # text → voice
GEMINI_TTS_VOICE=Sulafat         # female voice you chose

# ───────── Storage ─────────
REDIS_URL=rediss://default:password@your-db.upstash.io:6379
DATABASE_URL=                    # optional, Supabase Postgres (later)

# ───────── Bot behaviour ─────────
DEFAULT_LANGUAGE=gu-IN           # gu-IN | hi-IN | en-IN
DAILY_MESSAGE_LIMIT=50           # per farmer per day
MAX_VOICE_SECONDS=120            # longer voice notes are politely refused
VOICE_REPLY_ALSO_TEXT=false      # true = send text as well as voice (costs 2 messages)

# ───────── Monitoring (optional) ─────────
SENTRY_DSN=
```

| Variable | Where it comes from |
|---|---|
| `WA_ACCESS_TOKEN` | Section 2.2, Step E |
| `WA_PHONE_NUMBER_ID`, `WA_BUSINESS_ACCOUNT_ID` | Meta app → WhatsApp → API Setup |
| `WA_APP_SECRET` | Meta app → App settings → Basic |
| `WA_VERIFY_TOKEN` | You invent it |
| `GEMINI_API_KEY` | Section 2.3 |
| `REDIS_URL` | Section 2.5 |

---

## 4. Build the backend with Claude Code

1. Create a folder `krishi-sakhi`, and put this `README.md` inside it.
2. Open a terminal in that folder and run `claude`.
3. Paste the full prompt from **`CLAUDE_CODE_PROMPT.md`**.
4. Let Claude Code work through the 5 phases. Review each phase and run the tests when it asks:
   ```bash
   python -m venv .venv
   source .venv/bin/activate          # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   pytest -q
   ```
5. When all tests pass, create `.env` from `.env.example` using Section 3.

---

## 5. Run locally and connect WhatsApp

**5.1 Start the server**
```bash
uvicorn app.main:app --reload --port 8000
```
Open http://localhost:8000/health. It should show `{"ok": true}`.

**5.2 Give it a public HTTPS link**
```bash
ngrok http 8000
```
Copy the `https://xxxx.ngrok-free.app` address. It changes every time you restart ngrok, unless you use a fixed ngrok domain.

**5.3 Connect Meta to your backend**
1. Go to developers.facebook.com → your app → **WhatsApp → Configuration → Webhook → Edit**.
2. Enter:
   - **Callback URL**: `https://xxxx.ngrok-free.app/webhook`
   - **Verify token**: exactly the same as `WA_VERIFY_TOKEN` in `.env`.
3. Click **Verify and save**. Meta calls `GET /webhook`, and your server must answer with the challenge.
4. In **Webhook fields**, click **Subscribe** next to **messages**.

**5.4 Test from your phone**
Send these to the bot number (for the test number, your phone must be in the "To" list):

| Send | Expected |
|---|---|
| `hello` | 3 language buttons |
| tap **ગુજરાતી** | Gujarati welcome message |
| `કપાસમાં સફેદ માખી આવી છે, શું કરું?` | Gujarati text answer |
| Hindi voice note | Hindi voice-note answer |
| photo of a leaf | description + advice |
| `ભાષા` | language buttons again |

Watch the terminal for errors, and check `http://localhost:8000/admin/chats` with the header `X-Admin-Key`.

---

## 6. Deploy online

### Option A: Railway (easiest)
1. Push the code to a **private** GitHub repo. Check that `.env` is not included.
2. Go to railway.app → **New Project → Deploy from GitHub repo** and pick your repo. Railway finds the `Dockerfile` automatically, which also installs ffmpeg.
3. Optional: **+ New → Database → Redis**, then use its URL as `REDIS_URL`.
4. In your service's **Variables** tab, open **Raw Editor** and paste your `.env` contents. Change `APP_ENV=production`.
5. **Settings → Networking → Generate Domain** gives you `https://krishi-sakhi.up.railway.app`.
6. Open `https://krishi-sakhi.up.railway.app/health` and check it works.
7. In Meta, go to **WhatsApp → Configuration**, change the Callback URL to `https://krishi-sakhi.up.railway.app/webhook`, then **Verify and save**.

### Option B: Google Cloud Run (data stays in India)
```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud run deploy krishi-sakhi \
  --source . \
  --region asia-south1 \
  --allow-unauthenticated \
  --min-instances 1 \
  --no-cpu-throttling \
  --memory 1Gi
```
- Add the environment variables in **Cloud Run → your service → Edit & deploy new revision → Variables & Secrets**. Put the keys in **Secret Manager** instead of plain variables.
- `--min-instances 1` and `--no-cpu-throttling` are required, because the reply work runs in the background after the webhook has already answered Meta.
- Use the service URL + `/webhook` as the Meta Callback URL.
- Use Upstash or Memorystore for Redis.

### After deploying
- Send a test message from your phone.
- Set budget alerts in Google Cloud, Railway and Meta.

---

## 7. Go-live checklist

- [ ] Meta business verification approved; app switched to **Live**.
- [ ] Real business number added, display name approved, payment method added.
- [ ] Permanent System User token in use (not the 24-hour token).
- [ ] Webhook points to the deployed URL (not ngrok) and `messages` is subscribed.
- [ ] Gemini billing enabled; budget alerts set.
- [ ] An agronomist has reviewed 30–50 real answers in Gujarati and Hindi.
- [ ] Voice tested with real village recordings (noise, accents, mixed languages).
- [ ] Privacy policy published; farmers told what is stored and why.
- [ ] Farmers onboarded: save the number as "Krishi Sakhi", send "નમસ્તે" once, and a short demo of voice and photo.
- [ ] `ADMIN_API_KEY` changed from the default; `.env` not in GitHub.

---

## 8. Costs (approximate)

For 50 farmers × up to 50 messages/day, which is about 75,000 messages/month at full use:

| Item | Rough monthly cost |
|---|---|
| WhatsApp replies (from 1 Oct 2026, ~₹0.115 each after 1,000 free) | ₹8,500 (one reply per question) to ₹17,000 (voice + text) |
| Gemini Flash | depends on model and answer length; set a budget alert |
| Gemini voice: audio input tokens (voice → text) + TTS audio output tokens (text → voice) | depends on how much farmers use voice; check the Gemini pricing page |
| Railway / Cloud Run + Redis | ₹1,000 – ₹4,000 |

Prices change often. Check each provider's pricing page before budgeting. Real usage is usually far below the maximum.

---

## 9. Troubleshooting

| Problem | Likely cause / fix |
|---|---|
| Meta says "callback URL could not be validated" | Server not running or not public; `WA_VERIFY_TOKEN` differs from what you typed; URL missing `/webhook` |
| Messages arrive but no reply | `messages` field not subscribed; wrong `WA_PHONE_NUMBER_ID`; token expired (use the permanent token); check logs |
| Error 131030 "recipient not in allowed list" | Using the test number: add the phone to the "To" list |
| Error 190 / 401 | Access token wrong or expired |
| Same reply sent twice | Webhook too slow or dedup not working; check Redis connection |
| Voice reply shows as a file, not a voice note | Audio must be OGG with Opus codec, mono |
| Voice questions get "sorry, something went wrong" | ffmpeg missing, or `GEMINI_STT_MODEL` name wrong; check logs |
| Voice questions get a text answer instead of voice | Gemini TTS failed (model name, voice name, or language not supported); check logs for `voice reply failed` |
| Gemini 404 model not found | Model name wrong or retired; check ai.google.dev models page |
| Gemini 429 | Free-tier limits; enable billing |
| Replies stop after a while on Cloud Run | Set `--min-instances 1` and `--no-cpu-throttling` |
| Farmer can't message outside business hours | Not an issue: farmers can always message first; the 24-hour window only limits messages *you* start |

---

## 10. Project structure

```
krishi-sakhi/
  app/
    __init__.py
    main.py        FastAPI app and routes (/webhook, /health), background task entry
    config.py      reads .env
    handlers.py    message flow: text / voice / photo / buttons
    whatsapp.py    talks to Meta: send, buttons, audio, media, signature check
    speech.py      Gemini voice ↔ text + ffmpeg
    brain.py       Gemini + Krishi Sakhi system prompt
    lang.py        language detection + fixed messages (gu / hi / en)
    store.py       Redis: duplicates, daily limit, language, memory, logs
    admin.py       /admin/chats
    retry.py       retry helper
  tests/
    conftest.py    fakes (Meta via respx, Gemini, fakeredis) + payload builders
    test_flow.py   end-to-end flow tests (everything mocked, real ffmpeg)
  pytest.ini       pytest settings (async tests)
  Dockerfile       Python 3.12 + ffmpeg
  .dockerignore
  .gitignore
  .env.example     template for your .env
  requirements.txt
  README.md
  CLAUDE_CODE_PROMPT.md
```

### Next improvements
1. **Knowledge base**: add verified crop guides and KVK advisories (Postgres + pgvector) so answers are grounded in them.
2. **Farmer profile**: store village, crops and land size, and add them to the prompt.
3. **Weather and mandi prices**: add Gemini function calls to a weather API and data.gov.in (Agmarknet).
4. **Proactive alerts**: send weather or pest alerts using approved WhatsApp utility templates.
5. **Admin dashboard**: Streamlit or Retool to review chats and flag wrong answers.
#   W h a t s a p p - C h a t b o t  
 