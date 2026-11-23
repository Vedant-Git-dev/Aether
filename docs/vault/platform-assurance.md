# Platform assurance record

The feasibility checks used synthetic grants and queued work to assess native
key handling, hardware-backed storage, and background delivery. Evidence came
from a native test harness, a physical handset, and an emulator.

## Cryptography and interoperability

| Check | Result |
|---|---|
| RFC 9180 HPKE with X25519, HKDF-SHA256, and AES-256-GCM | Published key pairs, encapsulation, and ciphertext matched. Decryption rejected altered associated data. |
| ES256 and Ed25519 JWS | Published ES256 vector passed. Signing and verification passed with pinned algorithms. Python-generated ES256 signatures verified under the Kotlin JOSE implementation. |
| RFC 8785 canonical JSON | Published number edge cases and key ordering matched. |
| BIP39 recovery phrase | A synthetic 256-bit entropy value round-tripped through phrase encoding. A changed word failed checksum validation. |

The evidence used OpenSSL for native HPKE, pyhpke for HPKE with associated
data, joserfc for JWS, rfc8785 for canonical serialization, and mnemonic
for the recovery phrase. The Kotlin selections are Nimbus JOSE+JWT 9.40 for
JWS and Bouncy Castle 1.80 for HPKE. Bidirectional vectors now pass: Python
seals HPKE with nonempty associated data and Kotlin opens and reproduces the
bytes, Kotlin seals and Python opens, and Ed25519 recovery signatures verify
in both directions. Nimbus uses Tink 1.14 internally for Ed25519.

Tink 1.14's public HPKE primitive always uses empty associated data and maps
its caller context to the HPKE `info` parameter, so it cannot implement the
approved AAD-bound grant. Bouncy Castle's RFC 9180 implementation accepts
both `info` and AAD and is the Kotlin HPKE choice. This is a library
selection change, not a change to the suite or wire format.

## Native grant handling and cleanup

The native harness verified the signature, signer, and grant binding before
opening HPKE into a locked, non-dumpable page. Python received an opaque
handle and an HMAC result. The harness rejected altered associated data, bad
signatures, mismatched signer, request, boot or task, expired grants, and
attempts to reuse closed or cancelled handles.

The cleanup observer checked the affected memory after cleansing and
inspected the plaintext page immediately before unmapping.

| Path | Observed result |
|---|---|
| Close after a successful grant | The encoded key was present in the plaintext page and the decoded key was present in the handle slot. Both regions were zero after cleanup. |
| Cancellation | Both key-bearing regions were erased. The handle was no longer live. |
| Rejection after a valid first key was decoded | The partially populated slot and plaintext page were zero after cleanup. |
| Invalid HPKE authentication tag | The grant was rejected and the plaintext page was zero before unmapping. |

All four paths passed in ten independent runs. The experimental parser also
used temporary stack buffers for encoded and decoded keys without an explicit
wipe. Decode into protected native memory in the production adapter, or
cleanse every key-bearing temporary on all exits. Cleanup inside OpenSSL is
unverified.

## Physical handset

| Check | Observed result |
|---|---|
| Hardware custody | Android Keystore reported TEE-backed wrapping and signing keys. The wrapping key was non-exportable. Each wrap used a fresh IV. |
| Hardware signing and persistence | P-256 signatures verified against the public key. A stored wrap still opened after changing the lock mode. |
| Packaged WebView | Signed test packages built. Local assets loaded offline under a restrictive content security policy. External navigation and cleartext requests were blocked. Screenshot and recent-task previews were blank. |
| Packaging flags | The test manifest disabled backup and omitted the debugging flag. Backup and device transfer remain release checks for the packaged application. |
| Background fetch | A connectivity-triggered worker ran with the WebView closed. A live push woke the worker with the WebView closed, including after reboot and first unlock. |
| Power management | The power manager froze the process about five seconds after backgrounding, even with a foreground service. A battery-optimization exemption did not prevent the freeze. Automatic launch of a scheduled job required an additional permission. |

Background work must fit within the execution window the OS allows. Live
delivery of a complete grant and queue drain still needs to pass in the
packaged application on physical hardware.

## Emulator follow-up

| Check | Observed result |
|---|---|
| Foreground-service timeout | With the `dataSync` timeout configured for five seconds, `onTimeout` saw the item still pending and `stopSelf()` stopped the service. The earlier test package lacked a timeout override. |
| Reboot | Pending work survived. Android accepted the initial service request from `BOOT_COMPLETED` but blocked foreground promotion. |
| Deferred recovery | A simulated wake scheduled a network-constrained job. The operating system ran the job and cleared the queued item without reopening the activity or forcing the job to run. |

The run exercised scheduling after a simulated wake. Live push handling and
the complete grant flow require separate verification. If the implementation
uses `dataSync`, handle `onTimeout`, keep the queue durable, and resume
through scheduled work or a permitted wake. [Foreground service testing](https://developer.android.com/develop/background-work/services/fgs/timeout)

## Owner decisions

- Disclose the accepted residual metadata.
- Delete legacy decryption material at the approved cutover.
- Release Android first after the agreed scope passes.
- Keep push send credentials on the backend.

## Task 1a verification scope

The Python suite runs the published RFC 9180 Appendix A.1 base-mode case
and the Aether AES-256-GCM vectors. The JVM harness independently serializes
the Aether fixture objects before signing or opening them. The production
use-case registry starts empty and rejects unknown IDs. Its populated
shared entries require owner approval before serving code uses them.
Android app packaging and native serializer checks remain part of native
integration. The reference cryptography and deterministic inputs live under tests. Live
key management requires the native adapter and scoped handles.

[RFC 9180 Appendix A.1](https://www.rfc-editor.org/rfc/rfc9180.html#appendix-A.1)
