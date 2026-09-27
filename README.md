# RecoverFlow

RecoverFlow is a receivables operations platform for SMBs: bring invoices into one workspace, surface overdue cash, send follow-ups, create payment links and close invoices faster.

## Product surfaces

### Web
- Professional public marketing site at `/` when signed out
- Authenticated receivables dashboard
- Invoice creation + CSV import
- Customer follow-up workspace
- Razorpay payment links
- WhatsApp reminders
- Subscription billing
- Integration settings

### Mobile
- React Native + Expo client under `/mobile`
- Shared FastAPI + PostgreSQL backend
- Secure bearer-token storage with Expo SecureStore
- Mobile dashboard, invoice queue, invoice creation and account settings
- One codebase for iOS and Android

## API

Mobile and future web clients use the versioned API:
- `POST /api/v1/auth/login`
- `POST /api/v1/auth/register`
- `GET /api/v1/me`
- `GET /api/v1/dashboard`
- `GET /api/v1/invoices`
- `POST /api/v1/invoices`
- `POST /api/v1/invoices/{id}/mark-paid`

The API uses signed bearer tokens. Browser sessions remain isolated from API authentication.

## Backend

- FastAPI
- SQLAlchemy
- PostgreSQL
- scrypt password hashing
- signed sessions
- encrypted integration credentials
- Razorpay + WhatsApp integrations
- optional OpenAI copy generation

## Production environment

Set:
- `DATABASE_URL`
- `SESSION_SECRET`
- `CRON_SECRET`
- `WHATSAPP_GRAPH_VERSION`
- `RAZORPAY_PLATFORM_KEY_ID`
- `RAZORPAY_PLATFORM_KEY_SECRET`
- `RAZORPAY_PLATFORM_WEBHOOK_SECRET` when subscription webhooks are enabled

Never commit credentials to GitHub.

## Mobile development

From `mobile/`:
`npm install`
`npx expo start`

The client defaults to the production RecoverFlow API and can be overridden with `EXPO_PUBLIC_API_URL`.

## Deployment

Current backend deployment:
- Render web service
- PostgreSQL database
- automatic deploys from `main`

The mobile client is prepared for Expo Application Services builds using `mobile/eas.json`.

## Payment security architecture

RecoverFlow treats payment processing as a separate trust boundary. Razorpay hosts checkout; RecoverFlow does not store card numbers, bank credentials or payment instrument credentials. The backend creates orders, records transaction state, verifies the Razorpay signature, confirms the order/payment against Razorpay, and uses webhook events for asynchronous reconciliation. Razorpay recommends keeping API secrets out of version control, using HTTPS, validating callback signatures, and using webhooks with HMAC verification. (https://razorpay.com/security/checklist)

Payment transactions are persisted in payment_transactions with unique Razorpay order/payment IDs. Webhook deliveries are persisted in webhook_events with a unique provider event ID so retries and replays are not processed twice.

For production, keep the two Razorpay credential domains separate:
- RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET — merchant credentials for customer invoice collection (workspace credentials take precedence where configured)
- RAZORPAY_PLATFORM_KEY_ID / RAZORPAY_PLATFORM_KEY_SECRET — dedicated credentials for RecoverFlow subscription billing
- RAZORPAY_PLATFORM_WEBHOOK_SECRET — webhook secret for RecoverFlow subscription billing
- Workspace Razorpay webhook secret — webhook secret for customer invoice payment events

The /launch page is a production gate. It reports test vs live credentials, HTTPS, session secret strength, payment idempotency, webhook replay protection, billing credentials, automation secrets and legal identity. Test credentials intentionally do not satisfy the production gate.

Before enabling real-money operations, configure live Razorpay credentials, webhook secrets and production legal identity in Render, then perform successful, failed, retry, duplicate-click and webhook-retry tests in live/test environments as appropriate. Razorpay's Python integration guide recommends using webhooks as the primary asynchronous notification path and supplementing them with API verification for immediate user-facing confirmation. (https://razorpay.com/docs/server-integration/python/test-app/)