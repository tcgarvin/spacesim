import org.jetbrains.kotlin.gradle.tasks.KotlinCompile

plugins {
    kotlin("jvm") version "1.4.10"
    application
}
group = "com.tcgarvin.spacesim"
version = "1.0-SNAPSHOT"

repositories {
    mavenCentral()
}
dependencies {
    implementation("org.apache.commons:commons-math3:3.6.1")
    implementation("org.tinfour:TinfourCore:2.1.5")
    implementation("org.jgrapht:jgrapht-core:1.5.1")
    implementation("org.deeplearning4j:rl4j:1.0.0-beta6")
    implementation("org.deeplearning4j:rl4j-api:1.0.0-beta6")
    implementation("org.deeplearning4j:rl4j-core:1.0.0-beta6")
    testImplementation(kotlin("test"))
    testImplementation(kotlin("test-junit"))
}
tasks.withType<KotlinCompile>() {
    kotlinOptions.jvmTarget = "11"
}

application {
    mainClass.set("MainKt")
}
