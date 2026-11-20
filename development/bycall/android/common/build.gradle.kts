plugins { id("com.android.library") }
android {
    namespace = "cn.banyuexuan.call.common"
    compileSdk = 37
    defaultConfig { minSdk = 26 }
}
dependencies {
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
    api("com.hivemq:hivemq-mqtt-client:1.4.2")
}
