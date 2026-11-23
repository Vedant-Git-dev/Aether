package dev.aether.vault.vectors

import com.nimbusds.jose.JWSAlgorithm
import com.nimbusds.jose.JWSHeader
import com.nimbusds.jose.JWSSigner
import com.nimbusds.jose.JWSVerifier
import com.nimbusds.jose.crypto.ECDSASigner
import com.nimbusds.jose.crypto.ECDSAVerifier
import com.nimbusds.jose.crypto.Ed25519Signer
import com.nimbusds.jose.crypto.Ed25519Verifier
import com.nimbusds.jose.jwk.Curve
import com.nimbusds.jose.jwk.ECKey
import com.nimbusds.jose.jwk.OctetKeyPair
import com.nimbusds.jose.util.Base64URL

/**
 * Detached JWS helpers pinned to the two algorithms the protocol allows.
 *
 * The protected header is built here in RFC 8785 order (`alg` before `kid`)
 * instead of letting Nimbus serialize its map, which puts custom parameters
 * such as `kid` first. Nimbus still performs the signature itself.
 */
object Signing {
    data class Detached(val protected: String, val signingInput: String, val signature: String)

    fun ed25519Key(seed: ByteArray?, public: ByteArray): OctetKeyPair {
        val builder = OctetKeyPair.Builder(Curve.Ed25519, Base64URL.encode(public))
        if (seed != null) builder.d(Base64URL.encode(seed))
        return builder.build()
    }

    fun ecKey(private: ByteArray?, public: ByteArray): ECKey {
        require(public.size == 65 && public[0] == 4.toByte()) { "P-256 public key must be SEC1" }
        val builder =
            ECKey.Builder(
                Curve.P_256,
                Base64URL.encode(public.copyOfRange(1, 33)),
                Base64URL.encode(public.copyOfRange(33, 65)),
            )
        if (private != null) builder.d(Base64URL.encode(private))
        return builder.build()
    }

    fun signEd25519(body: ByteArray, seed: ByteArray, public: ByteArray, kid: String): Detached =
        detached("Ed25519", kid, body, Ed25519Signer(ed25519Key(seed, public)))

    fun signEs256(body: ByteArray, private: ByteArray, public: ByteArray, kid: String): Detached =
        detached("ES256", kid, body, ECDSASigner(ecKey(private, public)))

    fun verifyEd25519(signingInput: String, signature: ByteArray, public: ByteArray): Boolean =
        Ed25519Verifier(ed25519Key(null, public))
            .verify(header("Ed25519"), bytes(signingInput), Base64URL.encode(signature))

    fun verifyEs256(signingInput: String, signature: ByteArray, public: ByteArray): Boolean =
        ECDSAVerifier(ecKey(null, public))
            .verify(header("ES256"), bytes(signingInput), Base64URL.encode(signature))

    private fun detached(alg: String, kid: String, body: ByteArray, signer: JWSSigner): Detached {
        val protected = b64u("{\"alg\":\"$alg\",\"kid\":\"$kid\"}".toByteArray(Charsets.UTF_8))
        val payload = b64u(body)
        val signingInput = "$protected.$payload"
        val signature = signer.sign(header(alg), bytes(signingInput))
        return Detached(protected, signingInput, signature.toString())
    }

    private fun header(alg: String): JWSHeader =
        JWSHeader.Builder(JWSAlgorithm.parse(alg)).build()

    private fun bytes(value: String): ByteArray = value.toByteArray(Charsets.UTF_8)
}
