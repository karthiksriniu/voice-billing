import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

/* Release signing comes from a keystore.properties this repo never sees (see
 * android/README.md). Without it the release build falls back to the debug key so that
 * `assembleRelease` still works on a fresh clone — but a debug-signed APK must never go
 * on a shopkeeper's phone: the upgrade path breaks permanently the day we switch keys. */
val keystoreProps = Properties().apply {
    val f = rootProject.file("keystore.properties")
    if (f.exists()) f.inputStream().use { load(it) }
}
val hasReleaseKey = keystoreProps.getProperty("storeFile") != null

android {
    namespace = "com.synthia.app"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.synthia.app"
        minSdk = 29                 // Android 10, the floor in CLAUDE.md
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"

        // The UI is served, not bundled: a sideloaded APK has no Play update channel, so
        // anything that can be fixed without reinstalling should be.
        buildConfigField("String", "WEB_BASE", "\"https://bolo-bill.vercel.app\"")

        ndk {
            // x86 exists only for emulators and would triple the APK.
            abiFilters += listOf("arm64-v8a", "armeabi-v7a")
        }
    }

    signingConfigs {
        if (hasReleaseKey) {
            create("release") {
                storeFile = rootProject.file(keystoreProps.getProperty("storeFile"))
                storePassword = keystoreProps.getProperty("storePassword")
                keyAlias = keystoreProps.getProperty("keyAlias")
                keyPassword = keystoreProps.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = if (hasReleaseKey) signingConfigs.getByName("release")
                            else signingConfigs.getByName("debug")
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
        debug {
            applicationIdSuffix = ".debug"
            versionNameSuffix = "-debug"
            // Talks to the POC running on the developer's Mac via `adb reverse tcp:8077`,
            // so the page and the APK can be iterated on together without a deploy.
            buildConfigField("String", "WEB_BASE", "\"http://localhost:8077\"")
        }
    }

    /* One APK per architecture. Sideloading has no Play Store to pick the right slice, so
     * we hand out the arm64 file (every phone since ~2017) and keep the universal build as
     * the fallback for anything odd. Splitting matters here: onnxruntime is ~21 MB per
     * ABI, so a universal APK is nearly double an arm64-only one over somebody's data. */
    splits {
        abi {
            isEnable = true
            reset()
            include("arm64-v8a", "armeabi-v7a")
            isUniversalApk = true
        }
    }

    // The ONNX graphs are mapped straight out of the APK by sherpa-onnx; compressing them
    // would force a copy to disk on first run for no size win.
    androidResources {
        noCompress += listOf("onnx", "model")
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures { buildConfig = true; viewBinding = true }
}

dependencies {
    implementation(files("libs/sherpa-onnx-1.13.6.aar"))
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.activity:activity-ktx:1.9.3")
    implementation("androidx.localbroadcastmanager:localbroadcastmanager:1.1.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
}
