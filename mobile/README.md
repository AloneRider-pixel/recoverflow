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
EXPO_PUBLIC_API_URL=<your-api-url> npx expo start
```

## Release checklist

Validate authentication expiry, API compatibility, notification registration, payment-link behavior, and collection flows before distributing a build. Configure EAS/project credentials through the provider tooling; do not commit them.

## Security boundary

The mobile bundle contains public configuration only. API/session credentials use platform-secure storage; payment-provider and database secrets remain backend-only.

## Review path

Review `src/api.ts`, token lifecycle, notification handling, and API compatibility before release.

## License

MIT
