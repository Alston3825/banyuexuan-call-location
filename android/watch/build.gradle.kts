plugins { id("com.android.application") }
android {
    namespace = "cn.banyuexuan.call.watch"
    compileSdk = 37
    defaultConfig {
        applicationId = "cn.banyuexuan.call.watch"
        minSdk = 26
        targetSdk = 36
        versionCode = (System.currentTimeMillis() / 60000L).toInt()   // 每次编译自动递增(以分钟计),无需手改
        versionName = "0.2"
    }
    packaging { resources { excludes += setOf("META-INF/INDEX.LIST", "META-INF/io.netty.versions.properties", "META-INF/DEPENDENCIES") } }
}
dependencies { implementation(project(":common")) }
