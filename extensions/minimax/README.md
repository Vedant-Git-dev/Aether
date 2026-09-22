# MiniMax (Aether plugin)

Bundled MiniMax plugin for both:

- API-key provider setup (`minimax`)
- Token Plan OAuth setup (`minimax-portal`)

## Enable

```bash
aether plugins enable minimax
```

Restart the Gateway after enabling.

```bash
aether gateway restart
```

## Authenticate

OAuth:

```bash
aether models auth login --provider minimax-portal --set-default
```

API key:

```bash
aether setup --wizard --auth-choice minimax-global-api
```

## Notes

- MiniMax OAuth uses a user-code login flow.
- OAuth currently targets the Token Plan path.
