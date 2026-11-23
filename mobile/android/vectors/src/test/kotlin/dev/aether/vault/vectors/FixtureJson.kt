package dev.aether.vault.vectors

import org.json.JSONArray
import org.json.JSONObject

/** Canonical serialization for the safe-integer and ASCII strings in the wire fixtures. */
fun fixtureJson(value: Any): String = when {
    value === JSONObject.NULL -> "null"
    value is JSONObject -> value.keySet().sorted().joinToString(",", "{", "}") {
        fixtureJson(it) + ":" + fixtureJson(value.get(it))
    }
    value is JSONArray -> (0 until value.length()).joinToString(",", "[", "]") {
        fixtureJson(value.get(it))
    }
    value is String -> {
        require(value.all { it in ' '..'~' })
        "\"" + value.replace("\\", "\\\\").replace("\"", "\\\"") + "\""
    }
    value is Boolean -> value.toString()
    value is Int || value is Long -> {
        require((value as Number).toLong() in -9007199254740991L..9007199254740991L)
        value.toString()
    }
    else -> error("Unsupported wire fixture value: $value")
}

fun fixtureSigningInput(fixture: JSONObject): String {
    val envelope = fixture.getJSONObject("envelope")
    val protected = fixtureJson(JSONObject()
        .put("alg", if (fixture.has("seed")) "Ed25519" else "ES256")
        .put("kid", fixture.getString("kid")))
    return b64u(protected.toByteArray(Charsets.UTF_8)) + "." +
        b64u(fixtureJson(envelope.getJSONObject("body")).toByteArray(Charsets.UTF_8))
}

fun fixtureAad(fixture: JSONObject): ByteArray =
    fixtureJson(fixture.getJSONObject("header")).toByteArray(Charsets.UTF_8)
