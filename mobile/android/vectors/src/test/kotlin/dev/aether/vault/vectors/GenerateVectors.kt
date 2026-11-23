package dev.aether.vault.vectors

import org.json.JSONObject

/**
 * Writes the Kotlin side of the public vectors. The Python suite verifies these
 * files, so a failure means the two implementations disagree on bytes.
 *
 * Run with: gradle :vectors:generateVectors
 */
fun main() {
    generateHpke()
    generateEd25519()
    generateEs256()
    println("wrote Kotlin vectors under ${Vectors.dir.resolve("kotlin")}")
}

private fun generateHpke() {
    val fixture = Vectors.read("hpke_python.json")
    val recipientPrivate = unb64u(fixture.getString("recipient_private"))
    val recipientPublic = unb64u(fixture.getString("recipient_public"))
    val ephemeralPrivate = unb64u(fixture.getString("ephemeral_private"))
    val ephemeralPublic = unb64u(fixture.getString("ephemeral_public"))
    val info = fixture.getString("info").toByteArray(Charsets.UTF_8)
    val aad = fixtureAad(fixture)
    val plaintext = unb64u(fixture.getString("plaintext"))
    val (ct, enc) =
        Hpke.seal(recipientPublic, ephemeralPrivate, ephemeralPublic, info, aad, plaintext)
    val out =
        JSONObject()
            .put("usage", fixture.getString("usage"))
            .put("recipient_private", fixture.getString("recipient_private"))
            .put("recipient_public", fixture.getString("recipient_public"))
            .put("ephemeral_private", fixture.getString("ephemeral_private"))
            .put("ephemeral_public", fixture.getString("ephemeral_public"))
            .put("header", fixture.getJSONObject("header"))
            .put("info", fixture.getString("info"))
            .put("aad", b64u(aad))
            .put("plaintext", fixture.getString("plaintext"))
            .put("box", JSONObject().put("enc", b64u(enc)).put("ct", b64u(ct)))
    Vectors.write("kotlin/hpke_kotlin.json", out)
}

private fun generateEd25519() {
    val fixture = Vectors.read("ed25519_python.json")
    val seed = unb64u(fixture.getString("seed"))
    val public = unb64u(fixture.getString("public"))
    val body = fixtureJson(fixture.getJSONObject("body")).toByteArray(Charsets.UTF_8)
    val kid = fixture.getString("kid")
    val detached = Signing.signEd25519(body, seed, public, kid)
    val envelope =
        JSONObject()
            .put("body", fixture.getJSONObject("body"))
            .put("protected", detached.protected)
            .put("signature", detached.signature)
    val out =
        JSONObject()
            .put("seed", fixture.getString("seed"))
            .put("public", fixture.getString("public"))
            .put("kid", kid)
            .put("body", fixture.getJSONObject("body"))
            .put("body_b64", b64u(body))
            .put("envelope", envelope)
            .put("signing_input", detached.signingInput)
    Vectors.write("kotlin/ed25519_kotlin.json", out)
}

private fun generateEs256() {
    val fixture = Vectors.read("es256_python.json")
    val private = unb64u(fixture.getString("private"))
    val public = unb64u(fixture.getString("public"))
    val body = fixtureJson(fixture.getJSONObject("body")).toByteArray(Charsets.UTF_8)
    val kid = fixture.getString("kid")
    val detached = Signing.signEs256(body, private, public, kid)
    val envelope =
        JSONObject()
            .put("body", fixture.getJSONObject("body"))
            .put("protected", detached.protected)
            .put("signature", detached.signature)
    val out =
        JSONObject()
            .put("private", fixture.getString("private"))
            .put("public", fixture.getString("public"))
            .put("kid", kid)
            .put("body", fixture.getJSONObject("body"))
            .put("body_b64", b64u(body))
            .put("envelope", envelope)
            .put("signing_input", detached.signingInput)
    Vectors.write("kotlin/es256_kotlin.json", out)
}
