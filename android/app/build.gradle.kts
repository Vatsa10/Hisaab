plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "app.hisaab.otp"
    compileSdk = 34

    defaultConfig {
        applicationId = "app.hisaab.otp"
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0-beta"
        // The PWA's API. Change it here or on the app's screen.
        buildConfigField("String", "DEFAULT_SERVER", "\"https://api.gethisaab.online\"")
    }

    buildFeatures { buildConfig = true }

    buildTypes {
        release {
            isMinifyEnabled = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"))
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }

}

dependencies {
    implementation("androidx.security:security-crypto:1.1.0-alpha06")
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
}
