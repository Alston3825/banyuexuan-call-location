plugins { id("com.android.library") }
android {
    namespace = "cn.banyuexuan.call.common"
    compileSdk = 37
    defaultConfig { minSdk = 26 }
}
dependencies {
    api("com.hivemq:hivemq-mqtt-client:1.4.2")
}
