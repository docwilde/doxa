plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.plugin.compose")
}

android {
    namespace = "ai.ampiric.doxa.remote"
    compileSdk = 37

    defaultConfig {
        applicationId = "ai.ampiric.doxa.remote"
        minSdk = 26
        targetSdk = 36
        versionCode = 1
        versionName = "0.1.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }
    buildFeatures { compose = true; buildConfig = true; resValues = true }
    // Public Firebase app identifiers are supplied only for configured builds.
    val firebaseAppId = providers.gradleProperty("doxaFirebaseAppId").orElse("").get()
    val firebaseApiKey = providers.gradleProperty("doxaFirebaseApiKey").orElse("").get()
    val firebaseProjectId = providers.gradleProperty("doxaFirebaseProjectId").orElse("").get()
    val firebaseSenderId = providers.gradleProperty("doxaFirebaseSenderId").orElse("").get()
    defaultConfig {
        buildConfigField("String", "FIREBASE_APP_ID", "\"$firebaseAppId\"")
        buildConfigField("String", "FIREBASE_API_KEY", "\"$firebaseApiKey\"")
        buildConfigField("String", "FIREBASE_PROJECT_ID", "\"$firebaseProjectId\"")
        buildConfigField("String", "FIREBASE_SENDER_ID", "\"$firebaseSenderId\"")
        // FirebaseInitProvider needs these on process start for background delivery.
        resValue("string", "google_app_id", firebaseAppId)
        resValue("string", "google_api_key", firebaseApiKey)
        resValue("string", "gcm_defaultSenderId", firebaseSenderId)
        resValue("string", "project_id", firebaseProjectId)
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    packaging { resources.excludes += "/META-INF/{AL2.0,LGPL2.1}" }
}

dependencies {
    implementation(project(":protocol"))
    implementation(platform("com.google.firebase:firebase-bom:35.0.0"))
    implementation("com.google.firebase:firebase-messaging")
    val composeBom = platform("androidx.compose:compose-bom:2026.09.00")
    implementation(composeBom)
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.activity:activity-compose:1.13.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")
}
