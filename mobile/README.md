# RecoverFlow Mobile

React Native + Expo client for RecoverFlow.

## Features

- Secure authentication using Expo SecureStore.
- Dashboard, invoice, customer, and collection views.
- Payment-link and WhatsApp collection actions.
- Push-notification registration flows.
- iOS/Android and EAS configuration.

## Development

From `mobile/`:

```bash
npm install
EXPO_PUBLIC_API_URL=https://recoverflow-7vnr.onrender.com npx expo start
```

For remote push-notification validation, use an EAS development/production build on a physical device.

## Release

1. Link the Expo project with EAS.
2. Configure project/notification credentials.
3. Build Android/iOS with `eas build`.
4. Validate authentication expiry, API compatibility, notifications, and payment-link flows.
5. Submit the binaries only after backend compatibility checks pass.

## Security boundary

The mobile bundle contains public configuration only. API/session tokens use platform-secure storage; payment-provider and database secrets remain backend-only.

## Review path

Check `src/api.ts`, authentication/token lifecycle, notification handling, and backend endpoint compatibility before release.
