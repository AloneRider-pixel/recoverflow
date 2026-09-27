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
