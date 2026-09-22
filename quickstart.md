# Chronicle Quick Start

## What You're Building (Complete Beginners Start Here!)

You're setting up your own personal AI that:
- **Runs on your home computer** - processes audio, stores memories, runs AI models
- **Connects to your phone** - where you use the Chronicle app and OMI device
- **Works everywhere** - your phone can access your home AI from anywhere

Think of it like having Siri/Alexa, but it's **your own AI** running on **your hardware** with **your data**.

## The Setup (What You'll Install)

### On Your Home Computer
- **Docker** - Runs all the AI services (like having multiple apps in containers)
- **Chronicle Backend** - The main AI brain (transcription, memory, processing)
- **Tailscale** - Creates secure tunnel so your phone can reach home

### On Your Phone
- **Tailscale** - Connects securely to your home computer
- **Chronicle Mobile App** - Interface for your OMI device and conversations

### AI Services (Choose Your Path)

**Option A: Cloud Services (Easiest - Recommended for Beginners)**
- **Deepgram** - Speech-to-text ($200 free credits, then pay per use)
- **OpenAI** - Memory extraction (~$1-5/month typical usage)
- Best quality, minimal setup, small monthly cost

**Option B: Local Services (Free but More Complex)**
- **Parakeet ASR** - Offline speech-to-text (runs on your computer)
- **Ollama** - Local AI models (runs on your computer)
- Completely free and private, requires more powerful hardware

**Optional Add-ons (Both Paths)**
- **Hugging Face** - Speaker recognition (free API key)

## Step 1: Install Required Software

### On Your Home Computer

**Git** (Downloads code from the internet):
- **Windows/Mac**: [Download Git](https://git-scm.com/downloads)
- **Linux**: `sudo apt install git` or `sudo yum install git`

**Docker** (Runs the AI services):
- **Windows/Mac**: [Download Docker Desktop](https://www.docker.com/products/docker-desktop/)
- **Linux**: [Install Docker](https://docs.docker.com/engine/install/)
- **After install**: Make sure Docker Desktop is running

> **WSL Users**: Chronicle services will fail to start unless Docker
> is installed and running inside WSL2
> (or Docker Desktop with **WSL integration enabled**).

**uv** (Python package manager):
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```
*This downloads and runs Python programs for you*

**Tailscale** (Connects your phone to home computer):
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```
*Follow the login prompts - this gives your computer a special IP address*

### On Your Phone

**Tailscale App**:
- **iPhone**: [App Store - Tailscale](https://apps.apple.com/app/tailscale/id1470499037)
- **Android**: [Google Play - Tailscale](https://play.google.com/store/apps/details?id=com.tailscale.ipn)

*Log in with the same account you used on your computer*

## Step 2: Choose Your AI Services Path

### Path A: Cloud Services (Recommended for Beginners)

**Deepgram (Speech-to-Text)**
1. Go to [console.deepgram.com](https://console.deepgram.com)
2. Sign up for free account (get $200 free credits)
3. Go to "API Keys" → Create new key
4. **Copy the key** - you'll need it in setup

**OpenAI (AI Brain)**
1. Go to [platform.openai.com](https://platform.openai.com)
2. Create account and add payment method (typically costs $1-5/month)
3. Go to "API Keys" → Create new key
4. **Copy the key** - you'll need it in setup

**Optional: Hugging Face (Speaker Recognition)**
1. Go to [huggingface.co](https://huggingface.co/join)
2. Create free account
3. Go to Settings → Access Tokens → Create new token
4. **Copy the token** - for identifying different speakers

### Path B: Local Services (Free & Private)

**No API keys needed!** Everything runs on your computer.

The setup wizard will automatically download and configure:
- **Parakeet ASR** - Local speech-to-text service
- **Ollama** - Local AI model runner

*Note: First-time setup will download AI models (this can take time and storage space)*

## Step 3: Download and Setup Chronicle

### On Your Home Computer

**Download the code:**
```bash
git clone https://github.com/SimpleOpenSoftware/chronicle.git
cd chronicle
```

**Run the setup wizard:**
```bash
# Using convenience script (recommended)
./wizard.sh

# Or use direct command:
uv run --with-requirements setup-requirements.txt python wizard.py
```

**Operator entry points**: `./wizard.sh` configures Chronicle; `./services` manages it.

### What the Setup Wizard Will Ask You

The wizard will ask questions - here's what to answer:

**"Admin email"**: Your email (for logging into web dashboard)
**"Admin password"**: Password for web dashboard (8+ characters)

#### For Cloud Services (Path A):

**"Choose transcription provider"**: Choose `deepgram`
**"Deepgram API key"**: Paste the key you got from Deepgram

**"Choose LLM provider"**: Choose `openai`
**"OpenAI API key"**: Paste the key you got from OpenAI
**"OpenAI model"**: Keep default (gpt-4o-mini)

#### For Local Services (Path B):

**"Choose transcription provider"**: Choose `parakeet`
- The wizard will configure local Parakeet ASR service
- No API key needed

**"Choose LLM provider"**: Choose `ollama`
- The wizard will configure local Ollama
- No API key needed
- Default model: llama3.2 (will be downloaded automatically)

#### Optional (Both Paths):

**"Enable Speaker Recognition"**: Say Yes if you got Hugging Face token
**"Hugging Face token"**: Paste your token (if you got one)

#### HTTPS Setup (Required for Both):

**"Enable HTTPS"**: **Say Yes** (needed for phone connection)
**"Server IP for SSL certificate"**:
- Run `tailscale ip` in another terminal
- Copy the IP that starts with `100.` (like `100.64.1.5`)
- Paste that IP here

*The wizard creates all the configuration files automatically*

**Start the services:**
```bash
# Start configured services and wait for readiness
./services start --all
```

*This downloads and starts all the AI services - takes 5-10 minutes first time*

## Step 4: Test Your Backend (Important!)

Before connecting your phone, make sure everything works:

1. Visit: **https://[your-tailscale-ip]** (like `https://100.64.1.5`)

   *Your browser will warn about "unsafe certificate" - click "Advanced" → "Proceed anyway"*

2. You should see the Chronicle dashboard
3. Click "Live Recording" in the sidebar
4. Test your microphone - record a short clip
5. Check that it gets transcribed and appears in "Conversations"
6. **Only proceed to phone setup when this works perfectly!**

## Step 5: Install Chronicle on Your Phone

**No development setup needed - just download and install!**

### Android Users
1. Go to [GitHub Releases](https://github.com/SimpleOpenSoftware/chronicle/releases)
2. Find the latest release and download `chronicle-android.apk`
3. Install APK on your phone:
   - Enable "Install from unknown sources" in Android settings
   - Tap the downloaded APK file to install

### iPhone Users
1. Go to [GitHub Releases](https://github.com/SimpleOpenSoftware/chronicle/releases)
2. Find the latest release and download `chronicle-ios.ipa`
3. Install using sideloading tool:
   - **AltStore** (recommended): [altstore.io](https://altstore.io)
   - **Sideloadly**: [sideloadly.io](https://sideloadly.io)

   *Note: iOS requires sideloading since we're not on App Store yet*

### Configure the App
1. **First**: Make sure Tailscale is running on your phone
2. Open Chronicle app
3. Go to Settings → Backend Configuration
4. Enter Backend URL: `https://[your-tailscale-ip]`

   *Use the same IP as your web dashboard - like `https://100.64.1.5`*

5. Tap "Test Connection" - should show **green checkmark**
6. If connection fails, double-check:
   - Tailscale is running on phone
   - Same IP as web dashboard
   - Using `https://` (not `http://`)

## Step 6: Connect Your OMI Device

1. Turn on your OMI/Friend device (make sure it's charged)
2. Open Chronicle app on your phone
3. Go to "Devices" tab → "Add New Device"
4. Follow Bluetooth pairing instructions
5. Once connected, start a conversation!
6. **Check your web dashboard** - the conversation should appear there

## You're Done! 🎉

**What you now have:**
- ✅ Personal AI running on your home computer
- ✅ Phone app connected securely via Tailscale
- ✅ OMI device streaming audio to your AI
- ✅ All conversations processed privately and stored locally
- ✅ Access from anywhere via your phone

**Next steps:**
- Explore the web dashboard features
- Try voice commands and see memories get extracted
- Invite others to test conversations (if you enabled speaker recognition)

## Troubleshooting

### Setup Issues
- **"Command not found"**: Make sure Docker Desktop is running
- **"Permission denied"**: Try `sudo` before commands (Linux/Mac)
- **"uv not found"**: Restart terminal after installing uv

### Connection Issues
- **Phone can't reach backend**: Check Tailscale is running on both devices
- **Certificate warnings**: Click "Advanced" → "Proceed" in browser
- **Test connection fails**: Verify you're using `https://` and correct Tailscale IP

### Service Issues

Start with `./services status --detailed`. For lifecycle commands, starting only an affected
service, rebuilds, diagnostics, and recovery verification, follow the
[service-management workflow](docs/init-system.md#service-management). It applies to
both Docker and Podman and includes the managed LLM, wakeword, and TTS services.

**Cloud Services (Deepgram/OpenAI):**
- **Transcription not working**: Check Deepgram API key is correct
- **No memories created**: Check OpenAI API key and account has credits
- **High costs**: Switch to `gpt-4o-mini` model for cheaper processing

**Local Services (Parakeet/Ollama):**
- **Parakeet not starting**: Check `docker compose ps` - Parakeet container should be running
- **Slow transcription**: Local ASR is slower than cloud services, this is normal
- **Ollama model download stuck**: Check internet connection, models can be large (5-20GB)
- **Out of memory errors**: Local services need sufficient RAM, try smaller Ollama models

## Need Help?

- **Full Documentation**: [AGENTS.md](AGENTS.md) - Complete technical reference
- **Architecture Details**: [docs/overview.md](docs/overview.md) - How everything works
- **Advanced Setup**: [docs/init-system.md](docs/init-system.md) - Power user options
