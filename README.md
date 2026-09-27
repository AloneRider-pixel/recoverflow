# RecoverFlow

B2B receivables management for SMBs.

## Included
- Multi-account login with isolated invoice data
- CSV + manual invoice creation
- Demo data
- Razorpay Payment Links integration + signed webhook processing
- WhatsApp Cloud API template sending + reminder logging
- Daily automatic reminder endpoint
- Razorpay subscription billing
- Optional OpenAI copy generation (not required)

## Production environment
- DATABASE_URL
- SESSION_SECRET
- CRON_SECRET
- WHATSAPP_GRAPH_VERSION when WhatsApp API is enabled
- RAZORPAY_PLATFORM_KEY_ID / RAZORPAY_PLATFORM_KEY_SECRET for RecoverFlow subscriptions

Business customers configure their own Razorpay and WhatsApp credentials inside Settings → Integrations.

## Automation
A GitHub Actions workflow calls the protected reminder endpoint every day at 10:00 AM IST. Add the repository secret RECOVERFLOW_CRON_SECRET with the same value as Render's CRON_SECRET.

## Security
Never commit API keys to GitHub. Razorpay webhook signatures are verified with HMAC before payment/subscription state updates. Integration secrets stored in the application database are encrypted using a key derived from SESSION_SECRET.
