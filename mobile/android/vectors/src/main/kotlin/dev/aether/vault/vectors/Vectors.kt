package dev.aether.vault.vectors

import org.json.JSONObject
import java.nio.file.Files
import java.nio.file.Path
import java.util.Base64

object Vectors {
    val dir: Path =
        Path.of(System.getProperty("aether.vectors.dir") ?: error("aether.vectors.dir is not set"))

    fun read(name: String): JSONObject = JSONObject(Files.readString(dir.resolve(name)))

    fun write(name: String, value: JSONObject) {
        val target = dir.resolve(name)
        Files.createDirectories(target.parent)
        Files.writeString(target, value.toString(2) + "\n")
    }
}

fun b64u(bytes: ByteArray): String = Base64.getUrlEncoder().withoutPadding().encodeToString(bytes)

fun unb64u(text: String): ByteArray = Base64.getUrlDecoder().decode(text)
