package dev.aether.vault.vectors

import org.bouncycastle.crypto.hpke.HPKE

/**
 * RFC 9180 base mode over DHKEM(X25519, HKDF-SHA256), HKDF-SHA256 and
 * AES-256-GCM, using Bouncy Castle's reviewed implementation. Tink 1.14's
 * public HybridEncrypt primitive hardcodes empty AAD, which cannot express the
 * protocol's AAD-bound grant, so the Kotlin HPKE choice is Bouncy Castle.
 */
object Hpke {
    fun suite(): HPKE =
        HPKE(
            HPKE.mode_base,
            HPKE.kem_X25519_SHA256,
            HPKE.kdf_HKDF_SHA256,
            HPKE.aead_AES_GCM256,
        )

    fun open(
        recipientPrivate: ByteArray,
        recipientPublic: ByteArray,
        info: ByteArray,
        aad: ByteArray,
        enc: ByteArray,
        ct: ByteArray,
    ): ByteArray {
        val hpke = suite()
        val skR = hpke.deserializePrivateKey(recipientPrivate, recipientPublic)
        return hpke.open(enc, skR, info, aad, ct, null, null, null)
    }

    /** Deterministic sealing with a caller-supplied ephemeral key, for vectors. */
    fun seal(
        recipientPublic: ByteArray,
        ephemeralPrivate: ByteArray,
        ephemeralPublic: ByteArray,
        info: ByteArray,
        aad: ByteArray,
        plaintext: ByteArray,
    ): Pair<ByteArray, ByteArray> {
        val hpke = suite()
        val pkR = hpke.deserializePublicKey(recipientPublic)
        val kpE = hpke.deserializePrivateKey(ephemeralPrivate, ephemeralPublic)
        val context = hpke.setupBaseS(pkR, info, kpE)
        val ct = context.seal(aad, plaintext)
        return ct to context.encapsulation
    }
}
