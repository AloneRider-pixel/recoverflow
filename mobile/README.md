# RecoverFlow Mobile

React Native + Expo client for RecoverFlow.

## Scope

The mobile client provides:

- secure authentication using Expo SecureStore;
- dashboard, invoice, customer, and collection views;
- payment-link and WhatsApp collection actions;
- push-notification registration flows; and
- iOS/Android and EAS-oriented configuration.

The mobile application is a client of the RecoverFlow API. Payment-provider credentials, database credentials, authorization decisions, and server-side business rules remain backend responsibilities.

## Development

From `mobile/`:

```bash
npm install
EXPO_PUBLIC_API_URL=<your-api-url> npx expo start
```

## Verification

```bash
npx tsc --noEmit
```

Before release, validate authentication expiry, API compatibility, notification registration, payment-link behavior, and collection flows.

## Release checklist

- Verify authenticated API calls against the intended environment.
- Test token expiry and recovery.
- Validate push-notification registration and failure handling.
- Verify payment-link and collection actions against non-production credentials first.
- Configure EAS/project credentials through provider tooling; never commit them.

## Security boundary

The mobile bundle contains public configuration only. API/session credentials use platform-secure storage; payment-provider, database, and signing secrets remain backend- or provider-managed.

## Review path

Review `src/api.ts`, token lifecycle, notification handling, payment/collection actions, and API compatibility before release.

## License

MIT
