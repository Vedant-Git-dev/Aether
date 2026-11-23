# Android wire vectors

`vectors/` is a JVM-only Gradle module. It verifies the shared wire encodings
between the Python backend and the Kotlin client. It is not packaged into the
Android application.

## Toolchain

- Temurin JDK 17
- Gradle 8.9
- Kotlin 2.0.21

## Libraries

- Nimbus JOSE+JWT 9.40 for detached ES256 and Ed25519 JWS
- Bouncy Castle 1.80 for RFC 9180 HPKE with associated data
- Tink 1.14 for the Ed25519 primitive Nimbus uses

Tink's public HPKE primitive hardcodes empty associated data, so it cannot
express the approved AAD-bound grant. Bouncy Castle accepts both `info` and
AAD. Nimbus still uses Tink internally for Ed25519.

## Commands

From this directory:

```bash
gradle :vectors:test
gradle :vectors:generateVectors
```

The fixtures live in `tests/vault/vectors`. `generateVectors` writes the
Kotlin side under `tests/vault/vectors/kotlin`; the Python suite verifies it.

The JVM tests reconstruct canonical signing input and HPKE associated data
from the fixture objects using the safe-integer and ASCII subset used in
these fixtures. The Android app must verify its own serializer and package
contents when the native vault is integrated.
