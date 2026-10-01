# RecoverFlow

Receivables operations platform for SMBs: centralize invoices, surface overdue cash, coordinate follow-ups, create payment links, and manage collection workflows.

## Product surfaces

### Web / API

- Authenticated receivables dashboard.
- Invoice creation and CSV import.
- Customer and follow-up workspace.
- Razorpay payment links.
- WhatsApp reminders.
- Subscription billing and integration settings.

### Mobile

- React Native + Expo client under `mobile/`.
- Shared FastAPI backend.
- Secure token storage with Expo SecureStore.
- Invoice, customer, collection, notification, and account flows.
- EAS-ready iOS/Android configuration.

## Architecture

```text
Web / Mobile
     ↓
FastAPI API
 ├── Authentication / sessions
 ├── Invoices / customers
 ├── Collection engine
 ├── Payment integrations
 └── Scheduled operations
     ↓
PostgreSQL

Payment provider and messaging systems remain external trust boundaries.
```

## Stack

| Layer | Technology |
|---|---|
| Backend | FastAPI, SQLAlchemy, PostgreSQL |
| Security | scrypt, signed sessions, encrypted credentials |
| Integrations | Razorpay, WhatsApp, optional OpenAI |
| Automation | scheduled internal endpoints / collection engine |
| Mobile | React Native, Expo, SecureStore |
| Delivery | Render, GitHub Actions |

## Quick start

Backend:

```bash
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Mobile:

```bash
cd mobile
npm install
EXPO_PUBLIC_API_URL=https://recoverflow-7vnr.onrender.com npx expo start
```

## Verification

CI validates Python compilation/import and the deterministic collection-engine smoke path, plus mobile TypeScript typechecking.

Useful local checks:

```bash
python -m compileall -q app
python -c "from app.main import app; print(app.title)"
cd mobile
npx tsc --noEmit
```

## Payment security

RecoverFlow treats Razorpay checkout, webhooks, sessions, secrets, and customer data as separate trust boundaries. Provider signatures, idempotency, replay handling, and credential separation must remain intact.

The production gate distinguishes test credentials from live credentials. Do not enable real-money operations until live secrets, webhooks, HTTPS, legal identity, idempotency, and retry scenarios have been verified in the target environment.

## Operations

The repository includes scheduled recurring-invoice and overdue-reminder workflows. Production execution is secret-gated and skipped rather than bypassed when required credentials are absent.

## Security

Never commit API keys, webhook secrets, session secrets, or customer credentials. Keep payment-provider secrets exclusively server-side.

See [SECURITY.md](SECURITY.md).

## Evidence and limitations

Product capability is not the same as production outcome. Any published collection, payment, reliability, latency, or conversion metric should identify the dataset/workload, environment, observation window, denominator, and producing commit.

## Review path

Read [SECURITY.md](SECURITY.md) first, then review payment/webhook code, session boundaries, collection logic, and scheduled operations.

## Maintenance standard

Preserve idempotency, signature verification, replay protection, secret boundaries, and safe failure behavior.

## License

MIT
