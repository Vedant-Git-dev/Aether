plugins {
    kotlin("jvm")
}

repositories {
    mavenCentral()
}

dependencies {
    implementation("com.nimbusds:nimbus-jose-jwt:9.40")
    implementation("com.google.crypto.tink:tink:1.14.0")
    implementation("org.bouncycastle:bcprov-jdk18on:1.80")
    implementation("org.json:json:20240303")
    testImplementation("junit:junit:4.13.2")
}

kotlin {
    jvmToolchain(17)
}

val vectorsDir = rootProject.projectDir.parentFile.parentFile.resolve("tests/vault/vectors")

tasks.test {
    systemProperty("aether.vectors.dir", vectorsDir.absolutePath)
    testLogging {
        events("passed", "failed", "skipped")
    }
}

tasks.register<JavaExec>("generateVectors") {
    group = "verification"
    description = "Write the Kotlin-side public wire vectors."
    classpath = sourceSets["test"].runtimeClasspath
    mainClass.set("dev.aether.vault.vectors.GenerateVectorsKt")
    systemProperty("aether.vectors.dir", vectorsDir.absolutePath)
}
