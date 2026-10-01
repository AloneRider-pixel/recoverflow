# RecoverFlow Mobile

Production-oriented React Native + Expo client for RecoverFlow.

## Included

- Secure login / registration with Expo SecureStore
- Shared production FastAPI backend
- Dashboard + collection metrics
- Invoice portfolio
- Customer portfolio
- Create invoice
- Razorpay payment-link action
- WhatsApp collection action
- Mark-paid action
- Push notification registration and test flow
- iOS / Android application configuration
- EAS build configuration

## Local development

`npm install`

`EXPO_PUBLIC_API_URL=https://recoverflow-7vnr.onrender.com npx expo start`

For remote push notifications, use an EAS development or production build on a physical device. Expo documents that Android remote push notifications are not available in Expo Go from SDK 53 onward.

## Production release

1. Create/link the Expo project with EAS.
2. Configure the EAS project ID and notification credentials.
3. Build Android and iOS with `eas build`.
4. Submit the resulting binaries to Google Play and App Store.

The mobile client uses the shared RecoverFlow API and does not contain payment-provider secrets.

## Mobile security boundary

The mobile app should contain only public configuration such as the API base URL. Authentication tokens and provider credentials must use the platform-secure mechanisms described by the implementation; payment-provider secrets belong exclusively on the backend.

## Review path

Before release, validate Android/iOS builds, authentication/session expiry, API compatibility, deep links/notifications, and payment-link actions against the current backend contract.
