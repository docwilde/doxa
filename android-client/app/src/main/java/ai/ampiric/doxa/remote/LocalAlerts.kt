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
import android.os.SystemClock
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue

/** Best-effort local alerts while an authenticated SSE connection is still running. */
internal class LocalAlerts(private val context: Context, private val prefs: SharedPreferences) {
    private val manager = context.getSystemService(NotificationManager::class.java)
    private val channel = "doxa_remote_local_events"
    private val last = mutableMapOf<AlertKind, Long>()
    var enabled by mutableStateOf(false)
        private set
    var visible = true

    init {
        manager.createNotificationChannel(NotificationChannel(channel, "DOXA local events",
            NotificationManager.IMPORTANCE_DEFAULT))
        enabled = prefs.getBoolean("local_alerts", false) && hasPermission()
    }

    fun hasPermission(): Boolean = manager.areNotificationsEnabled() &&
        manager.getNotificationChannel(channel)?.importance != NotificationManager.IMPORTANCE_NONE &&
        (Build.VERSION.SDK_INT < 33 || context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) ==
            PackageManager.PERMISSION_GRANTED)

    fun updateEnabled(value: Boolean) {
        enabled = value && hasPermission()
        prefs.edit().putBoolean("local_alerts", enabled).apply()
    }

    fun onEvent(event: String) {
        val kind = AlertKind.fromEvent(event) ?: return
        if (!enabled || visible || !hasPermission()) return
        val now = SystemClock.elapsedRealtime()
        if (now - (last[kind] ?: Long.MIN_VALUE / 2) < 60_000) return
        last[kind] = now
        val open = PendingIntent.getActivity(context, 0,
            Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val notification = Notification.Builder(context, channel)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle(kind.title)
            .setContentText("Open DOXA Remote to review current state")
            .setContentIntent(open)
            .setAutoCancel(true)
            .setVisibility(Notification.VISIBILITY_SECRET)
            .build()
        try { manager.notify(kind.ordinal + 1, notification) }
        catch (_: SecurityException) { updateEnabled(false) }
    }
}
