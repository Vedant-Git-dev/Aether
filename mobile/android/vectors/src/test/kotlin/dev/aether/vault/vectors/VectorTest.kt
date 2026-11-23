package dev.aether.vault.vectors

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

class VectorTest {
    @Test
    fun pythonHpkeVectorOpensAndReproducesBytes() {
        val fixture = Vectors.read("hpke_python.json")
        val recipientPrivate = unb64u(fixture.getString("recipient_private"))
        val recipientPublic = unb64u(fixture.getString("recipient_public"))
        val info = fixture.getString("info").toByteArray(Charsets.UTF_8)
        val aad = fixtureAad(fixture)
        assertEquals(fixture.getString("aad"), b64u(aad))
        val plaintext = unb64u(fixture.getString("plaintext"))
        val box = fixture.getJSONObject("box")

        val opened =
            Hpke.open(
                recipientPrivate,
                recipientPublic,
                info,
                aad,
                unb64u(box.getString("enc")),
                unb64u(box.getString("ct")),
            )
        assertArrayEquals(plaintext, opened)

        val (ct, enc) =
            Hpke.seal(
                recipientPublic,
                unb64u(fixture.getString("ephemeral_private")),
                unb64u(fixture.getString("ephemeral_public")),
                info,
                aad,
                plaintext,
            )
        assertEquals(box.getString("enc"), b64u(enc))
        assertEquals(box.getString("ct"), b64u(ct))
    }

    @Test
    fun pythonHpkeVectorRejectsChangedAad() {
        val fixture = Vectors.read("hpke_python.json")
        val aad = fixtureAad(fixture)
        assertEquals(fixture.getString("aad"), b64u(aad))
        aad[0] = (aad[0].toInt() xor 0x01).toByte()
        assertThrows(Exception::class.java) {
            Hpke.open(
                unb64u(fixture.getString("recipient_private")),
                unb64u(fixture.getString("recipient_public")),
                fixture.getString("info").toByteArray(Charsets.UTF_8),
                aad,
                unb64u(fixture.getJSONObject("box").getString("enc")),
                unb64u(fixture.getJSONObject("box").getString("ct")),
            )
        }
    }

    @Test
    fun pythonEd25519VectorVerifies() {
        val fixture = Vectors.read("ed25519_python.json")
        assertEquals(fixture.getString("signing_input"), fixtureSigningInput(fixture))
        val signature = unb64u(fixture.getJSONObject("envelope").getString("signature"))
        assertTrue(
            Signing.verifyEd25519(
                fixtureSigningInput(fixture),
                signature,
                unb64u(fixture.getString("public")),
            )
        )
        assertFalse(
            Signing.verifyEd25519(
                fixtureSigningInput(fixture) + "x",
                signature,
                unb64u(fixture.getString("public")),
            )
        )
    }

    @Test
    fun pythonEs256VectorVerifies() {
        val fixture = Vectors.read("es256_python.json")
        assertEquals(fixture.getString("signing_input"), fixtureSigningInput(fixture))
        val signature = unb64u(fixture.getJSONObject("envelope").getString("signature"))
        assertTrue(
            Signing.verifyEs256(
                fixtureSigningInput(fixture),
                signature,
                unb64u(fixture.getString("public")),
            )
        )
        assertFalse(
            Signing.verifyEs256(
                fixtureSigningInput(fixture) + "x",
                signature,
                unb64u(fixture.getString("public")),
            )
        )
    }

    @Test
    fun kotlinHpkeVectorSelfChecks() {
        val fixture = Vectors.read("kotlin/hpke_kotlin.json")
        val info = fixture.getString("info").toByteArray(Charsets.UTF_8)
        val aad = fixtureAad(fixture)
        assertEquals(fixture.getString("aad"), b64u(aad))
        val plaintext = unb64u(fixture.getString("plaintext"))
        val box = fixture.getJSONObject("box")
        assertArrayEquals(
            plaintext,
            Hpke.open(
                unb64u(fixture.getString("recipient_private")),
                unb64u(fixture.getString("recipient_public")),
                info,
                aad,
                unb64u(box.getString("enc")),
                unb64u(box.getString("ct")),
            ),
        )
    }

    @Test
    fun kotlinEd25519VectorSelfChecks() {
        val fixture = Vectors.read("kotlin/ed25519_kotlin.json")
        assertTrue(
            Signing.verifyEd25519(
                fixtureSigningInput(fixture),
                unb64u(fixture.getJSONObject("envelope").getString("signature")),
                unb64u(fixture.getString("public")),
            )
        )
    }

    @Test
    fun kotlinEs256VectorSelfChecks() {
        val fixture = Vectors.read("kotlin/es256_kotlin.json")
        assertTrue(
            Signing.verifyEs256(
                fixtureSigningInput(fixture),
                unb64u(fixture.getJSONObject("envelope").getString("signature")),
                unb64u(fixture.getString("public")),
            )
        )
    }
}
