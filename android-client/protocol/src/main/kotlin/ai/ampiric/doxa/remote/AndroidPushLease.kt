package ai.ampiric.doxa.remote

/** Monotonic, process-local proof of a successfully registered hub lease. */
class AndroidPushLease {
    private var expiresAt: Long? = null
    private var renewAt: Long? = null

    fun clear() { expiresAt = null; renewAt = null }

    fun accept(now: Long, expiresInSeconds: Long): Boolean {
        clear()
        if (now < 0 || expiresInSeconds !in 1..86_400) return false
        val duration = expiresInSeconds * 1_000
        if (now > Long.MAX_VALUE - duration) return false
        expiresAt = now + duration
        renewAt = now + duration - maxOf(1, duration / 10)
        return true
    }

    fun current(now: Long): Boolean = now >= 0 && expiresAt?.let { now < it } == true

    /** Renew ahead of expiry while visible; unknown or expired leases renew now. */
    fun renewIn(now: Long): Long = if (!current(now)) 0 else maxOf(0, (renewAt ?: now) - now)
}
