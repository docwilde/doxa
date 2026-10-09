package ai.ampiric.doxa.remote

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.os.Build
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import com.google.firebase.FirebaseApp
import com.google.firebase.FirebaseOptions
import com.google.firebase.messaging.FirebaseMessaging
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import java.security.SecureRandom

internal object FcmConfiguration {
    fun ready(): Boolean = listOf(BuildConfig.FIREBASE_APP_ID, BuildConfig.FIREBASE_API_KEY,
        BuildConfig.FIREBASE_PROJECT_ID, BuildConfig.FIREBASE_SENDER_ID).all(String::isNotBlank)

    fun initialize(context: Context): Boolean {
        if (!ready()) return false
        return try {
            if (FirebaseApp.getApps(context).isEmpty()) {
                FirebaseApp.initializeApp(context, FirebaseOptions.Builder()
                    .setApplicationId(BuildConfig.FIREBASE_APP_ID)
                    .setApiKey(BuildConfig.FIREBASE_API_KEY)
                    .setProjectId(BuildConfig.FIREBASE_PROJECT_ID)
                    .setGcmSenderId(BuildConfig.FIREBASE_SENDER_ID).build())
            }
            FirebaseApp.getApps(context).isNotEmpty()
        } catch (_: Exception) { false }
    }
}

/** Explicit, session-bound FCM opt-in. The server authenticates the owner and incarnation. */
internal class BackgroundPush(private val context: Context, private val prefs: SharedPreferences,
                              private val scope: CoroutineScope, private val status: (String) -> Unit) {
    var enabled by mutableStateOf(prefs.getBoolean("background_push", false))
        private set
    var registered by mutableStateOf(false)
        private set
    private var currentOrigin: String? = null
    private var currentSession: Session? = null

    fun select(origin: String, session: Session) {
        currentOrigin = origin
        currentSession = session
        if (!enabled) return
        val changed = prefs.getString("push_origin", null) != origin ||
            prefs.getString("push_target", null) != session.id ||
            prefs.getString("push_incarnation", null) != session.incarnation
        val oldOrigin = prefs.getString("push_origin", null)
        val oldToken = prefs.getString("push_token", null)
        if (changed) {
            registered = false
            val tag = ByteArray(16).also { SecureRandom().nextBytes(it) }
                .joinToString("") { "%02x".format(it.toInt() and 0xff) }
            prefs.edit().putString("push_origin", origin).putString("push_target", session.id)
                .putString("push_incarnation", session.incarnation).putString("push_tag", tag).apply()
            // A same-hub POST atomically replaces the old target for this token.
            if (oldOrigin != null && oldOrigin != origin && oldToken != null) scope.launch(Dispatchers.IO) {
                try { HubApi(oldOrigin, null).unregisterAndroidPush(oldToken) } catch (_: Exception) { }
            }
        }
        register()
    }

    fun clearSelection() { currentOrigin = null; currentSession = null }

    fun enable() {
        val origin = currentOrigin
        val session = currentSession
        if (origin == null || session == null || session.incarnation.isBlank()) {
            status("Choose a live session before enabling background alerts"); return
        }
        if (!permission()) { status("Allow Android notifications first"); return }
        if (!FcmConfiguration.initialize(context)) { status("This build has no Firebase configuration"); return }
        enabled = true
        prefs.edit().putBoolean("background_push", true).apply()
        FirebaseMessaging.getInstance().isAutoInitEnabled = true
        select(origin, session)
    }

    fun disable() {
        enabled = false
        registered = false
        val origin = prefs.getString("push_origin", null)
        val token = prefs.getString("push_token", null)
        // Clear the local routing tag before any network call. Old messages are ignored offline.
        prefs.edit().putBoolean("background_push", false).remove("push_tag")
            .remove("push_target").remove("push_incarnation").remove("push_origin").remove("push_token").apply()
        if (FcmConfiguration.initialize(context)) {
            FirebaseMessaging.getInstance().isAutoInitEnabled = false
            FirebaseMessaging.getInstance().deleteToken()
        }
        if (origin != null && token != null) scope.launch(Dispatchers.IO) {
            try { HubApi(origin, null).unregisterAndroidPush(token) } catch (_: Exception) { }
        }
    }

    private fun register() {
        registered = false
        val origin = currentOrigin ?: return
        val session = currentSession ?: return
        val tag = prefs.getString("push_tag", null) ?: return
        if (!enabled || !FcmConfiguration.initialize(context) || !AndroidPushWire.validTag(tag)) return
        FirebaseMessaging.getInstance().token.addOnSuccessListener { token ->
            if (!AndroidPushWire.token(token) || !enabled || prefs.getString("push_tag", null) != tag) return@addOnSuccessListener
            prefs.edit().putString("push_token", token).apply()
            scope.launch(Dispatchers.IO) {
                try {
                    HubApi(origin, null).registerAndroidPush(session, token, tag)
                    if (enabled && prefs.getString("push_tag", null) == tag) {
                        withContext(Dispatchers.Main.immediate) {
                            registered = true
                            status("Background alerts enabled for ${session.title}")
                        }
                    }
                } catch (error: Exception) { status(error.message ?: "Background registration failed; reconnect to retry") }
            }
        }.addOnFailureListener { status("FCM token unavailable; reconnect to retry") }
    }

    private fun permission(): Boolean {
        val manager = context.getSystemService(NotificationManager::class.java)
        return manager.areNotificationsEnabled() && (Build.VERSION.SDK_INT < 33 ||
            context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED)
    }
}

/** Data-only FCM messages carry a generic kind and random routing tag, never session content. */
class DoxaFirebaseService : FirebaseMessagingService() {
    override fun onNewToken(token: String) {
        val prefs = getSharedPreferences("doxa-remote", MODE_PRIVATE)
        if (!prefs.getBoolean("background_push", false) || !FcmConfiguration.initialize(this)
            || !AndroidPushWire.token(token)) return
        prefs.edit().putString("push_token", token).apply()
        val origin = prefs.getString("push_origin", null) ?: return
        val target = prefs.getString("push_target", null) ?: return
        val incarnation = prefs.getString("push_incarnation", null) ?: return
        val tag = prefs.getString("push_tag", null) ?: return
        if (!Wire.target(target) || !AndroidPushWire.validTag(tag)) return
        try {
            runBlocking(Dispatchers.IO) {
                HubApi(origin, null).registerAndroidPush(Session(target, target, "session", false, incarnation), token, tag)
            }
        } catch (_: Exception) { /* Next foreground connection retries registration. */ }
    }

    override fun onMessageReceived(message: RemoteMessage) {
        val prefs = getSharedPreferences("doxa-remote", MODE_PRIVATE)
        if (message.from != BuildConfig.FIREBASE_SENDER_ID) return
        val kind = message.data["kind"] ?: return
        val tag = message.data["tag"] ?: return
        val expected = prefs.getString("push_tag", null) ?: return
        if (!AndroidPushWire.accepted(kind, tag, expected, prefs.getBoolean("background_push", false))
            || LocalAlerts.appVisible) return
        val manager = getSystemService(NotificationManager::class.java)
        val channel = "doxa_remote_background"
        manager.createNotificationChannel(NotificationChannel(channel, "DOXA background alerts",
            NotificationManager.IMPORTANCE_DEFAULT))
        if (!manager.areNotificationsEnabled() ||
            (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)) return
        val open = PendingIntent.getActivity(this, 0,
            Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val notification = Notification.Builder(this, channel)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle(if (kind == "needs_input") "DOXA needs input" else "DOXA turn finished")
            .setContentText("Open DOXA Remote to review current state")
            .setContentIntent(open).setAutoCancel(true).setVisibility(Notification.VISIBILITY_SECRET).build()
        try { manager.notify(if (kind == "needs_input") 100 else 101, notification) }
        catch (_: SecurityException) { }
    }
}
